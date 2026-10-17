"""Access Policy releases: compile → review → deliver → track (Phase 2).

A release is the compiled desired state of *all active* Access Policies for one
target, plus its diff against the Grant Ledger. It is reviewed like any other
governed artifact (separation of duties on approval), then shipped through the
Ontos Delivery Modes the admin picks:

* **Direct** — ``GrantManager`` applies GRANT/REVOKE via the SDK; per-item
  success is written to the ledger and a fully applied release is ``applied``.
* **Indirect** — Ontos YAML in the Git repo (``AccessPolicyReleaseFileModel``).
* **Manual** — an admin notification with a SQL runbook.

Indirect / Manual releases stay ``delivered`` until an admin confirms they were
applied; only then does the ledger change. *Applying* grants and *tracking*
them are separate: the ledger is what drives time-bound (GDPR) revocation —
:meth:`PolicyReleaseManager.revoke_expired` ships expired grants as a revocation
release through the same Delivery Modes.
"""
import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.common import uc_policy_adapter as uc
from src.common.logging import get_logger
from src.common.odrl_compiler import PolicyInput, compile_policies
from src.controller.access_policies_manager import (
    KIND_ASSET_COLLECTION,
    KIND_AUDIENCE,
    KIND_POLICY,
    STATUS_ACTIVE,
    AccessPoliciesManager,
    AccessPolicyError,
)
from src.db_models.access_policies import (
    ENTITY_TYPE_RELEASE,
    AccessGrantLedgerDb,
    AccessPolicyReleaseDb,
)
from src.repositories.access_policies_repository import repo_for

logger = get_logger(__name__)

TARGETS = (uc.TARGET_UC,)

KIND_POLICY_RELEASE = "policy"
KIND_REVOCATION = "revocation"

R_DRAFT = "draft"
R_UNDER_REVIEW = "under_review"
R_APPROVED = "approved"
R_DELIVERED = "delivered"
R_APPLIED = "applied"
R_FAILED = "failed"
R_SUPERSEDED = "superseded"
R_DISCARDED = "discarded"

# Transitions reachable through change_status; delivered/applied/failed are set by deliver()/confirm_applied().
RELEASE_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    R_DRAFT: (R_UNDER_REVIEW, R_DISCARDED),
    R_UNDER_REVIEW: (R_APPROVED, R_DRAFT),
    R_APPROVED: (R_DRAFT, R_DISCARDED),
    R_DELIVERED: (),
    R_FAILED: (R_DISCARDED,),
    R_APPLIED: (),
    R_SUPERSEDED: (),
    R_DISCARDED: (),
}
OPEN_STATUSES = (R_DRAFT, R_UNDER_REVIEW, R_APPROVED, R_DELIVERED, R_FAILED)
DELIVERABLE_STATUSES = (R_APPROVED, R_FAILED)

LEDGER_ACTIVE = "active"
LEDGER_REVOKED = "revoked"

LIVE_DIFF_MAX_SECURABLES = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ledger_grant(row: AccessGrantLedgerDb) -> uc.UCGrant:
    return uc.UCGrant(row.principal, row.securable_type, row.securable_name, row.privilege,
                      row.expires_at, bool(row.implied), list(row.sources or []))


