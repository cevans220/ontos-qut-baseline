"""Unit tests for the Authority Resolution feature (ARF).

Covers the pure pieces — the criteria-driven DNA-Coefficient engine and the
deterministic runtime gate — plus the DB-backed configurable-criteria path
(inline Compliance Checks driving both the gate and divergence) and the shared
Maturity Level integration.
"""
import pytest

from src.common.authority_resolution_dna import (
    Criterion,
    compute_dnaco,
    evaluate_row,
    maturity_level,
    DIR_ACTUAL_EXCEEDS,
    DIR_DOCUMENTED_EXCEEDS,
    DIR_NEUTRAL,
    DIR_NONE,
)
from src.controller.authority_resolution_manager import (
    AuthorityResolutionManager,
    VERDICT_APPROVED,
    VERDICT_DENIED,
)
from src.db_models.authority_resolution import AuthorityRelationDb, AuthorityCriterionDb
from src.db_models.compliance import CompliancePolicyDb


# --------------------------------------------------------------- helpers

def _crit(rule, *, direction=DIR_NEUTRAL, weight=1.0, dimension="people", message=None,
          order=0, enabled=True, name="crit") -> Criterion:
    """A pure engine Criterion (rule already includes ASSERT)."""
    return Criterion(code=name, rule=rule, direction=direction, weight=weight,
                     dimension=dimension, message=message)


# canonical criteria mirroring the trade-promotion worked example
ALLOWLIST = _crit("ASSERT obj.actor_identity IN ['rsm-east']",
                  direction=DIR_ACTUAL_EXCEEDS, name="approver_mismatch")
THRESHOLD = _crit("ASSERT obj.value <= 0.15 OR obj.escalated = True",
                  direction=DIR_ACTUAL_EXCEEDS, name="threshold_breach_without_escalation")
COSIGN = _crit("ASSERT obj.cosign_present = True",
               direction=DIR_DOCUMENTED_EXCEEDS, name="missing_cosign")


# --------------------------------------------------------------- DNAco engine

def test_clean_evidence_scores_zero():
    rows = [
        {"actor_identity": "rsm-east", "value": 0.10, "escalated": False},
        {"actor_identity": "rsm-east", "value": 0.12, "escalated": False},
    ]
    result = compute_dnaco([ALLOWLIST, THRESHOLD], rows)
    assert result.magnitude == 0.0
    assert result.direction == DIR_NONE
    assert result.divergent_count == 0


def test_threshold_breach_without_escalation_is_actual_exceeds():
    rows = [
        {"actor_identity": "rsm-east", "value": 0.22, "escalated": False},   # breach
        {"actor_identity": "rsm-east", "value": 0.10, "escalated": False},   # clean
    ]
    result = compute_dnaco([THRESHOLD], rows)
    assert result.divergent_count == 1
    assert result.magnitude == 0.5
    assert result.direction == DIR_ACTUAL_EXCEEDS


def test_escalated_breach_is_clean():
    rows = [{"actor_identity": "rsm-east", "value": 0.22, "escalated": True}]
    result = compute_dnaco([THRESHOLD], rows)
    assert result.divergent_count == 0
    assert result.magnitude == 0.0


def test_missing_cosign_is_documented_exceeds():
    rows = [
        {"actor_identity": "rsm-east", "value": 0.10, "cosign_present": False},  # missing co-sign
        {"actor_identity": "rsm-east", "value": 0.10, "cosign_present": True},   # clean
    ]
    result = compute_dnaco([COSIGN], rows)
    assert result.divergent_count == 1
    assert result.direction == DIR_DOCUMENTED_EXCEEDS


def test_approver_mismatch_flags_divergence():
    rd = evaluate_row([ALLOWLIST], {"actor_identity": "someone-else"})
    assert rd.diverged
    assert "approver_mismatch" in rd.reasons


def test_criterion_weights_raise_magnitude():
    rows = [
        {"actor_identity": "rsm-east", "value": 0.22, "escalated": False},
        {"actor_identity": "rsm-east", "value": 0.10, "escalated": False},
        {"actor_identity": "rsm-east", "value": 0.10, "escalated": False},
        {"actor_identity": "rsm-east", "value": 0.10, "escalated": False},
    ]
    # Default weight: 1 of 4 diverges -> 0.25
    assert compute_dnaco([THRESHOLD], rows).magnitude == 0.25
    # Weighted: the breach counts triple, capped at 1.0 -> 0.75
    weighted = _crit(THRESHOLD.rule, direction=DIR_ACTUAL_EXCEEDS, weight=3.0, name="threshold")
    assert compute_dnaco([weighted], rows).magnitude == 0.75


def test_percent_string_and_bool_string_coerce():
    rows = [{"actor_identity": "rsm-east", "value": "22%", "escalated": "false"}]
    result = compute_dnaco([THRESHOLD], rows)
    assert result.divergent_count == 1


