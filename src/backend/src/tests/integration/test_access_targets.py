"""Access Targets (Phase 4): governed compile + delivery targets, adapter registry, scoped releases."""
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from src.common import policy_targets as pt
from src.controller.access_policies_manager import (
    KIND_POLICY,
    KIND_TARGET,
    AccessPoliciesManager,
    AccessPolicyError,
)
from src.controller.access_policy_releases_manager import PolicyReleaseManager
from src.controller.delivery_service import DeliveryMode, DeliveryService
from src.models.access_policies import AccessPolicyCreate, TargetCreate, TargetUpdate

AUTHOR = "author@example.com"
APPROVER = "approver@example.com"
ADMIN = "admin@example.com"

SALES = {"permission": [{"target": "uc:main.sales.orders", "action": "read", "assignee": "group:analysts"}]}
HR = {"permission": [{"target": "uc:main.hr.people", "action": "read", "assignee": "group:hr"}]}


class FakeGrants:
    def __init__(self):
        self.calls = []

    def apply_grant(self, data, dry_run=False):
        self.calls.append(("grant", data["principal"], data["securable_name"]))
        return {"success": True}

    def apply_revoke(self, data, dry_run=False):
        self.calls.append(("revoke", data["principal"], data["securable_name"]))
        return {"success": True}

    def get_grants(self, securable_type, securable_name):
        return {"success": True, "grants": []}  # nothing pre-existing on the platform


class Delivery(DeliveryService):
    def __init__(self, modes, grants=None):
        super().__init__(settings=SimpleNamespace(DELIVERY_DIRECT_DRY_RUN=False), grant_manager=grants)
        self.modes = modes

    def get_active_modes(self):
        return list(self.modes)


@pytest.fixture
def policies():
    return AccessPoliciesManager(attribute_sources=[])


@pytest.fixture
def rm(policies):
    return PolicyReleaseManager(policies)


def _activate(policies, db, kind, row):
    policies.change_status(db, kind, row.id, "under_review", AUTHOR)
    policies.change_status(db, kind, row.id, "approved", APPROVER)
    return policies.change_status(db, kind, row.id, "active", AUTHOR)


def _policy(policies, db, rules, name):
    return _activate(policies, db, KIND_POLICY, policies.create(db, KIND_POLICY, AccessPolicyCreate(name=name, rules=rules), AUTHOR))


def _target(policies, db, key, scope=(), modes=(), activate=True, **kw):
    row = policies.create(db, KIND_TARGET, TargetCreate(name=key.upper(), key=key, scope=list(scope),
                                                         delivery_modes=list(modes), **kw), AUTHOR)
    return _activate(policies, db, KIND_TARGET, row) if activate else row


def _ship(rm, db, key, delivery):
    r = rm.create_release(db, key, AUTHOR)
    rm.change_status(db, r.id, "under_review", AUTHOR)
    rm.change_status(db, r.id, "approved", APPROVER)
    return rm.deliver(db, r.id, ADMIN, delivery)


def test_builtin_uc_target_until_a_uc_target_is_active(policies, rm, db_session: Session):
    assert [t.key for t in rm.targets(db_session)] == ["uc"]
    _target(policies, db_session, "uc-sales", scope=["main.sales"])
    assert [t.key for t in rm.targets(db_session)] == ["uc-sales"]
    with pytest.raises(AccessPolicyError):
        rm.compile(db_session, "uc")


def test_scope_limits_what_a_target_compiles(policies, rm, db_session: Session):
    _policy(policies, db_session, SALES, "Sales")
    _policy(policies, db_session, HR, "HR")
    _target(policies, db_session, "uc-sales", scope=["main.sales"])
    _target(policies, db_session, "uc-hr", scope=["main.hr"])
    sales = rm.compile(db_session, "uc-sales")
    names = {(g["securable_name"], g["privilege"]) for g in sales["plan"]["grants"]}
    # The implied USE CATALOG on the shared parent comes with the in-scope grant.
    assert names == {("main.sales.orders", "SELECT"), ("main.sales", "USE_SCHEMA"), ("main", "USE_CATALOG")}
    assert any(i["code"] == "outside_scope" for i in sales["issues"])


def test_overlapping_targets_of_one_system_cannot_both_be_active(policies, db_session: Session):
    _target(policies, db_session, "uc-main", scope=["main"])
    clash = _target(policies, db_session, "uc-sales", scope=["main.sales"], activate=False)
    policies.change_status(db_session, KIND_TARGET, clash.id, "under_review", AUTHOR)
    policies.change_status(db_session, KIND_TARGET, clash.id, "approved", APPROVER)
    with pytest.raises(AccessPolicyError) as e:
        policies.change_status(db_session, KIND_TARGET, clash.id, "active", AUTHOR)
    assert "overlaps" in str(e.value)