class PolicyReleaseManager:
    def __init__(self, policies: Optional[AccessPoliciesManager] = None):
        self.policies = policies or AccessPoliciesManager()

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _target(target: str) -> str:
        if target not in TARGETS:
            raise AccessPolicyError(f"Unknown target '{target}'. Supported: {', '.join(TARGETS)}", 400)
        return target

    def require(self, db: Session, release_id: str) -> AccessPolicyReleaseDb:
        row = db.get(AccessPolicyReleaseDb, release_id)
        if not row:
            raise AccessPolicyError("Release not found", 404)
        return row

    def _log_timeline(self, db: Session, row: AccessPolicyReleaseDb, message: str, user: Optional[str]) -> None:
        try:
            from src.controller.comments_manager import CommentsManager
            from src.models.comments import CommentCreate
            CommentsManager().create_comment(
                db, data=CommentCreate(entity_type=ENTITY_TYPE_RELEASE, entity_id=row.id, comment=message),
                user_email=user or "system@ontos", is_admin=True,
            )
        except Exception as e:  # pragma: no cover - timeline logging is best-effort
            logger.warning(f"Could not write timeline comment for release {row.id}: {e}")

    def active_ledger(self, db: Session, target: str) -> List[AccessGrantLedgerDb]:
        return (db.query(AccessGrantLedgerDb)
                .filter(AccessGrantLedgerDb.target == target, AccessGrantLedgerDb.status == LEDGER_ACTIVE)
                .all())

    def _fingerprint(self, db: Session, target: str) -> str:
        keys = sorted(f"{r.principal}|{r.securable_type}|{r.securable_name}|{r.privilege}|{r.expires_at or ''}"
                      for r in self.active_ledger(db, target))
        return hashlib.sha256("\n".join(keys).encode()).hexdigest()

    # ------------------------------------------------------------ compile

    def _active_policy_inputs(self, db: Session) -> Tuple[List[PolicyInput], List[Dict[str, Any]], List[str]]:
        inputs: List[PolicyInput] = []
        sources: List[Dict[str, Any]] = []
        warnings: List[str] = []
        for p in repo_for(KIND_POLICY).list_all(db):
            if p.status != STATUS_ACTIVE:
                continue
            doc, errs = self.policies._policy_doc(db, p)
            warnings.extend(f"{p.name}: {e}" for e in errs)
            inputs.append(PolicyInput(doc=doc, id=p.id, name=p.name, version=p.version))
            sources.append({"id": p.id, "uid": p.uid, "name": p.name, "version": p.version})
        return inputs, sources, warnings

    def compile(self, db: Session, target: str, *,
                resolve_object_kind: Optional[uc.ObjectKindResolver] = None,
                now: Optional[datetime] = None) -> Dict[str, Any]:
        """Compile active policies for ``target`` and diff against the ledger (no writes)."""
        self._target(target)
        inputs, sources, warnings = self._active_policy_inputs(db)
        plan = compile_policies(
            inputs,
            audiences=self.policies._collection_defs(db, KIND_AUDIENCE),
            asset_collections=self.policies._collection_defs(db, KIND_ASSET_COLLECTION),
            directory=lambda: self.policies.known_principals(db),
            now=now,
        )
        for w in warnings:
            plan.issue("error", "inheritance", w)
        grants = uc.build_grants(plan, resolve_object_kind)
        diff = uc.diff_grants(grants, [_ledger_grant(r) for r in self.active_ledger(db, target)])
        changes = diff.to_dict()
        changes["sql"] = uc.render_sql(diff)
        ir = plan.to_dict()
        return {
            "target": target,
            "plan": {"grants": [g.to_dict() for g in grants], "entitlements": ir["entitlements"],
                     "gated": ir["gated"], "obligations": ir["obligations"], "compiled_at": ir["compiled_at"]},
            "changes": changes,
            "issues": ir["issues"],
            "has_errors": plan.has_errors,
            "source_policies": sources,
            "ledger_fingerprint": self._fingerprint(db, target),
        }

    def create_release(self, db: Session, target: str, user: Optional[str], notes: Optional[str] = None, *,
                       resolve_object_kind: Optional[uc.ObjectKindResolver] = None) -> AccessPolicyReleaseDb:
        compiled = self.compile(db, target, resolve_object_kind=resolve_object_kind)
        seq = (db.query(func.max(AccessPolicyReleaseDb.sequence))
               .filter(AccessPolicyReleaseDb.target == target).scalar() or 0) + 1
        row = AccessPolicyReleaseDb(
            target=target, sequence=seq, release_kind=KIND_POLICY_RELEASE, status=R_DRAFT, notes=notes,
            plan=compiled["plan"], changes=compiled["changes"], issues=compiled["issues"],
            source_policies=compiled["source_policies"], ledger_fingerprint=compiled["ledger_fingerprint"],
            created_by=user,
        )
        db.add(row)
        db.flush()
        self._log_timeline(db, row, f"Release #{seq} compiled from {len(compiled['source_policies'])} active "
                                    f"policies: {self._summary(row)}", user)
        return row

    def recompile(self, db: Session, release_id: str, user: Optional[str], *,
                  resolve_object_kind: Optional[uc.ObjectKindResolver] = None) -> AccessPolicyReleaseDb:
        row = self.require(db, release_id)
        if row.status != R_DRAFT or row.release_kind != KIND_POLICY_RELEASE:
            raise AccessPolicyError("Only draft policy releases can be recompiled", 409)
        compiled = self.compile(db, row.target, resolve_object_kind=resolve_object_kind)
        row.plan, row.changes, row.issues = compiled["plan"], compiled["changes"], compiled["issues"]
        row.source_policies, row.ledger_fingerprint = compiled["source_policies"], compiled["ledger_fingerprint"]
        db.flush()
        self._log_timeline(db, row, f"Recompiled: {self._summary(row)}", user)
        return row

    # ------------------------------------------------------------ lifecycle

    def change_status(self, db: Session, release_id: str, new_status: str, user: Optional[str],
                      comment: Optional[str] = None, *, is_admin: bool = False) -> AccessPolicyReleaseDb:
        row = self.require(db, release_id)
        if new_status not in RELEASE_TRANSITIONS.get(row.status, ()):
            raise AccessPolicyError(f"Cannot move a release from '{row.status}' to '{new_status}'", 409)
        if new_status == R_UNDER_REVIEW:
            if any(i.get("severity") == "error" for i in row.issues or []):
                raise AccessPolicyError("Release has compile errors — fix the policies and recompile", 409,
                                        [i["message"] for i in row.issues if i.get("severity") == "error"])
            if self._is_empty(row):
                raise AccessPolicyError("Release has no changes to deliver", 409)
            row.submitted_by = user
        if new_status == R_APPROVED:
            if not is_admin and user and user.lower() in {(row.created_by or "").lower(), (row.submitted_by or "").lower()}:
                raise AccessPolicyError("Separation of duties: the author or submitter can't approve this release", 403)
            if row.ledger_fingerprint != self._fingerprint(db, row.target):
                raise AccessPolicyError("The Grant Ledger changed since this release was compiled — send it back to "
                                        "draft and recompile", 409)
            row.approved_by = user
        if new_status == R_DRAFT:
            row.approved_by = None
        row.status = new_status
        db.flush()
        self._log_timeline(db, row, f"Status → {new_status}" + (f": {comment}" if comment else ""), user)
        return row

    @staticmethod
    def _is_empty(row: AccessPolicyReleaseDb) -> bool:
        ch = row.changes or {}
        return not (ch.get("add") or ch.get("remove") or ch.get("update"))

    @staticmethod
    def _summary(row: AccessPolicyReleaseDb) -> str:
        ch = row.changes or {}
        return (f"{len(ch.get('add') or [])} to grant, {len(ch.get('remove') or [])} to revoke, "
                f"{len(ch.get('update') or [])} expiry updates")

    # ------------------------------------------------------------ delivery

    def _remaining(self, db: Session, row: AccessPolicyReleaseDb) -> Tuple[List[uc.UCGrant], List[uc.UCGrant]]:
        """Changes not yet reflected in the ledger (all of them on first delivery; the rest on re-delivery)."""
        have = {(r.principal, r.securable_type, r.securable_name, r.privilege) for r in self.active_ledger(db, row.target)}
        ch = row.changes or {}
        adds = [g for g in (uc.UCGrant.from_dict(d) for d in ch.get("add") or []) if g.key not in have]
        removes = [g for g in (uc.UCGrant.from_dict(d) for d in ch.get("remove") or []) if g.key in have]
        return adds, removes

    def deliver(self, db: Session, release_id: str, user: Optional[str], delivery_service: Any,
                modes: Optional[List[str]] = None) -> AccessPolicyReleaseDb:
        from src.controller.delivery_service import (
            DeliveryChangeType,
            DeliveryMode,
            DeliveryPayload,
        )

        row = self.require(db, release_id)
        if row.status not in DELIVERABLE_STATUSES:
            raise AccessPolicyError(f"Only approved (or failed) releases can be delivered; this one is '{row.status}'", 409)
        if row.status == R_APPROVED and row.ledger_fingerprint != self._fingerprint(db, row.target):
            raise AccessPolicyError("The Grant Ledger changed since this release was compiled — recompile it", 409)
        if delivery_service is None:
            raise AccessPolicyError("Delivery service is not configured", 503)
        active = delivery_service.get_active_modes()
        if modes:
            chosen = [DeliveryMode(m) for m in modes]
            not_enabled = [m.value for m in chosen if m not in active]
            if not_enabled:
                raise AccessPolicyError(f"Delivery mode(s) not enabled in Settings: {', '.join(not_enabled)}", 400)
        else:
            chosen = active
        if not chosen:
            raise AccessPolicyError("No delivery mode is enabled in Settings → Delivery", 409)

        adds, removes = self._remaining(db, row)
        diff = uc.GrantDiff(add=adds, remove=removes)
        payload = DeliveryPayload(
            change_type=DeliveryChangeType.POLICY_RELEASE,
            entity_type="AccessPolicyRelease",
            entity_id=row.id,
            data={
                "entity": row,
                "target": row.target,
                "sequence": row.sequence,
                "summary": f"Access Policy release #{row.sequence} for {row.target}: "
                           f"{len(adds)} to grant, {len(removes)} to revoke.",
                "grant": uc.group_for_apply(adds),
                "revoke": uc.group_for_apply(removes),
                "sql": uc.render_sql(diff),
                "link": f"/access-policies/releases/{row.id}",
            },
            user=user,
            db=db,
        )
        results = delivery_service.deliver(payload, chosen)
        result_dicts = [r.to_dict() for r in results.results]
        row.delivery_modes = [m.value for m in chosen]
        row.delivery_results = (row.delivery_results or []) + [{"at": _now().isoformat(), "by": user,
                                                                "results": result_dicts}]
        row.delivered_by, row.delivered_at = user, _now()

        direct = next((r for r in results.results if r.mode == DeliveryMode.DIRECT), None)
        applied_directly = direct is not None and not direct.details.get("dry_run") and not direct.details.get("skipped")
        if applied_directly:
            ok_add, ok_remove = self._succeeded_items(direct.details.get("items") or [], adds, removes)
            self._apply_to_ledger(db, row, ok_add, ok_remove, user)
            if len(ok_add) == len(adds) and len(ok_remove) == len(removes):
                self._mark_applied(db, row, user, "applied directly via the Databricks SDK")
            else:
                row.status = R_FAILED
                self._log_timeline(db, row, f"Direct delivery partially failed: {len(ok_add)}/{len(adds)} grants, "
                                            f"{len(ok_remove)}/{len(removes)} revokes applied", user)
        else:
            failed = [r for r in results.results if not r.success]
            row.status = R_FAILED if failed and len(failed) == len(results.results) else R_DELIVERED
            self._log_timeline(db, row, f"Delivered via {', '.join(m.value for m in chosen)} — "
                                        + ("awaiting confirmation that it was applied" if row.status == R_DELIVERED
                                           else "; ".join(r.error or "failed" for r in failed)), user)
        db.flush()
        return row

    @staticmethod
    def _succeeded_items(items: List[Dict[str, Any]], adds: List[uc.UCGrant],
                         removes: List[uc.UCGrant]) -> Tuple[List[uc.UCGrant], List[uc.UCGrant]]:
        ok: Dict[str, set] = {"grant": set(), "revoke": set()}
        for it in items:
            if it.get("success"):
                for priv in it.get("privileges") or []:
                    ok[it["op"]].add((it["principal"], it["securable_type"], it["securable_name"], priv))
        return [g for g in adds if g.key in ok["grant"]], [g for g in removes if g.key in ok["revoke"]]

    def confirm_applied(self, db: Session, release_id: str, user: Optional[str],
                        comment: Optional[str] = None) -> AccessPolicyReleaseDb:
        """An admin confirms an Indirect / Manual release was applied on the target → update the ledger."""
        row = self.require(db, release_id)
        if row.status != R_DELIVERED:
            raise AccessPolicyError("Only delivered releases can be confirmed as applied", 409)
        adds, removes = self._remaining(db, row)
        self._apply_to_ledger(db, row, adds, removes, user)
        self._mark_applied(db, row, user, "confirmed applied" + (f": {comment}" if comment else ""))
        db.flush()
        return row

    def _mark_applied(self, db: Session, row: AccessPolicyReleaseDb, user: Optional[str], how: str) -> None:
        row.status, row.applied_by, row.applied_at = R_APPLIED, user, _now()
        for other in (db.query(AccessPolicyReleaseDb)
                      .filter(AccessPolicyReleaseDb.target == row.target, AccessPolicyReleaseDb.id != row.id,
                              AccessPolicyReleaseDb.status.in_(OPEN_STATUSES)).all()):
            other.status = R_SUPERSEDED
            self._log_timeline(db, other, f"Superseded by release #{row.sequence}", user)
        self._log_timeline(db, row, f"Release {how}", user)

    def _apply_to_ledger(self, db: Session, row: AccessPolicyReleaseDb, adds: List[uc.UCGrant],
                         removes: List[uc.UCGrant], user: Optional[str]) -> None:
        active = {(r.principal, r.securable_type, r.securable_name, r.privilege): r for r in self.active_ledger(db, row.target)}
        now = _now()
        for g in removes:
            r = active.pop(g.key, None)
            if r:
                r.status, r.revoked_at, r.revoked_release_id = LEDGER_REVOKED, now, row.id
        for g in adds:
            if g.key in active:
                continue
            entry = AccessGrantLedgerDb(target=row.target, principal=g.principal, securable_type=g.securable_type,
                                        securable_name=g.securable_name, privilege=g.privilege, implied=g.implied,
                                        expires_at=g.expires_at, sources=g.sources, status=LEDGER_ACTIVE,
                                        granted_release_id=row.id)
            db.add(entry)
            active[g.key] = entry
        # Ledger-only refresh (expiry, provenance) for every desired grant that is applied.
        for d in (row.plan or {}).get("grants") or []:
            g = uc.UCGrant.from_dict(d)
            r = active.get(g.key)
            if r is not None:
                r.expires_at, r.sources, r.implied = g.expires_at, g.sources, g.implied
        db.flush()

    # ------------------------------------------------------------ expiry (GDPR)

    def expired_grants(self, db: Session, target: str, now: Optional[datetime] = None) -> List[AccessGrantLedgerDb]:
        now = now or _now()
        return [r for r in self.active_ledger(db, target)
                if (exp := _parse_iso(r.expires_at)) is not None and exp <= now]

    def revoke_expired(self, db: Session, target: str, user: Optional[str], delivery_service: Any,
                       now: Optional[datetime] = None) -> Optional[AccessPolicyReleaseDb]:
        """Ship a revocation release for every expired ledger grant (auto-approved: revocation is mandatory)."""
        self._target(target)
        expired = self.expired_grants(db, target, now)
        if not expired:
            return None
        expired_keys = {(r.principal, r.securable_type, r.securable_name, r.privilege) for r in expired}
        remaining = [_ledger_grant(r) for r in self.active_ledger(db, target)
                     if (r.principal, r.securable_type, r.securable_name, r.privilege) not in expired_keys]
        diff = uc.GrantDiff(remove=sorted((_ledger_grant(r) for r in expired), key=lambda g: g.key))
        changes = diff.to_dict()
        changes["sql"] = uc.render_sql(diff)
        seq = (db.query(func.max(AccessPolicyReleaseDb.sequence))
               .filter(AccessPolicyReleaseDb.target == target).scalar() or 0) + 1
        row = AccessPolicyReleaseDb(
            target=target, sequence=seq, release_kind=KIND_REVOCATION, status=R_APPROVED,
            notes=f"Automatic revocation of {len(expired)} expired grant(s)",
            plan={"grants": [g.to_dict() for g in remaining]}, changes=changes, issues=[],
            source_policies=[], ledger_fingerprint=self._fingerprint(db, target),
            created_by=user, submitted_by=user, approved_by=user,
        )
        db.add(row)
        db.flush()
        self._log_timeline(db, row, f"Revocation release #{seq} created for {len(expired)} expired grant(s)", user)
        return self.deliver(db, row.id, user, delivery_service)

    # ------------------------------------------------------------ live state

    def live_diff(self, db: Session, release_id: str, get_grants: Callable[[str, str], Dict[str, Any]]) -> Dict[str, Any]:
        """Report-only comparison of the release's desired grants with live UC grants."""
        row = self.require(db, release_id)
        desired = [uc.UCGrant.from_dict(d) for d in (row.plan or {}).get("grants") or []]
        managed = [_ledger_grant(r) for r in self.active_ledger(db, row.target)]
        securables = sorted({(g.securable_type, g.securable_name) for g in desired + managed})
        truncated = len(securables) > LIVE_DIFF_MAX_SECURABLES
        live: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        errors: List[str] = []
        for stype, sname in securables[:LIVE_DIFF_MAX_SECURABLES]:
            res = get_grants(stype, sname) or {}
            if res.get("success"):
                live[(stype, sname)] = res.get("grants") or []
            else:
                errors.append(f"{stype} {sname}: {res.get('error') or 'unreadable'}")
        checked = set(live)
        report = uc.live_diff([g for g in managed if (g.securable_type, g.securable_name) in checked],
                              [g for g in desired if (g.securable_type, g.securable_name) in checked], live)
        report.update({"checked_at": _now().isoformat(), "securables_checked": len(checked),
                       "truncated": truncated, "errors": errors})
        row.live_diff = report
        db.flush()
        return report

    # ------------------------------------------------------------ read model

    def list(self, db: Session, target: Optional[str] = None, status: Optional[str] = None) -> List[AccessPolicyReleaseDb]:
        q = db.query(AccessPolicyReleaseDb)
        if target:
            q = q.filter(AccessPolicyReleaseDb.target == target)
        if status:
            q = q.filter(AccessPolicyReleaseDb.status == status)
        return q.order_by(AccessPolicyReleaseDb.target, AccessPolicyReleaseDb.sequence.desc()).all()

    def ledger(self, db: Session, target: Optional[str] = None, status: Optional[str] = LEDGER_ACTIVE,
               principal: Optional[str] = None) -> List[Dict[str, Any]]:
        q = db.query(AccessGrantLedgerDb)
        if target:
            q = q.filter(AccessGrantLedgerDb.target == target)
        if status:
            q = q.filter(AccessGrantLedgerDb.status == status)
        if principal:
            q = q.filter(AccessGrantLedgerDb.principal.ilike(principal))
        now = _now()
        out = []
        for r in q.order_by(AccessGrantLedgerDb.securable_name, AccessGrantLedgerDb.principal,
                            AccessGrantLedgerDb.privilege).all():
            exp = _parse_iso(r.expires_at)
            out.append({
                "id": r.id, "target": r.target, "principal": r.principal, "securable_type": r.securable_type,
                "securable_name": r.securable_name, "privilege": r.privilege, "implied": bool(r.implied),
                "expires_at": r.expires_at, "expired": bool(exp and exp <= now), "sources": r.sources or [],
                "legal_basis": r.legal_basis, "status": r.status,
                "granted_release_id": r.granted_release_id, "revoked_release_id": r.revoked_release_id,
                "granted_at": r.granted_at, "revoked_at": r.revoked_at,
            })
        return out

    def to_read_dict(self, row: AccessPolicyReleaseDb) -> Dict[str, Any]:
        ch = row.changes or {}
        issues = row.issues or []
        return {
            "id": row.id, "entity_type": ENTITY_TYPE_RELEASE, "target": row.target, "sequence": row.sequence,
            "name": f"{row.target.upper()} release #{row.sequence}",
            "release_kind": row.release_kind, "status": row.status, "notes": row.notes,
            "allowed_transitions": list(RELEASE_TRANSITIONS.get(row.status, ())),
            "deliverable": row.status in DELIVERABLE_STATUSES,
            "confirmable": row.status == R_DELIVERED,
            "counts": {"add": len(ch.get("add") or []), "remove": len(ch.get("remove") or []),
                       "update": len(ch.get("update") or []), "unchanged": ch.get("unchanged", 0),
                       "grants": len((row.plan or {}).get("grants") or []),
                       "gated": len((row.plan or {}).get("gated") or []),
                       "errors": sum(1 for i in issues if i.get("severity") == "error"),
                       "warnings": sum(1 for i in issues if i.get("severity") == "warning")},
            "plan": row.plan or {}, "changes": ch, "issues": issues,
            "source_policies": row.source_policies or [],
            "delivery_modes": row.delivery_modes or [], "delivery_results": row.delivery_results or [],
            "live_diff": row.live_diff,
            "created_by": row.created_by, "submitted_by": row.submitted_by, "approved_by": row.approved_by,
            "delivered_by": row.delivered_by, "delivered_at": row.delivered_at,
            "applied_by": row.applied_by, "applied_at": row.applied_at,
            "created_at": row.created_at, "updated_at": row.updated_at,
        }


def uc_object_kind_resolver(ws: Any) -> uc.ObjectKindResolver:
    """Resolve 3-part UC names to table / volume / function / model via the SDK (cached; defaults to table)."""
    cache: Dict[str, Optional[str]] = {}

    def resolve(name: str) -> Optional[str]:
        if name in cache:
            return cache[name]
        kind: Optional[str] = None
        for k, fn in ((uc.KIND_TABLE, lambda: ws.tables.get(name)),
                      (uc.KIND_VOLUME, lambda: ws.volumes.read(name)),
                      (uc.KIND_FUNCTION, lambda: ws.functions.get(name)),
                      (uc.KIND_MODEL, lambda: ws.registered_models.get(name))):
            try:
                fn()
                kind = k
                break
            except Exception:
                continue
        cache[name] = kind
        return kind
    return resolve

