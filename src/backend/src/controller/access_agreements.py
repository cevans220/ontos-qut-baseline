"""Access Grants as ODRL Agreements (Phase 3).

An approved Access Grant is a concrete deal between the approving admin
(``assigner``) and the requester (``assignee``) — an ``odrl:Agreement``. Recording
it as an Access Policy makes it part of the one permission model:

* it is **active** right away — the access-request approval *is* its review
  (``submitted_by`` = requester, ``approved_by`` = approver, separation of duties held);
* its permissions target the entity's UC assets (Data Product → output ports'
  ``asset_identifier``) with action from the permission level and
  ``dateTime lteq expires_at``, so compiled grants carry the expiry and the Grant
  Ledger sweep revokes them on time;
* every normal release includes it; with ``ACCESS_AGREEMENTS_AUTO_DELIVER`` it is
  also shipped immediately as an **agreement-scoped release** (only this
  Agreement's grants — unrelated pending policy changes never ride along);
* revoking or expiring the grant retires the Agreement (and, with auto-deliver,
  ships the matching revocation).

Everything here is best-effort from the Access Grants flow's point of view: a
failure is logged and never blocks approving or revoking a grant.
"""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.common import odrl
from src.common import uc_policy_adapter as uc
from src.common.config import get_settings
from src.common.logging import get_logger
from src.controller.access_policies_manager import (
    KIND_POLICY,
    STATUS_ACTIVE,
    STATUS_RETIRED,
    AccessPoliciesManager,
)
from src.db_models.access_policies import AccessPolicyDb, AccessPolicyReleaseDb

logger = get_logger(__name__)

KIND_AGREEMENT_RELEASE = "agreement"

# Access Grant permission level → profile action.
PERMISSION_ACTIONS = {"READ": "read", "WRITE": "modify", "MANAGE": "ontos:manage"}
_INACTIVE_PORT_STATUSES = {"deprecated", "retired", "inactive"}


def entity_targets(db: Session, entity_type: str, entity_id: str) -> List[str]:
    """UC securable refs an Access Grant on this entity covers (empty if none are known)."""
    if entity_type == "data_product":
        from src.db_models.data_products import OutputPortDb

        ports = db.query(OutputPortDb).filter(OutputPortDb.product_id == entity_id).all()
        refs = []
        for port in ports:
            ident = (port.asset_identifier or "").strip()
            if ident and (port.status or "").lower() not in _INACTIVE_PORT_STATUSES and 1 <= len(ident.split(".")) <= 3:
                ref = odrl.UC_PREFIX + ident
                if ref not in refs:
                    refs.append(ref)
        return refs
    if entity_type in ("asset", "uc_asset", "table", "view", "volume") and 1 <= len(str(entity_id).split(".")) <= 3:
        return [odrl.UC_PREFIX + str(entity_id)]
    return []


