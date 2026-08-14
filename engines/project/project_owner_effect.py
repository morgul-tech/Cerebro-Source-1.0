#!/usr/bin/env python3
"""Project Engine consumer for an MCP-routed REVISION_REQUIRED effect."""

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


BASIS_SCHEMA = "cerebro-project-basis-state/v1"


class ProjectOwnerEffectError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProjectOwnerEffectError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def project_basis_fingerprint(value: dict[str, Any]) -> str:
    subject = copy.deepcopy(value)
    subject.pop("basis_fingerprint", None)
    return hashlib.sha256(_canonical(subject)).hexdigest()


def create_project_basis(project_ref: str, payload: dict[str, Any], *, revision: int = 1) -> dict[str, Any]:
    _require(isinstance(project_ref, str) and bool(project_ref.strip()), "project-basis-project-ref-required")
    _require(isinstance(payload, dict) and bool(payload), "project-basis-payload-required")
    _require(isinstance(revision, int) and revision >= 1, "project-basis-revision-invalid")
    value: dict[str, Any] = {
        "schema": BASIS_SCHEMA,
        "project_ref": project_ref,
        "basis_revision": revision,
        "basis_ref": "",
        "basis_payload": copy.deepcopy(payload),
        "basis_fingerprint": "",
    }
    provisional = project_basis_fingerprint(value)
    value["basis_ref"] = f"PROJECT-BASIS-{project_ref}-R{revision}-{provisional[:12].upper()}"
    value["basis_fingerprint"] = project_basis_fingerprint(value)
    validate_project_basis(value)
    return value


def validate_project_basis(value: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(value, dict), "project-basis-object-required")
    _require(value.get("schema") == BASIS_SCHEMA, "project-basis-schema-mismatch")
    _require(isinstance(value.get("project_ref"), str) and bool(value["project_ref"]), "project-basis-project-ref-required")
    _require(isinstance(value.get("basis_revision"), int) and value["basis_revision"] >= 1, "project-basis-revision-invalid")
    _require(isinstance(value.get("basis_ref"), str) and bool(value["basis_ref"]), "project-basis-ref-required")
    _require(isinstance(value.get("basis_payload"), dict) and bool(value["basis_payload"]), "project-basis-payload-required")
    _require(value.get("basis_fingerprint") == project_basis_fingerprint(value), "project-basis-fingerprint-mismatch")
    return {
        "result": "PASS",
        "project_ref": value["project_ref"],
        "basis_revision": value["basis_revision"],
        "basis_ref": value["basis_ref"],
        "basis_fingerprint": value["basis_fingerprint"],
    }


def consume_project_revision_effect(
    *,
    owner_effect: dict[str, Any],
    control_decision_ref: str,
    consolidation_result_ref: str,
    current_basis: dict[str, Any],
    revised_payload: dict[str, Any],
    affected_refs: list[str],
    evidence_refs: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive Project-owned revision state and emit a non-current candidate receipt.

    Persistence belongs to the Project owner adapter.  This pure semantic
    consumer cannot claim that its returned state is durably current.
    """

    validate_project_basis(current_basis)
    _require(owner_effect.get("owner") == "project", "project-owner-effect-owner-mismatch")
    _require(owner_effect.get("effect") == "REVISION_REQUIRED", "project-owner-effect-type-mismatch")
    _require(owner_effect.get("state_mutation_by_MCP") is False, "MCP-cannot-mutate-project-basis")
    _require(owner_effect.get("candidate_ref") == consolidation_result_ref, "project-owner-effect-candidate-ref-mismatch")
    _require(isinstance(revised_payload, dict) and bool(revised_payload), "project-revised-payload-required")
    _require(revised_payload != current_basis["basis_payload"], "project-revision-requires-material-delta")
    output = create_project_basis(
        current_basis["project_ref"],
        revised_payload,
        revision=current_basis["basis_revision"] + 1,
    )
    receipt = build_owner_effect_receipt(
        owner="project",
        control_decision_ref=control_decision_ref,
        consolidation_result_ref=consolidation_result_ref,
        effect="REVISION_REQUIRED",
        input_state_ref=current_basis["basis_ref"],
        input_state_fingerprint=current_basis["basis_fingerprint"],
        output_state_ref=output["basis_ref"],
        output_state_fingerprint=output["basis_fingerprint"],
        affected_refs=affected_refs,
        evidence_refs=evidence_refs,
        unaffected_state_preserved=True,
        state_mutated=True,
    )
    return output, receipt


def selftest() -> dict[str, Any]:
    current = create_project_basis("TOTAL_MCP_REVISION", {"objective": "A", "constraints": ["NO_GIT_STATE"]})
    effect = {"owner": "project", "effect": "REVISION_REQUIRED", "candidate_ref": "CCR-000000000000000000000001", "state_mutation_by_MCP": False}
    output, receipt = consume_project_revision_effect(
        owner_effect=effect,
        control_decision_ref="MCPD-OWNER-1",
        consolidation_result_ref=effect["candidate_ref"],
        current_basis=current,
        revised_payload={"objective": "A", "constraints": ["NO_GIT_STATE", "COMMIT_GATED_HNS"]},
        affected_refs=["CTX-ROOT"],
        evidence_refs=["SYNTH-1"],
    )
    tests = [
        {"name": "R30-project-consumer-increments-basis-revision", "result": "PASS" if output["basis_revision"] == 2 else "FAIL"},
        {"name": "R30-project-consumer-emits-candidate-until-persisted", "result": "PASS" if receipt["owner"] == "project" and receipt["result"] == "CANDIDATE" and receipt["current"] is False else "FAIL"},
        {"name": "project-consumer-does-not-mutate-input", "result": "PASS" if current["basis_revision"] == 1 else "FAIL"},
    ]
    return {"schema": "cerebro-project-owner-effect-selftest/v1", "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL", "test_count": len(tests), "failures": [item for item in tests if item["result"] != "PASS"], "tests": tests}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["selftest"]); parser.parse_args()
    result = selftest(); print(json.dumps(result, indent=2, ensure_ascii=False)); return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
