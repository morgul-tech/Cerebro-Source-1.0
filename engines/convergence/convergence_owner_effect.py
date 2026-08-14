#!/usr/bin/env python3
"""Convergence owner consumer using the existing work-family dependency graph."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SOURCE_ROOT = Path(__file__).resolve().parents[2]
MCP_ROOT = SOURCE_ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from control_owner_effect_receipt import build_owner_effect_receipt  # noqa: E402


STATE_SCHEMA = "cerebro-convergence-owner-state/v1"


class ConvergenceOwnerEffectError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConvergenceOwnerEffectError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _state_fingerprint(value: dict[str, Any]) -> str:
    subject = copy.deepcopy(value)
    subject.pop("state_fingerprint", None)
    return hashlib.sha256(_canonical(subject)).hexdigest()


def create_convergence_state(state_ref: str, basis_fingerprint: str, work_families: list[dict[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "state_ref": state_ref,
        "basis_fingerprint": basis_fingerprint,
        "work_families": copy.deepcopy(work_families),
        "state_fingerprint": "",
    }
    value["state_fingerprint"] = _state_fingerprint(value)
    validate_convergence_state(value)
    return value


def validate_convergence_state(value: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(value, dict) and value.get("schema") == STATE_SCHEMA, "convergence-owner-state-schema-mismatch")
    _require(isinstance(value.get("state_ref"), str) and bool(value["state_ref"]), "convergence-state-ref-required")
    _require(isinstance(value.get("basis_fingerprint"), str) and len(value["basis_fingerprint"]) == 64, "convergence-basis-fingerprint-invalid")
    families = value.get("work_families")
    _require(isinstance(families, list) and bool(families), "convergence-work-families-required")
    mapping: dict[str, dict[str, Any]] = {}
    for family in families:
        _require(isinstance(family, dict), "convergence-work-family-object-required")
        family_id = family.get("family_id")
        _require(isinstance(family_id, str) and bool(family_id) and family_id not in mapping, "convergence-family-id-invalid")
        _require(family.get("state") in {"PENDING", "READY", "IN_PROGRESS", "PASS", "BLOCKED", "STALE"}, "convergence-family-state-invalid")
        depends_on = family.get("depends_on")
        invalidated_by = family.get("invalidated_by")
        _require(isinstance(depends_on, list) and all(isinstance(item, str) and item for item in depends_on), "convergence-family-depends-on-array-required")
        _require(len(depends_on) == len(set(depends_on)), "convergence-family-dependency-duplicate")
        _require(isinstance(invalidated_by, list) and all(isinstance(item, str) and item for item in invalidated_by), "convergence-family-invalidated-by-array-required")
        _require(len(invalidated_by) == len(set(invalidated_by)), "convergence-family-invalidated-by-duplicate")
        pass_basis = family.get("pass_basis_fingerprint")
        _require(pass_basis is None or (isinstance(pass_basis, str) and len(pass_basis) == 64), "convergence-family-pass-basis-invalid")
        mapping[family_id] = family
    _require(all(dep in mapping for family in families for dep in family["depends_on"]), "convergence-dependency-not-found")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(family_id: str) -> None:
        _require(family_id not in visiting, "convergence-dependency-cycle")
        if family_id in visited:
            return
        visiting.add(family_id)
        for dependency in mapping[family_id]["depends_on"]:
            visit(dependency)
        visiting.remove(family_id)
        visited.add(family_id)

    for family_id in mapping:
        visit(family_id)
    _require(value.get("state_fingerprint") == _state_fingerprint(value), "convergence-state-fingerprint-mismatch")
    return {"result": "PASS", "state_ref": value["state_ref"], "family_count": len(mapping), "state_fingerprint": value["state_fingerprint"]}


def _dependent_closure(families: list[dict[str, Any]], initial: set[str]) -> set[str]:
    affected = set(initial)
    changed = True
    while changed:
        changed = False
        for family in families:
            if family["family_id"] not in affected and any(dep in affected for dep in family["depends_on"]):
                affected.add(family["family_id"])
                changed = True
    return affected


def consume_convergence_revalidation_effect(
    *,
    owner_effect: dict[str, Any],
    control_decision_ref: str,
    consolidation_result_ref: str,
    current_state: dict[str, Any],
    new_basis_fingerprint: str,
    directly_affected_family_refs: list[str],
    evidence_refs: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Invalidate affected/dependent PASS using the existing dependency graph."""

    validate_convergence_state(current_state)
    _require(owner_effect.get("owner") == "convergence", "convergence-owner-effect-owner-mismatch")
    _require(owner_effect.get("effect") == "REVALIDATE_AFFECTED", "convergence-owner-effect-type-mismatch")
    _require(owner_effect.get("state_mutation_by_MCP") is False, "MCP-cannot-mutate-convergence-state")
    _require(owner_effect.get("candidate_ref") == consolidation_result_ref, "convergence-owner-effect-candidate-ref-mismatch")
    _require(isinstance(new_basis_fingerprint, str) and len(new_basis_fingerprint) == 64, "convergence-new-basis-fingerprint-invalid")
    mapping = {item["family_id"]: item for item in current_state["work_families"]}
    initial = set(directly_affected_family_refs)
    _require(bool(initial) and initial.issubset(mapping), "convergence-affected-family-not-found")
    affected = _dependent_closure(current_state["work_families"], initial)
    output = copy.deepcopy(current_state)
    output["basis_fingerprint"] = new_basis_fingerprint
    for family in output["work_families"]:
        if family["family_id"] in affected:
            if family["state"] == "PASS":
                family["state"] = "STALE"
                family["pass_basis_fingerprint"] = None
            family["invalidated_by"] = sorted(set(family["invalidated_by"] + [consolidation_result_ref]))
    unaffected = set(mapping).difference(affected)
    _require(
        all(
            next(item for item in output["work_families"] if item["family_id"] == ref) == mapping[ref]
            for ref in unaffected
        ),
        "unaffected-convergence-family-changed",
    )
    output["state_fingerprint"] = _state_fingerprint(output)
    validate_convergence_state(output)
    receipt = build_owner_effect_receipt(
        owner="convergence",
        control_decision_ref=control_decision_ref,
        consolidation_result_ref=consolidation_result_ref,
        effect="REVALIDATE_AFFECTED",
        input_state_ref=current_state["state_ref"],
        input_state_fingerprint=current_state["state_fingerprint"],
        output_state_ref=output["state_ref"],
        output_state_fingerprint=output["state_fingerprint"],
        affected_refs=sorted(affected),
        evidence_refs=evidence_refs,
        unaffected_state_preserved=True,
        state_mutated=output != current_state,
    )
    return output, receipt


