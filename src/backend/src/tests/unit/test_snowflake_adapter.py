"""Snowflake target adapter (P4b): roles for principals, ALL + FUTURE grants for containers."""
from src.common import policy_targets as pt
from src.common import snowflake_policy_adapter as sf
from src.common import uc_policy_adapter as uc
from src.common.odrl_compiler import PolicyInput, compile_policies

SF = pt.get_adapter("snowflake")


def _plan(rules, policy_type="Set"):
    doc = {"uid": "urn:ontos:policy:p1", "@type": policy_type, "conflict": "prohibit", **rules}
    return compile_policies([PolicyInput(doc=doc, id="p1", name="P", version="1.0.0")], audiences={},
                            asset_collections={}, directory=lambda: [])


def _keys(grants):
    return {(g.principal, g.securable_type, g.securable_name, g.privilege) for g in grants}


def test_registered_and_owns_only_snowflake_refs():
    assert SF is not None and SF.label == "Snowflake" and pt.CAP_APPLY_GRANTS not in SF.capabilities
    plan = _plan({"permission": [{"target": "snowflake:DB.S.T", "action": "read", "assignee": "group:analysts"},
                                 {"target": "uc:main.s.t", "action": "read", "assignee": "group:analysts"}]})
    SF.own_plan(plan)
    assert [e.securable for e in plan.entitlements] == ["snowflake:db.s.t"]


def test_table_read_for_a_group_uses_the_group_role_and_parent_usage():
    plan = _plan({"permission": [{"target": "snowflake:db.s.t", "action": "read", "assignee": "group:analysts"}]})
    SF.own_plan(plan)
    assert _keys(SF.build(plan, None)) == {("ANALYSTS", "table", "db.s.t", "SELECT"),
                                         ("ANALYSTS", "database", "db", "USAGE"),
                                         ("ANALYSTS", "schema", "db.s", "USAGE")}


def test_user_gets_a_derived_role_and_schema_read_becomes_all_and_future():
    plan = _plan({"permission": [{"target": "snowflake:db.s", "action": "read", "assignee": "user:Jane@x.com"}]})
    SF.own_plan(plan)
    grants = SF.build(plan, None)
    role = sf.derived_role("user:Jane@x.com")
    assert role.startswith("ONTOS_U_") and role == sf.derived_role("user:jane@x.com")
    keys = _keys(grants)
    assert ("jane@x.com", "role", role, "MEMBER") in keys
    assert {(role, f"schema:{p}:{w}", "db.s", "SELECT") for p in ("tables", "views") for w in ("all", "future")} <= keys
    sql = SF.render_sql(uc.diff_grants(grants, []))
    assert sql[0] == f'CREATE ROLE IF NOT EXISTS "{role}";'
    assert f'GRANT SELECT ON FUTURE TABLES IN SCHEMA "DB"."S" TO ROLE "{role}";' in sql
    assert sql[-1] == f'GRANT ROLE "{role}" TO USER "JANE@X.COM";'


def test_revocations_drop_membership_first():
    plan = _plan({"permission": [{"target": "snowflake:db.s.t", "action": "read", "assignee": "user:a@x.com"}]})
    SF.own_plan(plan)
    sql = SF.render_sql(uc.diff_grants([], SF.build(plan, None)))
    assert sql[0].startswith("REVOKE ROLE") and all(s.startswith("REVOKE") for s in sql)


def test_unsupported_actions_are_gated_and_masks_block():
    plan = _plan({"permission": [{"target": "snowflake:db.s.t", "action": "ontos:manage", "assignee": "group:g"}]})
    SF.own_plan(plan)
    assert SF.build(plan, None) == [] and plan.gated
    plan2 = _plan({})
    from src.common.odrl_compiler import AbacRule
    plan2.abac = [AbacRule("column_mask", ["group:g"], ["snowflake:db.s.t"], "pii")]
    SF.build(plan2, None)
    assert plan2.has_errors


def test_identifiers_are_quoted_safely():
    assert sf.ident('db.we"ird') == '"DB"."WE""IRD"'


def test_prohibitions_do_not_cross_platforms():
    plan = _plan({"permission": [{"target": "snowflake:db.s.t", "action": "read", "assignee": "group:g"}],
                  "prohibition": [{"target": "uc:db.s.t", "action": "read", "assignee": "group:g"}]})
    SF.own_plan(plan)
    assert [e.securable for e in plan.entitlements] == ["snowflake:db.s.t"]
