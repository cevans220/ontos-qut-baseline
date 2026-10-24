"""Product Offers: a Data Product's terms of use bind every Agreement concluded for it (Phase 3)."""
import uuid

import pytest
from sqlalchemy.orm import Session

from src.controller.access_grants_manager import AccessGrantsManager
from src.controller.access_policies_manager import (
    KIND_POLICY,
    AccessPoliciesManager,
    AccessPolicyError,
)
from src.controller.access_policy_releases_manager import PolicyReleaseManager
from src.db_models.access_grants import AccessGrantRequestDb
from src.db_models.access_policies import AccessPolicyDb
from src.db_models.data_products import DataProductDb, OutputPortDb
from src.models.access_grants import AccessGrantHandlePayload
from src.models.access_policies import AccessPolicyCreate

OWNER = "owner@example.com"
APPROVER = "approver@example.com"


@pytest.fixture
def policies():
    return AccessPoliciesManager(attribute_sources=[])


@pytest.fixture
def product(db_session: Session):
    dp = DataProductDb(id=str(uuid.uuid4()), name="Sales", version="1.0.0", status="active")
    db_session.add(dp)
    db_session.flush()
    db_session.add(OutputPortDb(product_id=dp.id, name="orders", version="1.0.0", asset_type="table",
                                asset_identifier="main.sales.orders", status="active"))
    db_session.flush()
    return dp


def _offer(policies, db, product, status="active"):
    o = policies.create(db, KIND_POLICY, AccessPolicyCreate(
        name="Sales terms", policy_type="Offer", subject_entity_type="data_product", subject_entity_id=product.id,
        rules={"permission": [{"target": "uc:main.sales", "action": "read", "assigner": f"user:{OWNER}"}],
               "prohibition": [{"target": "uc:main.sales", "action": "distribute", "assigner": f"user:{OWNER}"}]}), OWNER)
    if status == "active":
        policies.change_status(db, KIND_POLICY, o.id, "under_review", OWNER)
        policies.change_status(db, KIND_POLICY, o.id, "approved", APPROVER)
        o = policies.change_status(db, KIND_POLICY, o.id, "active", OWNER)
    return o


def _approve(db, product, email="ana@example.com"):
    req = AccessGrantRequestDb(requester_email=email, entity_type="data_product", entity_id=product.id,
                               entity_name=product.name, requested_duration_days=30, permission_level="READ",
                               status="pending")
    db.add(req)
    db.flush()
    _, grant = AccessGrantsManager().handle_request(db, APPROVER, AccessGrantHandlePayload(request_id=req.id, approved=True))
    return db.query(AccessPolicyDb).filter(AccessPolicyDb.access_grant_id == str(grant.id)).one()


def test_offer_is_scoped_to_a_product_and_listed(policies, product, db_session: Session):
    offer = _offer(policies, db_session, product)
    d = policies.to_read_dict(db_session, KIND_POLICY, offer)
    assert (d["subject_entity_type"], d["subject_name"]) == ("data_product", "Sales")
    assert [o.id for o in policies.offers_for(db_session, "data_product", product.id)] == [offer.id]


def test_subject_must_exist(policies, db_session: Session):
    with pytest.raises(AccessPolicyError):
        policies.create(db_session, KIND_POLICY, AccessPolicyCreate(
            name="x", policy_type="Offer", subject_entity_type="data_product", subject_entity_id="nope"), OWNER)
    with pytest.raises(AccessPolicyError):
        policies.create(db_session, KIND_POLICY, AccessPolicyCreate(
            name="x", policy_type="Offer", subject_entity_type="team", subject_entity_id="t"), OWNER)


def test_agreement_snapshots_the_offer_terms_bound_to_the_grantee(policies, product, db_session: Session):
    offer = _offer(policies, db_session, product)
    agreement = _approve(db_session, product)
    assert agreement.inherit_from == [] and "Sales terms" in agreement.description
    assert [(r["action"], r["assignee"]) for r in agreement.body["prohibition"]] == [("distribute", "user:ana@example.com")]
    assert [r["target"] for r in agreement.body["permission"]] == ["uc:main.sales.orders"]
    # Retiring the Offer later doesn't rewrite or break the concluded Agreement.
    policies.change_status(db_session, KIND_POLICY, offer.id, "deprecated", OWNER)
    compiled = PolicyReleaseManager(policies).compile(db_session, "uc")
    assert not compiled["has_errors"]
    assert {(g["principal"], g["privilege"]) for g in compiled["plan"]["grants"] if g["privilege"] == "SELECT"} == {
        ("ana@example.com", "SELECT")}


def test_the_offer_itself_entitles_nobody(policies, product, db_session: Session):
    _offer(policies, db_session, product)
    compiled = PolicyReleaseManager(policies).compile(db_session, "uc")
    assert compiled["plan"]["grants"] == []
    assert [g["gate"] for g in compiled["plan"]["gated"]] == ["workflow"]


def test_draft_offers_are_not_applied(policies, product, db_session: Session):
    _offer(policies, db_session, product, status="draft")
    assert _approve(db_session, product).body.get("prohibition") == []