def test_no_criteria_scores_zero():
    rows = [{"actor_identity": "whoever", "value": 9.9, "escalated": False}]
    result = compute_dnaco([], rows)
    assert result.magnitude == 0.0
    assert result.divergent_count == 0


def test_dimensions_combine_by_probabilistic_union():
    people = _crit("ASSERT obj.actor_identity IN ['ok']", dimension="people", name="ppl")
    policy = _crit("ASSERT obj.value <= 10", dimension="policy", name="pol")
    rows = [
        {"actor_identity": "bad", "value": 5},    # people fails only
        {"actor_identity": "ok", "value": 50},    # policy fails only
    ]
    r = compute_dnaco([people, policy], rows)
    assert r.dimensions["people"] == 0.5      # 1 of 2 rows
    assert r.dimensions["policy"] == 0.5      # 1 of 2 rows
    # Noisy-OR: 1 - (1-0.5)(1-0.5) = 0.75 (scales into [0,1], not a capped sum).
    assert r.magnitude == 0.75


def test_combine_dimensions_properties():
    from src.common.authority_resolution_dna import combine_dimensions
    assert combine_dimensions([]) == 0.0
    assert combine_dimensions([0.0, 0.0]) == 0.0
    assert combine_dimensions([1.0, 0.3]) == 1.0        # a maxed dimension forces 1.0 (no dilution)
    assert combine_dimensions([0.5, 0.0]) == 0.5        # clean dimension leaves it unchanged
    assert combine_dimensions([0.225, 0.375, 0.0]) == 0.5156  # worked example


# ---------------------------------------------------- deterministic gate

def _relation(*criteria: AuthorityCriterionDb) -> AuthorityRelationDb:
    return AuthorityRelationDb(id="ar-1", name="Test AR", status="active", version=1,
                              criteria=list(criteria))


def _link(rule, *, direction=DIR_NEUTRAL, weight=1.0, message=None, order=0,
          enabled=True, name="crit") -> AuthorityCriterionDb:
    return AuthorityCriterionDb(
        id=f"crit-{order}", relation_id="ar-1",
        compliance_policy=CompliancePolicyDb(
            id=f"pol-{order}", name=name, rule=rule, failure_message=message, is_active=True,
        ),
        direction=direction, weight=weight, display_order=order, enabled=enabled,
    )


def test_gate_approves_when_all_criteria_pass():
    mgr = AuthorityResolutionManager()
    rel = _relation(
        _link("ASSERT obj.actor_identity IN ['rsm-east']", order=0),
        _link("ASSERT obj.value <= 0.15 OR obj.escalated = True", order=1),
    )
    verdict, _ = mgr._evaluate_decision(rel, {"actor_identity": "rsm-east", "value": 0.10})
    assert verdict == VERDICT_APPROVED


def test_gate_denies_unauthorized_signer_with_message():
    mgr = AuthorityResolutionManager()
    rel = _relation(_link("ASSERT obj.actor_identity IN ['rsm-east']",
                          message="Signer is not an authorized RSM.", order=0))
    verdict, reason = mgr._evaluate_decision(rel, {"actor_identity": "intruder"})
    assert verdict == VERDICT_DENIED
    assert reason == "Signer is not an authorized RSM."


def test_gate_denies_over_threshold_without_escalation():
    mgr = AuthorityResolutionManager()
    rel = _relation(_link("ASSERT obj.value <= 0.15 OR obj.escalated = True", order=0))
    verdict, _ = mgr._evaluate_decision(rel, {"value": 0.30, "escalated": False})
    assert verdict == VERDICT_DENIED


def test_gate_allows_over_threshold_with_escalation():
    mgr = AuthorityResolutionManager()
    rel = _relation(_link("ASSERT obj.value <= 0.15 OR obj.escalated = True", order=0))
    verdict, _ = mgr._evaluate_decision(rel, {"value": 0.30, "escalated": True})
    assert verdict == VERDICT_APPROVED


def test_gate_denies_missing_cosign():
    mgr = AuthorityResolutionManager()
    rel = _relation(_link("ASSERT obj.cosign_present = True", order=0))
    verdict, _ = mgr._evaluate_decision(rel, {"cosign_present": False})
    assert verdict == VERDICT_DENIED


def test_gate_denies_action_mismatch():
    mgr = AuthorityResolutionManager()
    rel = _relation(_link("ASSERT obj.action = 'approve'", order=0))
    verdict, _ = mgr._evaluate_decision(rel, {"action": "override"})
    assert verdict == VERDICT_DENIED


def test_gate_disabled_criterion_is_skipped():
    mgr = AuthorityResolutionManager()
    rel = _relation(_link("ASSERT obj.actor_identity IN ['rsm-east']", enabled=False, order=0))
    verdict, _ = mgr._evaluate_decision(rel, {"actor_identity": "intruder"})
    assert verdict == VERDICT_APPROVED