class AgreementService:
    def __init__(self, policies: Optional[AccessPoliciesManager] = None):
        self.policies = policies or AccessPoliciesManager()

    # ------------------------------------------------------------ lifecycle

    def for_grant(self, db: Session, grant_id: Any) -> List[AccessPolicyDb]:
        return db.query(AccessPolicyDb).filter(AccessPolicyDb.access_grant_id == str(grant_id)).all()

    def create_for_grant(self, db: Session, grant: Any, *, requested_by: Optional[str],
                         approved_by: Optional[str]) -> AccessPolicyDb:
        """Record an approved Access Grant as an active ``odrl:Agreement``."""
        targets = entity_targets(db, grant.entity_type, grant.entity_id)
        if not targets:
            # Keep the deal on record; the compiler reports the non-UC target and grants nothing.
            targets = [f"urn:ontos:{grant.entity_type}:{grant.entity_id}"]
        action = PERMISSION_ACTIONS.get(str(grant.permission_level).upper(), "read")
        expires = grant.expires_at.isoformat().replace("+00:00", "Z") if grant.expires_at else None
        constraint = [{"leftOperand": "dateTime", "operator": "lteq", "rightOperand": expires}] if expires else []
        rules = {"permission": [
            {"target": t, "action": action, "assignee": f"user:{grant.grantee_email}",
             **({"assigner": f"user:{approved_by}"} if approved_by else {}), "constraint": constraint}
            for t in targets
        ]}
        label = grant.entity_name or grant.entity_id
        row = AccessPolicyDb(
            name=f"Agreement: {grant.grantee_email} → {label}",
            description=f"Access Grant {grant.id}: {grant.permission_level} on {grant.entity_type} '{label}'"
                        + (f" until {expires}" if expires else ""),
            status=STATUS_ACTIVE, policy_type="Agreement", conflict=odrl.DEFAULT_AUTHORED_CONFLICT,
            inherit_from=[], body=self.policies._normalize_rules("Agreement", odrl.DEFAULT_AUTHORED_CONFLICT, [], rules),
            access_grant_id=str(grant.id),
            created_by=approved_by, submitted_by=requested_by, approved_by=approved_by,
        )
        db.add(row)
        db.flush()
        self.policies._rebuild_index(db, row)
        self.policies._log_timeline(db, KIND_POLICY, row.id,
                                    f"Agreement recorded from approved Access Grant {grant.id}", approved_by)
        return row

    def retire_for_grant(self, db: Session, grant_id: Any, user: Optional[str], reason: str) -> List[AccessPolicyDb]:
        """The grant was revoked or expired: retire its Agreement(s) (system transition)."""
        retired = []
        for row in self.for_grant(db, grant_id):
            if row.status == STATUS_RETIRED:
                continue
            row.status, row.updated_by = STATUS_RETIRED, user
            self.policies._log_timeline(db, KIND_POLICY, row.id, f"Agreement retired: {reason}", user)
            retired.append(row)
        db.flush()
        return retired

    # ------------------------------------------------------------ delivery

    def deliver(self, db: Session, agreement: AccessPolicyDb, user: Optional[str], delivery_service: Any, *,
                revoke: bool = False) -> Optional[AccessPolicyReleaseDb]:
        """Ship only this Agreement's grants (or their revocation) as an auto-approved release."""
        from src.controller.access_policy_releases_manager import R_APPROVED, PolicyReleaseManager

        rm = PolicyReleaseManager(self.policies)
        target = uc.TARGET_UC
        compiled = rm.compile(db, target)
        desired = [uc.UCGrant.from_dict(d) for d in compiled["plan"]["grants"]]
        diff = uc.diff_grants(desired, [uc.UCGrant.from_dict(d) for d in self._ledger_dicts(rm, db, target)])

        def mine(g: uc.UCGrant) -> bool:
            return any(s.get("policy_uid") == agreement.uid for s in g.sources or [])

        scoped = uc.GrantDiff(
            add=[] if revoke else [g for g in diff.add if mine(g)],
            remove=[g for g in diff.remove if mine(g)] if revoke else [],
            untrack=[g for g in diff.untrack if mine(g)] if revoke else [],
        )
        if scoped.empty:
            return None
        changes = scoped.to_dict()
        changes["sql"] = uc.render_sql(scoped)
        row = rm._insert(db, AccessPolicyReleaseDb(
            target=target, release_kind=KIND_AGREEMENT_RELEASE, status=R_APPROVED,
            notes=f"{'Revocation of' if revoke else 'Provisioning'} {agreement.name}",
            # Full desired state, so the ledger's provenance refresh stays accurate.
            plan={k: compiled["plan"][k] for k in ("grants", "compiled_at")},
            changes=changes, issues=[], ledger_fingerprint=compiled["ledger_fingerprint"],
            source_policies=[{"id": agreement.id, "uid": agreement.uid, "name": agreement.name,
                              "version": agreement.version}],
            created_by=user, submitted_by=agreement.submitted_by, approved_by=agreement.approved_by,
        ))
        rm._log_timeline(db, row, f"Release #{row.sequence} created for {agreement.name}", user)
        return rm.deliver(db, row.id, user, delivery_service)

    @staticmethod
    def _ledger_dicts(rm: Any, db: Session, target: str) -> List[Dict[str, Any]]:
        return [{"principal": r.principal, "securable_type": r.securable_type, "securable_name": r.securable_name,
                 "privilege": r.privilege, "expires_at": r.expires_at, "implied": r.implied,
                 "sources": r.sources or [], "preexisting": r.preexisting} for r in rm.active_ledger(db, target)]


# ---------------------------------------------------------------- Access Grants hooks

def _delivery_service() -> Any:
    try:
        from src.controller.delivery_service import get_delivery_service
        return get_delivery_service()
    except RuntimeError:
        return None


def on_grant_approved(db: Session, grant: Any, requested_by: Optional[str], approved_by: Optional[str]) -> Optional[AccessPolicyDb]:
    """Called inside the approval transaction. Never raises."""
    try:
        with db.begin_nested():
            return AgreementService().create_for_grant(db, grant, requested_by=requested_by, approved_by=approved_by)
    except Exception as e:
        logger.error(f"Could not record Agreement for Access Grant {getattr(grant, 'id', '?')}: {e}", exc_info=True)
        return None


def on_grant_ended(db: Session, grant_id: Any, user: Optional[str], reason: str) -> List[AccessPolicyDb]:
    """Called inside the revoke / expiry transaction. Never raises."""
    try:
        with db.begin_nested():
            return AgreementService().retire_for_grant(db, grant_id, user, reason)
    except Exception as e:
        logger.error(f"Could not retire Agreement for Access Grant {grant_id}: {e}", exc_info=True)
        return []


def auto_deliver(db: Session, agreements: List[AccessPolicyDb], user: Optional[str], *, revoke: bool) -> None:
    """After the grant transaction committed: ship agreement-scoped releases if enabled. Never raises."""
    if not agreements or not get_settings().ACCESS_AGREEMENTS_AUTO_DELIVER:
        return
    service = _delivery_service()
    if service is None or not service.get_active_modes():
        logger.warning("ACCESS_AGREEMENTS_AUTO_DELIVER is on but no Delivery Mode is available — skipped")
        return
    for agreement in agreements:
        try:
            AgreementService().deliver(db, agreement, user, service, revoke=revoke)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Auto-delivery for {agreement.name} failed: {e}", exc_info=True)