def test_activation_validates_the_configuration(policies, db_session: Session):
    bad = policies.create(db_session, KIND_TARGET, TargetCreate(
        name="Bad", key="Bad Key!", scope=["a.b.c.d"], delivery_modes=["carrier-pigeon"],
        reconcile_schedule="every day"), AUTHOR)
    problems = policies.activation_problems(db_session, KIND_TARGET, bad)
    assert len(problems) == 4
    with pytest.raises(AccessPolicyError):
        policies.create(db_session, KIND_TARGET, TargetCreate(name="X", key="x1", system="oracle"), AUTHOR)


def test_target_restricts_delivery_modes(policies, rm, db_session: Session):
    _policy(policies, db_session, SALES, "Sales")
    _target(policies, db_session, "uc", modes=["manual"])
    r = rm.create_release(db_session, "uc", AUTHOR)
    rm.change_status(db_session, r.id, "under_review", AUTHOR)
    rm.change_status(db_session, r.id, "approved", APPROVER)
    with pytest.raises(AccessPolicyError):
        rm.deliver(db_session, r.id, ADMIN, Delivery([DeliveryMode.DIRECT, DeliveryMode.MANUAL]), modes=["direct"])
    grants = FakeGrants()
    out = rm.deliver(db_session, r.id, ADMIN, Delivery([DeliveryMode.DIRECT, DeliveryMode.MANUAL], grants))
    assert out.delivery_modes == ["manual"] and grants.calls == []


def test_narrowed_scope_stops_tracking_instead_of_revoking(policies, rm, db_session: Session):
    _policy(policies, db_session, SALES, "Sales")
    _policy(policies, db_session, HR, "HR")
    target = _target(policies, db_session, "uc")
    _ship(rm, db_session, "uc", Delivery([DeliveryMode.DIRECT], FakeGrants()))
    v2 = policies.create_new_version(db_session, KIND_TARGET, target.id, "1.1.0", "sales only", AUTHOR)
    policies.update(db_session, KIND_TARGET, v2.id, TargetUpdate(scope=["main.sales"]), AUTHOR)
    _activate(policies, db_session, KIND_TARGET, v2)
    grants = FakeGrants()
    out = _ship(rm, db_session, "uc", Delivery([DeliveryMode.DIRECT], grants))
    # hr keeps USE CATALOG main: its (now untracked) main.hr.people grant needs it.
    assert out.status == "applied" and grants.calls == []
    assert {g["securable_name"] for g in out.changes["untrack"]} == {"main.hr.people", "main.hr", "main"}
    assert {x["securable_name"] for x in rm.ledger(db_session, "uc")} == {"main", "main.sales", "main.sales.orders"}


def test_first_uc_target_must_adopt_builtin_ledger(policies, rm, db_session: Session):
    _policy(policies, db_session, SALES, "Sales")
    _ship(rm, db_session, "uc", Delivery([DeliveryMode.DIRECT], FakeGrants()))
    other = _target(policies, db_session, "uc-prod", activate=False)
    assert any("orphaned" in p for p in policies.activation_problems(db_session, KIND_TARGET, other))
    adopt = _target(policies, db_session, "uc")
    assert rm.resolve(db_session, "uc").entity_id == adopt.id


def test_target_with_tracked_grants_cannot_be_retired(policies, rm, db_session: Session):
    _policy(policies, db_session, SALES, "Sales")
    target = _target(policies, db_session, "uc")
    _ship(rm, db_session, "uc", Delivery([DeliveryMode.DIRECT], FakeGrants()))
    policies.change_status(db_session, KIND_TARGET, target.id, "deprecated", ADMIN)
    # Deprecated: no new compiles, but tracked grants can still be revoked through it.
    assert rm.resolve(db_session, "uc", include_deprecated=True).entity_id == target.id
    with pytest.raises(AccessPolicyError) as e:
        policies.change_status(db_session, KIND_TARGET, target.id, "retired", ADMIN)
    assert "tracks" in str(e.value)


def test_key_is_fixed_once_versioned(policies, db_session: Session):
    target = _target(policies, db_session, "uc")
    v2 = policies.create_new_version(db_session, KIND_TARGET, target.id, "2.0.0", None, AUTHOR)
    with pytest.raises(AccessPolicyError):
        policies.update(db_session, KIND_TARGET, v2.id, TargetUpdate(key="uc-other"), AUTHOR)
    assert v2.key == "uc" and v2.system == "uc"


def test_scope_helpers():
    assert pt.normalize_scope([" Main.Sales.* ", "main.sales", ""]) == ["main.sales"]
    assert pt.scopes_overlap(["main"], ["main.sales"]) and not pt.scopes_overlap(["main.hr"], ["main.sales"])
    assert pt.scopes_overlap([], ["x"])
    assert not pt.inside("main.salesforce", "main.sales")
