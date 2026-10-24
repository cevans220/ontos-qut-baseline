"""SHACL shapes for Ontos Access Policies (ODRL 2.2 + the Ontos profile). Pure module.

Generated from the Python profile (:mod:`src.common.odrl`) so the shapes never drift from
what the app accepts; ``data/taxonomies/ontos-odrl-shapes.ttl`` is this output, checked in
(see ``test_odrl_shapes_ttl_in_sync``). The shapes validate policies as RDF — e.g. the
JSON-LD export (``/api/access-policies/policies/{id}/jsonld``) loaded with the ODRL context —
in any SHACL engine (pySHACL, TopBraid, Jena):

* a policy names the Ontos profile, uses a known conflict strategy and has at least one rule;
* every rule has exactly one known action (plain or refined) and, for permissions and
  prohibitions, a target; constraints use known left operands and operators;
* Agreement rules name both parties (ODRL §2.4: an Agreement has an assigner and assignee).
"""
from typing import Iterable, List

from src.common import odrl

SHAPES_IRI = "http://ontos.app/odrl/shapes/v1"


def _term(key: str) -> str:
    """Profile key → Turtle name: ``read`` → ``odrl:read``, ``ontos:browse`` → ``ontos:browse``."""
    return key if key.startswith("ontos:") else f"odrl:{key}"


def _list(items: Iterable[str]) -> str:
    return "( " + " ".join(items) + " )"


def _str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def shapes_turtle() -> str:
    """The shapes graph as Turtle (deterministic: sorted terms)."""
    actions = _list(_term(k) for k in sorted(odrl.ACTIONS))
    operands = _list(_term(k) for k in sorted(odrl.LEFT_OPERANDS))
    operators = _list(f"odrl:{k}" for k in sorted(odrl.OPERATORS))
    conflicts = _list(f"odrl:{k}" for k in odrl.CONFLICT_TERMS)
    logical = [f"[ sh:path odrl:{k} ; sh:minCount 1 ]" for k in odrl.LOGICAL_OPERANDS]
    types = ", ".join(f"odrl:{t}" for t in ("Policy",) + tuple(odrl.POLICY_TYPES))
    rules_or = " ".join(f"[ sh:path odrl:{k} ; sh:minCount 1 ]" for k in odrl.RULE_KINDS)

    out: List[str] = [
        "# Ontos Access Policies — SHACL shapes (v1). Generated from src/common/odrl_shacl.py; keep in sync",
        "# (see test_odrl_shapes_ttl_in_sync). Validates ODRL 2.2 policies against the Ontos profile.",
        "@prefix sh: <http://www.w3.org/ns/shacl#> .",
        f"@prefix odrl: <{odrl.ODRL_NS}> .",
        f"@prefix ontos: <{odrl.ONTOS_NS}> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "",
        f"<{SHAPES_IRI}> a owl:Ontology ;",
        '    rdfs:label "Ontos Access Policies SHACL shapes"@en ;',
        f"    owl:imports <{odrl.ONTOS_PROFILE}> .",
        "",
        "ontos:PolicyShape a sh:NodeShape ;",
        f"    sh:targetClass {types} ;",
        "    sh:nodeKind sh:IRI ;",
        "    sh:property [ sh:path odrl:profile ; sh:minCount 1 ;",
        f"        sh:hasValue <{odrl.ONTOS_PROFILE}> ;",
        f"        sh:message {_str('An Ontos access policy declares the Ontos ODRL profile')} ] ;",
        f"    sh:property [ sh:path odrl:conflict ; sh:maxCount 1 ; sh:in {conflicts} ] ;",
        "    sh:property [ sh:path odrl:inheritFrom ; sh:nodeKind sh:IRI ] ;",
        "    sh:property [ sh:path odrl:permission ; sh:node ontos:PermissionShape ] ;",
        "    sh:property [ sh:path odrl:prohibition ; sh:node ontos:RuleShape ] ;",
        "    sh:property [ sh:path odrl:obligation ; sh:node ontos:DutyShape ] ;",
        f"    sh:or ( {rules_or} ) .",
        "",
        "ontos:ActionShape a sh:NodeShape ;",
        "    rdfs:comment \"A profile action, or a refined action whose rdf:value is one.\"@en ;",
        f"    sh:or ( [ sh:in {actions} ]",
        f"            [ sh:property [ sh:path rdf:value ; sh:minCount 1 ; sh:maxCount 1 ; sh:in {actions} ] ;",
        "              sh:property [ sh:path odrl:refinement ; sh:node ontos:ConstraintShape ] ] ) .",
        "",
        "ontos:RuleShape a sh:NodeShape ;",
        "    sh:property [ sh:path odrl:action ; sh:minCount 1 ; sh:maxCount 1 ; sh:node ontos:ActionShape ] ;",
        f"    sh:property [ sh:path odrl:target ; sh:minCount 1 ;",
        f"        sh:message {_str('A permission or prohibition names its target asset or asset collection')} ] ;",
        "    sh:property [ sh:path odrl:constraint ; sh:node ontos:ConstraintShape ] .",
        "",
        "ontos:PermissionShape a sh:NodeShape ;",
        "    sh:node ontos:RuleShape ;",
        "    sh:property [ sh:path odrl:duty ; sh:node ontos:DutyShape ] .",
        "",
        "ontos:DutyShape a sh:NodeShape ;",
        "    sh:property [ sh:path odrl:action ; sh:minCount 1 ; sh:maxCount 1 ; sh:node ontos:ActionShape ] ;",
        "    sh:property [ sh:path odrl:constraint ; sh:node ontos:ConstraintShape ] .",
        "",
        "ontos:ConstraintShape a sh:NodeShape ;",
        "    sh:xone ( ontos:AtomicConstraintShape ontos:LogicalConstraintShape ) .",
        "",
        "ontos:AtomicConstraintShape a sh:NodeShape ;",
        f"    sh:property [ sh:path odrl:leftOperand ; sh:minCount 1 ; sh:maxCount 1 ; sh:in {operands} ] ;",
        f"    sh:property [ sh:path odrl:operator ; sh:minCount 1 ; sh:maxCount 1 ; sh:in {operators} ] ;",
        "    sh:or ( [ sh:path odrl:rightOperand ; sh:minCount 1 ]",
        "            [ sh:path odrl:rightOperandReference ; sh:minCount 1 ] ) .",
        "",
        "ontos:LogicalConstraintShape a sh:NodeShape ;",
        "    sh:or ( " + "\n            ".join(logical) + " ) .",
        "",
        "ontos:AgreementShape a sh:NodeShape ;",
        "    sh:targetClass odrl:Agreement ;",
        "    sh:property [ sh:path odrl:permission ; sh:node ontos:AgreementRuleShape ] .",
        "",
        "ontos:AgreementRuleShape a sh:NodeShape ;",
        "    sh:property [ sh:path odrl:assigner ; sh:minCount 1 ;",
        f"        sh:message {_str('An Agreement rule names its assigner (the approver)')} ] ;",
        "    sh:property [ sh:path odrl:assignee ; sh:minCount 1 ;",
        f"        sh:message {_str('An Agreement rule names its assignee (the grantee)')} ] .",
        "",
    ]
    return "\n".join(out)