def test_gate_no_criteria_approves():
    mgr = AuthorityResolutionManager()
    verdict, _ = mgr._evaluate_decision(_relation(), {"actor_identity": "anyone"})
    assert verdict == VERDICT_APPROVED


# ------------------------------------------------------------- maturity

def test_maturity_levels():
    assert maturity_level(has_object=False, dna_measured=False, fully_affirmed=False) == "L1"
    assert maturity_level(has_object=True, dna_measured=False, fully_affirmed=False) == "L2"
    assert maturity_level(has_object=True, dna_measured=True, fully_affirmed=False) == "L3"
    assert maturity_level(has_object=True, dna_measured=True, fully_affirmed=True) == "L4"


# ---------------------------------- configurable criteria (DB-backed, end to end)

def test_configurable_criteria_gate_and_divergence(db_session):
    """Inline-authored criteria create Compliance Checks, drive the runtime gate,
    and produce signed/weighted DNAco divergence — one definition, both engines."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import AuthorityRelationCreate, CriterionInput

    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(
        name="Trade promo approval",
        criteria=[
            CriterionInput(name="Authorized RSM", rule="obj.actor_identity IN ['rsm-east']",
                           failure_message="Signer is not an authorized RSM.",
                           direction=DIR_ACTUAL_EXCEEDS, weight=1.0, order=0),
            CriterionInput(name="Within discount cap",
                           rule="obj.value <= 0.15 OR obj.escalated = True",
                           failure_message="Discount exceeds 15% without escalation.",
                           direction=DIR_ACTUAL_EXCEEDS, weight=1.5, order=1),
        ],
    ))
    db_session.commit()

    # Inline criteria created Compliance Checks (category tagged), linked to the AR.
    assert len(rel.criteria) == 2
    cats = {c.compliance_policy.category for c in rel.criteria}
    assert cats == {"Authority Decision"}
    # The ASSERT keyword is prepended when the author omits it.
    assert all(c.compliance_policy.rule.upper().startswith("ASSERT") for c in rel.criteria)

    # Gate (dry-run, any status): good request approved, bad request denied w/ message.
    ok = mgr.resolve(db_session, rel.id, {"actor_identity": "rsm-east", "value": 0.10},
                     record=False, require_active=False)
    assert ok["verdict"] == VERDICT_APPROVED
    bad = mgr.resolve(db_session, rel.id, {"actor_identity": "intruder", "value": 0.10},
                      record=False, require_active=False)
    assert bad["verdict"] == VERDICT_DENIED
    assert bad["reason"] == "Signer is not an authorized RSM."

    # Divergence over an evidence sample: 1 of 2 rows breaches the cap (weight 1.5).
    run = mgr.compute_dnaco(db_session, rel.id, rows=[
        {"actor_identity": "rsm-east", "value": 0.22, "escalated": False},   # over cap
        {"actor_identity": "rsm-east", "value": 0.10, "escalated": False},   # clean
    ])
    db_session.commit()
    assert run.divergent_count == 1
    assert run.direction == DIR_ACTUAL_EXCEEDS
    # people dimension = 1.5 / 2 rows; structural also scored; overall = noisy-OR.
    assert run.per_dimension_scores["people"] == 0.75
    assert "structural" in run.per_dimension_scores
    from src.common.authority_resolution_dna import combine_dimensions
    assert run.magnitude == combine_dimensions(run.per_dimension_scores.values())


def test_structural_dimension_scores_incompleteness(db_session):
    """The built-in structural dimension scores AR-definition completeness: a
    relation complete except for its justification-chain scores 1/5 = 0.2."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import (
        AuthorityRelationCreate, EvidenceSource, CriterionInput, AffirmationInput,
    )
    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(
        name="Structural AR", object_id="dp-9",
        evidence_sources=[EvidenceSource(type="delta_table", ref="c.s.t", column_map={"actor_identity": "a"})],
        criteria=[CriterionInput(name="Authorized", rule="obj.actor_identity IN ['x']", dimension="people")],
        affirmations=[AffirmationInput(role="Governance Lead", principal="g@x.com", is_approver=True, is_reviewer=False)],
    ))
    db_session.commit()
    run = mgr.compute_dnaco(db_session, rel.id, rows=[])   # empty evidence sample
    db_session.commit()
    assert run.per_dimension_scores["structural"] == 0.2   # only justification_chain missing
    assert run.per_dimension_scores["people"] == 0.0       # no divergent rows
    assert run.magnitude == 0.2


