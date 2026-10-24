"""Access Grants → ODRL Agreements → compiled grants / agreement-scoped releases (Phase 3)."""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from src.controller import access_agreements
from src.controller.access_agreements import AgreementService, entity_targets
from src.controller.access_grants_manager import AccessGrantsManager
from src.controller.access_policies_manager import AccessPoliciesManager
from src.controller.access_policy_releases_manager import PolicyReleaseManager
from src.controller.delivery_service import DeliveryMode, DeliveryService
from src.db_models.access_grants import AccessGrantRequestDb
from src.db_models.access_policies import AccessPolicyDb
from src.db_models.data_products import DataProductDb, OutputPortDb
from src.models.access_grants import AccessGrantHandlePayload

ADMIN = "admin@example.com"
REQUESTER = "Ana@Example.com"


class FakeGrants:
    def __init__(self):
        self.calls = []

    def apply_grant(self, data, dry_run=False):
        self.calls.append(("grant", data["principal"], data["securable_name"], tuple(data["privileges"])))
        return {"success": True}

    def apply_revoke(self, data, dry_run=False):
        self.calls.append(("revoke", data["principal"], data["securable_name"], tuple(data["privileges"])))
        return {"success": True}


def _delivery(grants):
    svc = DeliveryService(settings=SimpleNamespace(DELIVERY_DIRECT_DRY_RUN=False), grant_manager=grants)
    svc.get_active_modes = lambda: [DeliveryMode.DIRECT]
    return svc


@pytest.fixture
def product(db_session: Session):
    dp = DataProductDb(id=str(uuid.uuid4()), name="Sales", version="1.0.0", status="active",
                       )
    db_session.add(dp)
    db_session.flush()
    for name, ident, status in (("orders", "main.sales.orders", "active"), ("old", "main.sales.old", "deprecated"),
                                ("none", None, "active")):
        db_session.add(OutputPortDb(product_id=dp.id, name=name, version="1.0.0", asset_type="table",
                                    asset_identifier=ident, status=status))
    db_session.flush()
    return dp


def _approve(db, product, duration=30):
    req = AccessGrantRequestDb(requester_email=REQUESTER, entity_type="data_product", entity_id=product.id,
                               entity_name=product.name, requested_duration_days=duration, permission_level="READ",
                               status="pending")
    db.add(req)
    db.flush()
    return AccessGrantsManager().handle_request(db, ADMIN, AccessGrantHandlePayload(request_id=req.id, approved=True))


def test_entity_targets_use_active_output_ports(product, db_session: Session):
    assert entity_targets(db_session, "data_product", product.id) == ["uc:main.sales.orders"]
    assert entity_targets(db_session, "data_contract", "x") == []


def test_approved_grant_becomes_active_agreement_that_compiles_with_expiry(product, db_session: Session):
    _, grant = _approve(db_session, product)
    agreement = db_session.query(AccessPolicyDb).filter(AccessPolicyDb.access_grant_id == str(grant.id)).one()
    assert agreement.policy_type == "Agreement" and agreement.status == "active"
    assert (agreement.submitted_by, agreement.approved_by) == (REQUESTER, ADMIN)
    rule = agreement.body["permission"][0]
    assert rule["assignee"] == f"user:{REQUESTER}" and rule["assigner"] == f"user:{ADMIN}" and rule["action"] == "read"

    compiled = PolicyReleaseManager(AccessPoliciesManager(attribute_sources=[])).compile(db_session, "uc")
    select = [g for g in compiled["plan"]["grants"] if g["privilege"] == "SELECT"]
    assert [(g["principal"], g["securable_name"]) for g in select] == [("ana@example.com", "main.sales.orders")]
    assert select[0]["expires_at"].startswith(grant.expires_at.date().isoformat())


def test_revoking_the_grant_retires_the_agreement(product, db_session: Session, monkeypatch):
    from src.db_models.access_grants import AccessGrantDb
    # SQLite returns naive datetimes; the response's days_until_expiry would fail on them (Postgres is aware).
    monkeypatch.setattr(AccessGrantDb, "days_until_expiry", property(lambda self: None))
    _, grant = _approve(db_session, product)
    AccessGrantsManager().revoke_grant(db_session, grant.id, ADMIN, "no longer needed")
    agreement = db_session.query(AccessPolicyDb).filter(AccessPolicyDb.access_grant_id == str(grant.id)).one()
    assert agreement.status == "retired"


def test_agreement_scoped_release_ships_only_its_grants_and_revokes_them(product, db_session: Session):
    policies = AccessPoliciesManager(attribute_sources=[])
    _, grant = _approve(db_session, product)
    # An unrelated, unreleased active policy must not ride along.
    other = AccessPolicyDb(name="Other", status="active", policy_type="Set", conflict="prohibit", body=policies._normalize_rules(
        "Set", "prohibit", [], {"permission": [{"target": "uc:hr", "action": "read", "assignee": "group:hr"}]}))
    db_session.add(other)
    db_session.flush()
    agreement = db_session.query(AccessPolicyDb).filter(AccessPolicyDb.access_grant_id == str(grant.id)).one()
    grants = FakeGrants()
    release = AgreementService(policies).deliver(db_session, agreement, ADMIN, _delivery(grants))
    assert release.release_kind == "agreement" and release.status == "applied"
    assert {c[2] for c in grants.calls} == {"main", "main.sales", "main.sales.orders"}

    AgreementService(policies).retire_for_grant(db_session, grant.id, ADMIN, "revoked")
    grants.calls.clear()
    rev = AgreementService(policies).deliver(db_session, agreement, ADMIN, _delivery(grants), revoke=True)
    assert rev.status == "applied" and grants.calls == [("revoke", "ana@example.com", "main.sales.orders", ("SELECT",))]


def test_auto_deliver_is_opt_in(product, db_session: Session, monkeypatch):
    called = []
    monkeypatch.setattr(AgreementService, "deliver", lambda *a, **k: called.append(1))
    access_agreements.auto_deliver(db_session, [object()], ADMIN, revoke=False)
    assert called == []


def test_agreement_failure_never_blocks_approval(product, db_session: Session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(AgreementService, "create_for_grant", boom)
    request, grant = _approve(db_session, product)
    assert grant is not None and request.status.lower() == "approved"
