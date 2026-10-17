"""DB-backed tests for policy releases: compile → review → deliver (Delivery Modes) → Grant Ledger."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from src.controller.access_policies_manager import (
    KIND_POLICY,
    AccessPoliciesManager,
    AccessPolicyError,
)
from src.controller.access_policy_releases_manager import PolicyReleaseManager
from src.controller.delivery_service import DeliveryMode, DeliveryService
from src.models.access_policies import AccessPolicyCreate

AUTHOR = "author@example.com"
APPROVER = "approver@example.com"
ADMIN = "admin@example.com"


class FakeGrants:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def apply_grant(self, data, dry_run=False):
        self.calls.append(("grant", data["principal"], data["securable_name"], tuple(data["privileges"])))
        if data["securable_name"] in self.fail_on:
            return {"success": False, "error": "PERMISSION_DENIED"}
        return {"success": True}

    def apply_revoke(self, data, dry_run=False):
        self.calls.append(("revoke", data["principal"], data["securable_name"], tuple(data["privileges"])))
        return {"success": True}


class FakeGit:
    is_cloned = True

    def __init__(self):
        self.saved = []

    def save_yaml(self, filename, data, subdir=None):
        self.saved.append((subdir, filename, data))
        return True


class Delivery(DeliveryService):
    def __init__(self, modes, grants=None, git=None):
        super().__init__(settings=SimpleNamespace(DELIVERY_DIRECT_DRY_RUN=False), git_service=git, grant_manager=grants)
        self.modes = modes

    def get_active_modes(self):
        return list(self.modes)


@pytest.fixture
def policies():
    return AccessPoliciesManager(attribute_sources=[])


@pytest.fixture
def rm(policies):
    return PolicyReleaseManager(policies)


def _active_policy(policies, db, rules, name="Sales read"):
    p = policies.create(db, KIND_POLICY, AccessPolicyCreate(name=name, rules=rules), AUTHOR)
    policies.change_status(db, KIND_POLICY, p.id, "under_review", AUTHOR)
    policies.change_status(db, KIND_POLICY, p.id, "approved", APPROVER)
    return policies.change_status(db, KIND_POLICY, p.id, "active", AUTHOR)


def _approved_release(rm, db):
    r = rm.create_release(db, "uc", AUTHOR)
    rm.change_status(db, r.id, "under_review", AUTHOR)
    return rm.change_status(db, r.id, "approved", APPROVER)


SALES = {"permission": [{"target": "uc:main.sales.orders", "action": "read", "assignee": "group:analysts"}]}


def test_compile_creates_draft_with_diff_and_runbook(policies, rm, db_session: Session):
    _active_policy(policies, db_session, SALES)
    r = rm.create_release(db_session, "uc", AUTHOR)
    d = rm.to_read_dict(r)
    assert d["status"] == "draft" and d["sequence"] == 1 and d["counts"]["add"] == 3
    assert "GRANT SELECT ON TABLE `main`.`sales`.`orders` TO `analysts`;" in r.changes["sql"]
    assert d["source_policies"][0]["name"] == "Sales read"


def test_separation_of_duties_and_error_gate(policies, rm, db_session: Session):
    _active_policy(policies, db_session, SALES)
    r = rm.create_release(db_session, "uc", AUTHOR)
    rm.change_status(db_session, r.id, "under_review", AUTHOR)
    with pytest.raises(AccessPolicyError) as e:
        rm.change_status(db_session, r.id, "approved", AUTHOR)
    assert e.value.status_code == 403
    assert rm.change_status(db_session, r.id, "approved", APPROVER).approved_by == APPROVER

    _active_policy(policies, db_session, {
        "permission": [{"target": "uc:hr", "action": "read", "assignee": "group:all"}],
        "prohibition": [{"target": "uc:hr.salaries", "action": "read", "assignee": "group:all"}]}, name="Bad")
    bad = rm.create_release(db_session, "uc", AUTHOR)
    with pytest.raises(AccessPolicyError) as e:
        rm.change_status(db_session, bad.id, "under_review", AUTHOR)
    assert "compile errors" in str(e.value)


def test_direct_delivery_applies_and_tracks_in_ledger(policies, rm, db_session: Session):
    _active_policy(policies, db_session, SALES)
    grants = FakeGrants()
    r = rm.deliver(db_session, _approved_release(rm, db_session).id, ADMIN, Delivery([DeliveryMode.DIRECT], grants))
    assert r.status == "applied" and r.applied_by == ADMIN
    assert {c[2] for c in grants.calls} == {"main", "main.sales", "main.sales.orders"}
    ledger = rm.ledger(db_session, "uc")
    assert {(x["securable_name"], x["privilege"]) for x in ledger} == {
        ("main", "USE_CATALOG"), ("main.sales", "USE_SCHEMA"), ("main.sales.orders", "SELECT")}
    # Nothing changed since: the next release is empty and can't be submitted.
    nxt = rm.create_release(db_session, "uc", AUTHOR)
    with pytest.raises(AccessPolicyError):
        rm.change_status(db_session, nxt.id, "under_review", AUTHOR)


def test_indirect_writes_ontos_yaml_and_waits_for_confirmation(policies, rm, db_session: Session):
    _active_policy(policies, db_session, SALES)
    git = FakeGit()
    r = rm.deliver(db_session, _approved_release(rm, db_session).id, ADMIN, Delivery([DeliveryMode.INDIRECT], git=git))
    assert r.status == "delivered" and rm.ledger(db_session, "uc") == []
    subdir, filename, doc = git.saved[0]
    assert subdir == "access-policies/releases" and filename == "uc-release-00001.yaml"
    assert doc["kind"] == "AccessPolicyRelease" and len(doc["spec"]["changes"]["grant"]) == 3 and doc["spec"]["sql"]
    with pytest.raises(AccessPolicyError):
        rm.deliver(db_session, r.id, ADMIN, Delivery([DeliveryMode.INDIRECT], git=git))
    r = rm.confirm_applied(db_session, r.id, ADMIN, "merged PR #42")
    assert r.status == "applied" and len(rm.ledger(db_session, "uc")) == 3


def test_deprecating_policy_revokes_only_managed_grants(policies, rm, db_session: Session):
    p = _active_policy(policies, db_session, SALES)
    grants = FakeGrants()
    rm.deliver(db_session, _approved_release(rm, db_session).id, ADMIN, Delivery([DeliveryMode.DIRECT], grants))
    policies.change_status(db_session, KIND_POLICY, p.id, "deprecated", AUTHOR)
    grants.calls.clear()
    r = rm.deliver(db_session, _approved_release(rm, db_session).id, ADMIN, Delivery([DeliveryMode.DIRECT], grants))
    assert r.status == "applied" and all(c[0] == "revoke" and c[3] for c in grants.calls)
    assert rm.ledger(db_session, "uc") == [] and len(rm.ledger(db_session, "uc", status="revoked")) == 3


def test_partial_direct_failure_then_redeliver_remaining(policies, rm, db_session: Session):
    _active_policy(policies, db_session, SALES)
    r = rm.deliver(db_session, _approved_release(rm, db_session).id, ADMIN,
                   Delivery([DeliveryMode.DIRECT], FakeGrants(fail_on={"main.sales.orders"})))
    assert r.status == "failed" and len(rm.ledger(db_session, "uc")) == 2
    retry = FakeGrants()
    r = rm.deliver(db_session, r.id, ADMIN, Delivery([DeliveryMode.DIRECT], retry))
    assert r.status == "applied" and [c[2] for c in retry.calls] == ["main.sales.orders"]


def test_mode_must_be_enabled_and_stale_release_superseded(policies, rm, db_session: Session):
    _active_policy(policies, db_session, SALES)
    a = _approved_release(rm, db_session)
    with pytest.raises(AccessPolicyError):
        rm.deliver(db_session, a.id, ADMIN, Delivery([DeliveryMode.INDIRECT], git=FakeGit()), modes=["direct"])
    b = rm.create_release(db_session, "uc", AUTHOR)
    rm.deliver(db_session, a.id, ADMIN, Delivery([DeliveryMode.DIRECT], FakeGrants()))
    db_session.refresh(b)
    assert b.status == "superseded"


def test_expired_grants_are_revoked_through_a_revocation_release(policies, rm, db_session: Session):
    _active_policy(policies, db_session, {"permission": [{
        "target": "uc:main.sales", "action": "read", "assignee": "user:temp@example.com",
        "constraint": [{"leftOperand": "dateTime", "operator": "lteq", "rightOperand": "2099-01-01T00:00:00Z"}]}]})
    rm.deliver(db_session, _approved_release(rm, db_session).id, ADMIN, Delivery([DeliveryMode.DIRECT], FakeGrants()))
    assert {x["expires_at"] for x in rm.ledger(db_session, "uc")} == {"2099-01-01T00:00:00Z"}
    assert rm.revoke_expired(db_session, "uc", "system", Delivery([DeliveryMode.DIRECT], FakeGrants())) is None
    later = datetime(2099, 1, 2, tzinfo=timezone.utc)
    grants = FakeGrants()
    rev = rm.revoke_expired(db_session, "uc", "system", Delivery([DeliveryMode.DIRECT], grants), now=later)
    assert rev.release_kind == "revocation" and rev.status == "applied"
    assert {c[0] for c in grants.calls} == {"revoke"} and rm.ledger(db_session, "uc") == []


def test_live_diff_reports_drift(policies, rm, db_session: Session):
    _active_policy(policies, db_session, SALES)
    r = rm.create_release(db_session, "uc", AUTHOR)
    live = {("table", "main.sales.orders"): [{"principal": "analysts", "privileges": ["SELECT"]},
                                             {"principal": "someone", "privileges": ["MODIFY"]}]}
    report = rm.live_diff(db_session, r.id, lambda t, n: {"success": True, "grants": live.get((t, n), [])})
    assert {m["privilege"] for m in report["missing"]} == {"USE_CATALOG", "USE_SCHEMA"}
    assert report["unmanaged"] == [{"principal": "someone", "securable_type": "table",
                                    "securable_name": "main.sales.orders", "privilege": "MODIFY"}]