def test_update_does_not_bump_version(db_session):
    """Edits mutate the current row in place — no auto version bump (DP/DC parity)."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import AuthorityRelationCreate, AuthorityRelationUpdate

    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(name="Editable AR"))
    db_session.commit()
    assert rel.version == "1.0.0"
    assert rel.version_family_id == rel.id
    assert rel.base_name == "Editable AR"

    updated = mgr.update_relation(db_session, rel.id, AuthorityRelationUpdate(description="tweaked"))
    db_session.commit()
    assert updated.version == "1.0.0"          # unchanged
    assert updated.description == "tweaked"


def test_create_new_version_snapshots_definition(db_session):
    """New Version deep-clones the definition (criteria + participants + evidence)
    into a fresh draft row in the same family, resetting measured/observed state."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import (
        AuthorityRelationCreate, CriterionInput, AffirmationInput, EvidenceSource,
    )
    mgr = AuthorityResolutionManager()
    src = mgr.create_relation(db_session, AuthorityRelationCreate(
        name="Versioned AR", object_id="dp-1",
        evidence_sources=[EvidenceSource(type="delta_table", ref="c.s.t", column_map={"actor_identity": "a"})],
        criteria=[CriterionInput(name="Authorized", rule="obj.actor_identity IN ['rsm-east']",
                                 direction=DIR_ACTUAL_EXCEEDS, weight=1.5, dimension="people")],
        affirmations=[AffirmationInput(role="Governance Lead", principal="g@x.com",
                                       is_approver=True, is_reviewer=True)],
    ))
    db_session.commit()
    # Simulate the source having been measured/used.
    src.dna_magnitude = 0.6
    src.usage_count = 5
    db_session.commit()

    v2 = mgr.create_new_version(db_session, src.id, "2.0.0", change_summary="raised the cap",
                                current_user="lars@x.com")
    db_session.commit()

    assert v2 is not None and v2.id != src.id
    assert v2.version == "2.0.0"
    assert v2.status == "draft"
    assert v2.version_family_id == src.version_family_id      # same family
    assert v2.parent_relation_id == src.id                    # lineage edge
    assert v2.change_summary == "raised the cap"
    assert v2.base_name == "Versioned AR"
    # Definition copied over.
    assert v2.object_id == "dp-1"
    assert v2.evidence_sources == src.evidence_sources
    # Measured/observed state reset on the new draft.
    assert v2.dna_magnitude is None
    assert v2.usage_count == 0

    # Criteria + affirmations were cloned onto the new row.
    v2_crit = mgr._load_criteria(v2)
    assert len(v2_crit) == 1
    v2_dict = mgr.to_read_dict(db_session, v2)
    assert len(v2_dict["affirmations"]) == 1
    assert v2_dict["affirmations"][0]["affirmed"] is False    # reset

    # Both versions live in the family, newest first.
    fam = mgr.get_relation_versions(db_session, src.id)
    assert {r.version for r in fam} == {"1.0.0", "2.0.0"}
    assert fam[0].id == v2.id                                  # newest first


def test_slug_resolves_to_family_active_version(db_session):
    """A slug is the family-level @id: it resolves to the family's active version
    (even though the slug physically lives on the original row), while a UUID pins
    the exact version."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager, STATUS_ACTIVE
    from src.models.authority_resolution import AuthorityRelationCreate

    mgr = AuthorityResolutionManager()
    v1 = mgr.create_relation(db_session, AuthorityRelationCreate(name="Promo rule", slug="ar-promo"))
    db_session.commit()
    v2 = mgr.create_new_version(db_session, v1.id, "2.0.0")
    db_session.commit()
    # v2 is a fresh draft with no slug of its own; activate it.
    v2.status = STATUS_ACTIVE
    db_session.commit()

    # By slug -> the family's active version (v2), not the row that holds the slug (v1).
    assert v2.slug is None
    resolved = mgr.resolve_relation_ref(db_session, "ar-promo")
    assert resolved is not None and resolved.id == v2.id
    # By UUID -> the exact version.
    assert mgr.resolve_relation_ref(db_session, v1.id).id == v1.id
    # No active version -> falls back to the anchor row that carries the slug.
    v2.status = "draft"
    db_session.commit()
    assert mgr.resolve_relation_ref(db_session, "ar-promo").id == v1.id


def test_list_collapses_by_family(db_session):
    """The default list returns one representative per family with a version_count;
    include_history returns every version."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import AuthorityRelationCreate

    mgr = AuthorityResolutionManager()
    a = mgr.create_relation(db_session, AuthorityRelationCreate(name="Family A"))
    mgr.create_relation(db_session, AuthorityRelationCreate(name="Family B"))
    db_session.commit()
    mgr.create_new_version(db_session, a.id, "2.0.0")
    db_session.commit()

    collapsed = mgr.list_relations(db_session)
    fam_a = [r for r in collapsed if (r.version_family_id or r.id) == a.id]
    assert len(fam_a) == 1                                     # A collapsed to one rep
    assert getattr(fam_a[0], "_version_count", None) == 2      # count surfaced

    full = mgr.list_relations(db_session, include_history=True)
    fam_a_full = [r for r in full if (r.version_family_id or r.id) == a.id]
    assert len(fam_a_full) == 2                                # both A versions