def selftest() -> dict[str, Any]:
    old_basis = hashlib.sha256(b"old").hexdigest(); new_basis = hashlib.sha256(b"new").hexdigest()
    current = create_convergence_state("CONV-TOTAL-MCP", old_basis, [
        {"family_id": "F-A", "state": "PASS", "depends_on": [], "pass_basis_fingerprint": old_basis, "invalidated_by": []},
        {"family_id": "F-B", "state": "PASS", "depends_on": ["F-A"], "pass_basis_fingerprint": old_basis, "invalidated_by": []},
        {"family_id": "F-C", "state": "PASS", "depends_on": [], "pass_basis_fingerprint": old_basis, "invalidated_by": []},
    ])
    effect = {"owner": "convergence", "effect": "REVALIDATE_AFFECTED", "candidate_ref": "CCR-000000000000000000000001", "state_mutation_by_MCP": False}
    output, receipt = consume_convergence_revalidation_effect(
        owner_effect=effect,
        control_decision_ref="MCPD-OWNER-1",
        consolidation_result_ref=effect["candidate_ref"],
        current_state=current,
        new_basis_fingerprint=new_basis,
        directly_affected_family_refs=["F-A"],
        evidence_refs=["QUALITY-RECEIPT"],
    )
    mapping = {item["family_id"]: item for item in output["work_families"]}
    tests = [
        {"name": "R33-existing-graph-invalidates-dependent-PASS", "result": "PASS" if mapping["F-A"]["state"] == mapping["F-B"]["state"] == "STALE" else "FAIL"},
        {"name": "R33-unaffected-family-remains-PASS", "result": "PASS" if mapping["F-C"]["state"] == "PASS" else "FAIL"},
        {"name": "convergence-consumer-emits-candidate-until-persisted", "result": "PASS" if receipt["owner"] == "convergence" and receipt["result"] == "CANDIDATE" and receipt["current"] is False and set(receipt["affected_refs"]) == {"F-A", "F-B"} else "FAIL"},
    ]
    return {"schema": "cerebro-convergence-owner-effect-selftest/v1", "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL", "test_count": len(tests), "failures": [item for item in tests if item["result"] != "PASS"], "tests": tests}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["selftest"]); parser.parse_args()
    result = selftest(); print(json.dumps(result, indent=2, ensure_ascii=False)); return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