def test_authority_maturity_feature_integration(db_session):
    """The ARF ladder is seeded as entity_type-specific levels, overrides the
    generic "all" set, and the compliance-gated evaluator advances an AR through
    the levels as it gains an object / evidence+DNA / affirmation."""
    from src.repositories.maturity_repository import maturity_repo
    from src.controller.maturity_evaluator import MaturityEvaluator
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import (
        AuthorityRelationCreate, EvidenceSource,
    )

    # Seed the generic "all" model AND the ARF-specific ladder.
    maturity_repo.seed_defaults(db_session)
    maturity_repo.seed_authority_defaults(db_session)
    db_session.commit()

    ar_levels = maturity_repo.get_all_ordered(db_session, entity_type="AuthorityRelation")
    # Specific overrides generic: only the 4 ARF levels, none of the "all" set.
    assert [l.name for l in ar_levels] == [
        "Documented", "Object-Resolved", "DNA-Measured", "N-Functionally-Affirmed",
    ]
    assert all(l.entity_type == "AuthorityRelation" for l in ar_levels)

    mgr = AuthorityResolutionManager()
    evaluator = MaturityEvaluator()

    # (1) name only -> L1 Documented (no object stops the ladder).
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(name="AR"))
    db_session.commit()
    rep = evaluator.evaluate(db_session, entity_type="AuthorityRelation", entity_id=rel.id, persist=False)
    assert rep.achieved_level_order == 1

    # (2) add an object -> L2 Object-Resolved (no evidence/DNA stops it there).
    rel2 = mgr.create_relation(db_session, AuthorityRelationCreate(
        name="AR2", object_id="dp-1",
        evidence_sources=[EvidenceSource(type="delta_table", ref="c.s.t", column_map={"actor_identity": "a"})],
    ))
    db_session.commit()
    mgr.compute_dnaco(db_session, rel2.id, rows=[])  # measured (empty sample -> magnitude 0.0)
    db_session.commit()
    rep2 = evaluator.evaluate(db_session, entity_type="AuthorityRelation", entity_id=rel2.id, persist=False)
    # object + evidence + measured + within ceiling, but not affirmed -> L3 DNA-Measured
    assert rep2.achieved_level_order == 3


# ---------------------------------------------------------------------------
# MCP tool: ComputeDnaCoefficientTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_dna_coefficient_tool_by_uuid(db_session):
    """The MCP tool is a thin wrapper over `manager.compute_dnaco`; verify it
    accepts a UUID, returns the persisted run shape, and leaves the AR row's
    cached magnitude updated."""
    from unittest.mock import MagicMock
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import AuthorityRelationCreate, CriterionInput
    from src.tools.authority_resolution import ComputeDnaCoefficientTool
    from src.tools.base import ToolContext

    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(
        name="DNAco-tool AR",
        criteria=[CriterionInput(name="cap", rule="obj.value <= 0.15",
                                 failure_message="over", direction=DIR_ACTUAL_EXCEEDS,
                                 weight=1.0, order=0)],
    ))
    db_session.commit()

    tool = ComputeDnaCoefficientTool()
    ctx = ToolContext(db=db_session, settings=MagicMock())
    # Patch _collect_evidence so the tool doesn't try to read a real Delta table.
    mgr._collect_evidence = lambda _rel: [
        {"actor_identity": "a", "value": 0.22},   # breaches
        {"actor_identity": "a", "value": 0.10},   # clean
    ]
    # The tool references the module-level manager; patch it there too.
    import src.tools.authority_resolution as artools
    artools._manager._collect_evidence = mgr._collect_evidence

    result = await tool.execute(ctx, ar_id=rel.id)
    assert result.success is True, result.error
    assert result.data["relation_id"] == rel.id
    assert result.data["status"] == "succeeded"
    assert result.data["sampled_count"] == 2
    assert result.data["divergent_count"] == 1
    assert result.data["direction"] == DIR_ACTUAL_EXCEEDS
    assert result.data["magnitude"] > 0.0
    assert "people" in (result.data["per_dimension_scores"] or {})


@pytest.mark.asyncio
async def test_compute_dna_coefficient_tool_resolves_slug_and_handles_not_found(db_session):
    """Slug input is resolved to the family's active version (so this works
    stably across version clones), and an unknown ar_id returns a tool error
    rather than crashing."""
    from unittest.mock import MagicMock
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import AuthorityRelationCreate
    from src.tools.authority_resolution import ComputeDnaCoefficientTool
    from src.tools.base import ToolContext
    import src.tools.authority_resolution as artools

    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(
        name="DNAco-tool AR by slug", slug="dnaco-tool-slug",
    ))
    db_session.commit()
    artools._manager._collect_evidence = lambda _rel: []

    tool = ComputeDnaCoefficientTool()
    ctx = ToolContext(db=db_session, settings=MagicMock())

    ok = await tool.execute(ctx, ar_id="dnaco-tool-slug")
    assert ok.success is True, ok.error
    assert ok.data["relation_id"] == rel.id

    missing = await tool.execute(ctx, ar_id="does-not-exist-slug")
    assert missing.success is False
    assert "not found" in (missing.error or "").lower()

    empty = await tool.execute(ctx, ar_id="")
    assert empty.success is False
    assert "required" in (empty.error or "").lower()


# ---------------------------------------------------------------------------
# ARF v2 graph: resource refs + prerequisites (schema + model smoke)
# ---------------------------------------------------------------------------


def test_resource_ref_round_trips_and_enforces_unique_field(db_session):
    """A ref row persists the typed pointer for an AR field, and the DB
    rejects a second row for the same ``(relation_id, field_path)``."""
    import uuid
    from sqlalchemy.exc import IntegrityError
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.db_models.authority_resolution import AuthorityResourceRefDb
    from src.models.authority_resolution import AuthorityRelationCreate

    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(name="Ref-test AR"))
    db_session.commit()

    db_session.add(AuthorityResourceRefDb(
        id=str(uuid.uuid4()), relation_id=rel.id,
        field_path="actor_identity", ref_type="mcp_tool",
        mcp_service_name="mcp-services/system.ai.github",
        mcp_tool_name="get_user_by_role",
        mcp_params={"role": "{{ actor_role }}"},
    ))
    db_session.commit()

    stored = db_session.query(AuthorityResourceRefDb).filter_by(
        relation_id=rel.id, field_path="actor_identity",
    ).one()
    assert stored.mcp_result_pointer == "$"           # server default landed
    assert stored.mcp_params == {"role": "{{ actor_role }}"}

    db_session.add(AuthorityResourceRefDb(
        id=str(uuid.uuid4()), relation_id=rel.id,
        field_path="actor_identity", ref_type="literal", literal_value="x",
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_resource_ref_cascades_on_relation_delete(db_session):
    """Deleting the parent AR removes its resource_refs via the FK CASCADE."""
    import uuid
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.db_models.authority_resolution import AuthorityResourceRefDb
    from src.models.authority_resolution import AuthorityRelationCreate

    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(name="Cascade-ref AR"))
    db_session.commit()
    rel_id = rel.id

    db_session.add(AuthorityResourceRefDb(
        id=str(uuid.uuid4()), relation_id=rel_id,
        field_path="object_id", ref_type="ontos_entity",
        entity_type="data_product", entity_id="dp-123",
    ))
    db_session.commit()
    assert db_session.query(AuthorityResourceRefDb).filter_by(relation_id=rel_id).count() == 1

    db_session.delete(rel)
    db_session.commit()
    assert db_session.query(AuthorityResourceRefDb).filter_by(relation_id=rel_id).count() == 0


def test_prerequisite_edge_rejects_self_loop(db_session):
    """A self-referential prerequisite is a cycle by definition; the DB
    check-constraint rejects it at write time so the resolve-time cycle
    detector never has to fire for the trivial case."""
    import uuid
    from sqlalchemy.exc import IntegrityError
    from src.db_models.authority_resolution import AuthorityPrerequisiteDb

    db_session.add(AuthorityPrerequisiteDb(
        id=str(uuid.uuid4()),
        relation_family_slug="trade-promotion",
        prerequisite_family_slug="trade-promotion",
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_prerequisite_edge_round_trips(db_session):
    """Two ARs in distinct families can be wired as an A→B prerequisite,
    retrieved by downstream family slug (``list_for_family``), and by
    upstream family slug (``list_dependents`` — used by the write-time
    cycle check)."""
    import uuid
    from src.db_models.authority_resolution import AuthorityPrerequisiteDb
    from src.repositories.authority_resolution_repository import authority_prerequisite_repo

    db_session.add(AuthorityPrerequisiteDb(
        id=str(uuid.uuid4()),
        relation_family_slug="trade-promotion",
        prerequisite_family_slug="market-compliance",
        display_order=0,
    ))
    db_session.add(AuthorityPrerequisiteDb(
        id=str(uuid.uuid4()),
        relation_family_slug="trade-promotion",
        prerequisite_family_slug="budget-delegation",
        display_order=1,
    ))
    db_session.commit()

    deps = authority_prerequisite_repo.list_for_family(
        db_session, relation_family_slug="trade-promotion",
    )
    assert [e.prerequisite_family_slug for e in deps] == [
        "market-compliance", "budget-delegation",
    ]

    rev = authority_prerequisite_repo.list_dependents(
        db_session, prerequisite_family_slug="market-compliance",
    )
    assert [e.relation_family_slug for e in rev] == ["trade-promotion"]


def test_resolver_auth_defaults_to_obo(db_session):
    """New ARs default to OBO; the author pins ``app_sp`` when stability
    matters more than per-caller context."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import AuthorityRelationCreate

    mgr = AuthorityResolutionManager()
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(name="Auth-flag AR"))
    db_session.commit()
    assert rel.resolver_auth == "obo"


# ---------------------------------------------------------------------------
# ARF v2: prerequisite chain evaluator
# ---------------------------------------------------------------------------


def _make_ar(mgr, db, *, slug, name, criteria=None, prerequisites=None):
    """Helper for prereq tests — slug is mandatory because edges are keyed by it."""
    from src.models.authority_resolution import (
        AuthorityRelationCreate, CriterionInput, PrerequisiteInput,
    )
    return mgr.create_relation(db, AuthorityRelationCreate(
        name=name, slug=slug,
        criteria=[CriterionInput(**c) for c in (criteria or [])],
        prerequisites=[PrerequisiteInput(**p) for p in (prerequisites or [])],
    ))


def test_resolve_blocked_by_failing_prerequisite(db_session):
    """A required prerequisite that returns a non-approved verdict short-circuits
    the outer AR to ``denied`` with reason ``blocked by prerequisite ar:<slug>``.
    The full chain is attached to the response so the agent can see which gate
    rejected them, and the decision row's ``provenance`` records the walk."""
    from src.controller.authority_resolution_manager import (
        AuthorityResolutionManager, VERDICT_APPROVED, VERDICT_DENIED,
    )
    from src.repositories.authority_resolution_repository import authority_decision_repo

    mgr = AuthorityResolutionManager()
    # Upstream gate: denies anyone not on the allow-list.
    _make_ar(mgr, db_session, slug="market-compliance", name="Market compliance",
             criteria=[dict(name="allow", rule="obj.actor_identity IN ['approved-rsm']",
                             failure_message="actor not approved for this market")])
    # Downstream: trivially approves on its own, but requires market-compliance.
    _make_ar(mgr, db_session, slug="trade-promo", name="Trade promotion",
             prerequisites=[dict(prerequisite_family_slug="market-compliance")])
    db_session.commit()

    # Signer not on the market allow-list → outer is blocked, verdict = denied,
    # chain carries the upstream failure.
    out = mgr.resolve(db_session, "trade-promo",
                       {"actor_identity": "intruder"},
                       record=True, require_active=False)
    assert out["verdict"] == VERDICT_DENIED
    assert out["reason"].startswith("blocked by prerequisite ar:market-compliance")
    chain = out["prerequisite_chain"]
    assert len(chain) == 1
    assert chain[0]["slug"] == "market-compliance"
    assert chain[0]["verdict"] == VERDICT_DENIED
    assert chain[0]["required"] is True

    # Signer on the allow-list → upstream clears, outer approves.
    out = mgr.resolve(db_session, "trade-promo",
                       {"actor_identity": "approved-rsm"},
                       record=True, require_active=False)
    assert out["verdict"] == VERDICT_APPROVED
    assert out["prerequisite_chain"][0]["verdict"] == VERDICT_APPROVED

    # Provenance landed on the persisted decisions.
    decisions = authority_decision_repo.list_for_relation(
        db_session, relation_id=out["relation_id"],
    )
    # Both runs recorded; the blocked one has the chain, the approved one too.
    assert any(d.provenance and d.provenance.get("chain") for d in decisions)


def test_optional_prerequisite_adds_warning_not_block(db_session):
    """A ``required=False`` prerequisite surfaces in the chain for the agent to
    see, but does not block the outer verdict when it fails."""
    from src.controller.authority_resolution_manager import (
        AuthorityResolutionManager, VERDICT_APPROVED, VERDICT_DENIED,
    )

    mgr = AuthorityResolutionManager()
    _make_ar(mgr, db_session, slug="advisory-check", name="Advisory",
             criteria=[dict(name="allow", rule="obj.actor_identity = 'nobody'",
                             failure_message="advisory failed")])
    _make_ar(mgr, db_session, slug="main-gate", name="Main gate",
             prerequisites=[dict(prerequisite_family_slug="advisory-check", required=False)])
    db_session.commit()

    out = mgr.resolve(db_session, "main-gate",
                       {"actor_identity": "anyone"},
                       record=False, require_active=False)
    assert out["verdict"] == VERDICT_APPROVED
    assert len(out["prerequisite_chain"]) == 1
    entry = out["prerequisite_chain"][0]
    assert entry["slug"] == "advisory-check"
    assert entry["verdict"] == VERDICT_DENIED
    assert entry["required"] is False


def test_prerequisite_slug_resolves_to_active_version(db_session):
    """Edges store the family slug, so a prereq version bump + reactivation
    automatically redirects downstream resolves to the new active version
    without rewriting the edge."""
    from src.controller.authority_resolution_manager import (
        AuthorityResolutionManager, STATUS_ACTIVE, VERDICT_DENIED,
    )

    mgr = AuthorityResolutionManager()
    v1 = _make_ar(mgr, db_session, slug="pre-slug", name="Prereq v1",
                   criteria=[dict(name="v1", rule="obj.value <= 10",
                                   failure_message="over cap (v1)")])
    _make_ar(mgr, db_session, slug="dep-slug", name="Dependent",
             prerequisites=[dict(prerequisite_family_slug="pre-slug")])
    v1.status = STATUS_ACTIVE
    db_session.commit()

    # Baseline: resolves against v1's rule (cap = 10).
    first = mgr.resolve(db_session, "dep-slug", {"value": 20},
                         record=False, require_active=False)
    assert first["prerequisite_chain"][0]["version"] == "1.0.0"
    assert first["verdict"] == VERDICT_DENIED

    # Bump to v2 with a tighter rule and activate it; v1 goes draft (anchor keeps slug).
    v2 = mgr.create_new_version(db_session, v1.id, "2.0.0")
    db_session.commit()
    # Re-author v2's criteria to a stricter cap.
    from src.models.authority_resolution import AuthorityRelationUpdate, CriterionInput
    mgr.update_relation(db_session, v2.id, AuthorityRelationUpdate(
        criteria=[CriterionInput(name="v2", rule="obj.value <= 5",
                                   failure_message="over cap (v2)")],
    ))
    v1.status = "draft"
    v2.status = STATUS_ACTIVE
    db_session.commit()

    second = mgr.resolve(db_session, "dep-slug", {"value": 7},
                          record=False, require_active=False)
    # v2 is active; the chain now reports v2's version string.
    assert second["prerequisite_chain"][0]["version"] == "2.0.0"
    assert second["verdict"] == VERDICT_DENIED
    assert "over cap (v2)" in second["reason"]


def test_cycle_in_prerequisite_chain_returns_conflict(db_session):
    """The write-time check normally rejects cycles, but a hand-crafted edge
    (bypassing the manager) is still caught by the resolve-time cycle guard,
    which returns ``conflict`` on the outer verdict."""
    import uuid as _uuid
    from src.controller.authority_resolution_manager import (
        AuthorityResolutionManager, VERDICT_CONFLICT,
    )
    from src.db_models.authority_resolution import AuthorityPrerequisiteDb

    mgr = AuthorityResolutionManager()
    _make_ar(mgr, db_session, slug="a", name="A")
    _make_ar(mgr, db_session, slug="b", name="B",
             prerequisites=[dict(prerequisite_family_slug="a")])
    # Hand-craft the reverse edge so the write-time check doesn't see it.
    db_session.add(AuthorityPrerequisiteDb(
        id=str(_uuid.uuid4()),
        relation_family_slug="a",
        prerequisite_family_slug="b",
    ))
    db_session.commit()

    out = mgr.resolve(db_session, "b", {"actor_identity": "x"},
                       record=False, require_active=False)
    assert out["verdict"] == VERDICT_CONFLICT
    assert "cycle" in out["reason"].lower()


def test_write_rejects_cycle_in_prerequisites(db_session):
    """``_replace_prerequisites`` rejects an edge whose upstream family's
    transitive prereqs already contain the head slug."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import AuthorityRelationUpdate, PrerequisiteInput

    mgr = AuthorityResolutionManager()
    # A requires B.
    _make_ar(mgr, db_session, slug="x", name="X")
    y = _make_ar(mgr, db_session, slug="y", name="Y",
                  prerequisites=[dict(prerequisite_family_slug="x")])
    db_session.commit()

    # Now try to make X require Y → closes a cycle.
    x = mgr.get_relation_by_slug(db_session, "x")
    with pytest.raises(ValueError, match="cycle"):
        mgr.update_relation(db_session, x.id, AuthorityRelationUpdate(
            prerequisites=[PrerequisiteInput(prerequisite_family_slug="y")],
        ))
    # And attempting to make a family require itself.
    with pytest.raises(ValueError, match="cannot require itself"):
        mgr.update_relation(db_session, y.id, AuthorityRelationUpdate(
            prerequisites=[PrerequisiteInput(prerequisite_family_slug="y")],
        ))


def test_prerequisites_require_a_family_slug(db_session):
    """``_replace_prerequisites`` rejects when the AR has no slug yet —
    edges can't anchor to an identity that doesn't exist. An empty list
    is still accepted (noop)."""
    from src.controller.authority_resolution_manager import AuthorityResolutionManager
    from src.models.authority_resolution import (
        AuthorityRelationCreate, PrerequisiteInput, AuthorityRelationUpdate,
    )

    mgr = AuthorityResolutionManager()
    _make_ar(mgr, db_session, slug="existing", name="Existing")
    rel = mgr.create_relation(db_session, AuthorityRelationCreate(name="Slugless AR"))
    db_session.commit()
    assert rel.slug is None

    with pytest.raises(ValueError, match="slug"):
        mgr.update_relation(db_session, rel.id, AuthorityRelationUpdate(
            prerequisites=[PrerequisiteInput(prerequisite_family_slug="existing")],
        ))
    # Empty prereqs on a slugless AR is a noop (not an error).
    mgr.update_relation(db_session, rel.id, AuthorityRelationUpdate(prerequisites=[]))
    db_session.commit()
