#!/usr/bin/env python3
"""Quality Engine consumer for MCP-routed affected-state invalidation."""

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
QUALITY_VALIDATOR_ROOT = SOURCE_ROOT / "tooling" / "validator"
for path in (MCP_ROOT, QUALITY_VALIDATOR_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_owner_effect_receipt import build_owner_effect_receipt  # noqa: E402
from quality_trace import new as new_quality_trace, pass_stage  # noqa: E402


class QualityOwnerEffectError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise QualityOwnerEffectError(message)


def quality_trace_fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def validate_quality_trace(trace: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(trace, dict) and trace.get("schema") == "cerebro-quality-trace/v0.2", "quality-trace-schema-mismatch")
    _require(isinstance(trace.get("work_item_ref"), str) and bool(trace["work_item_ref"]), "quality-work-item-ref-required")
    _require(isinstance(trace.get("basis_fingerprint"), str) and bool(trace["basis_fingerprint"]), "quality-basis-fingerprint-required")
    stages = trace.get("stages")
    _require(isinstance(stages, dict) and bool(stages), "quality-stages-required")
    for stage_ref, stage in stages.items():
        _require(isinstance(stage_ref, str) and isinstance(stage, dict), "quality-stage-invalid")
        _require(stage.get("state") in {"PENDING", "IN_PROGRESS", "PASS", "BLOCKED", "STALE"}, "quality-stage-state-invalid")
    return {
        "result": "PASS",
        "work_item_ref": trace["work_item_ref"],
        "basis_fingerprint": trace["basis_fingerprint"],
        "trace_fingerprint": quality_trace_fingerprint(trace),
    }


def consume_quality_invalidation_effect(
    *,
    owner_effect: dict[str, Any],
    control_decision_ref: str,
    consolidation_result_ref: str,
    current_trace: dict[str, Any],
    new_basis_fingerprint: str,
    affected_stage_refs: list[str],
    evidence_refs: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Invalidate only affected Quality stages; preserve all unaffected stages."""

    validate_quality_trace(current_trace)
    _require(owner_effect.get("owner") == "quality", "quality-owner-effect-owner-mismatch")
    _require(owner_effect.get("effect") == "INVALIDATE_AFFECTED", "quality-owner-effect-type-mismatch")
    _require(owner_effect.get("state_mutation_by_MCP") is False, "MCP-cannot-mutate-quality-state")
    _require(owner_effect.get("candidate_ref") == consolidation_result_ref, "quality-owner-effect-candidate-ref-mismatch")
    _require(isinstance(new_basis_fingerprint, str) and len(new_basis_fingerprint) == 64, "quality-new-basis-fingerprint-invalid")
    _require(isinstance(affected_stage_refs, list) and bool(affected_stage_refs), "affected-quality-stage-refs-required")
    _require(len(affected_stage_refs) == len(set(affected_stage_refs)), "affected-quality-stage-refs-duplicate")
    stages = current_trace["stages"]
    _require(all(ref in stages for ref in affected_stage_refs), "affected-quality-stage-not-found")
    input_fingerprint = quality_trace_fingerprint(current_trace)
    output = copy.deepcopy(current_trace)
    output["basis_fingerprint"] = new_basis_fingerprint
    for stage_ref in affected_stage_refs:
        stage = output["stages"][stage_ref]
        stage["state"] = "STALE"
        stage["basis_fingerprint"] = new_basis_fingerprint
    output["overall_assurance"] = "STALE"
    unaffected = [ref for ref in stages if ref not in set(affected_stage_refs)]
    _require(
        all(output["stages"][ref] == current_trace["stages"][ref] for ref in unaffected),
        "unaffected-quality-state-changed",
    )
    validate_quality_trace(output)
    output_fingerprint = quality_trace_fingerprint(output)
    receipt = build_owner_effect_receipt(
        owner="quality",
        control_decision_ref=control_decision_ref,
        consolidation_result_ref=consolidation_result_ref,
        effect="INVALIDATE_AFFECTED",
        input_state_ref=current_trace["work_item_ref"],
        input_state_fingerprint=input_fingerprint,
        output_state_ref=current_trace["work_item_ref"],
        output_state_fingerprint=output_fingerprint,
        affected_refs=affected_stage_refs,
        evidence_refs=evidence_refs,
        unaffected_state_preserved=True,
        state_mutated=output != current_trace,
    )
    return output, receipt


def selftest() -> dict[str, Any]:
    old_basis = quality_trace_fingerprint({"basis": "old"})
    new_basis = quality_trace_fingerprint({"basis": "new"})
    trace = new_quality_trace("TOTAL-MCP-QUALITY", "DEEP", old_basis)
    pass_stage(trace, "REFINE", old_basis, ["E-REFINE"])
    pass_stage(trace, "CRITIQUE", old_basis, ["E-CRITIQUE"])
    effect = {"owner": "quality", "effect": "INVALIDATE_AFFECTED", "candidate_ref": "CCR-000000000000000000000001", "state_mutation_by_MCP": False}
    output, receipt = consume_quality_invalidation_effect(
        owner_effect=effect,
        control_decision_ref="MCPD-OWNER-1",
        consolidation_result_ref=effect["candidate_ref"],
        current_trace=trace,
        new_basis_fingerprint=new_basis,
        affected_stage_refs=["REFINE"],
        evidence_refs=["PROJECT-REVISION-RECEIPT"],
    )
    tests = [
        {"name": "R31-affected-quality-PASS-becomes-STALE", "result": "PASS" if output["stages"]["REFINE"]["state"] == "STALE" else "FAIL"},
        {"name": "R32-unaffected-quality-PASS-is-preserved", "result": "PASS" if output["stages"]["CRITIQUE"] == trace["stages"]["CRITIQUE"] else "FAIL"},
        {"name": "quality-consumer-emits-candidate-until-persisted", "result": "PASS" if receipt["owner"] == "quality" and receipt["result"] == "CANDIDATE" and receipt["current"] is False and receipt["unaffected_state_preserved"] is True else "FAIL"},
    ]
    return {"schema": "cerebro-quality-owner-effect-selftest/v1", "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL", "test_count": len(tests), "failures": [item for item in tests if item["result"] != "PASS"], "tests": tests}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["selftest"]); parser.parse_args()
    result = selftest(); print(json.dumps(result, indent=2, ensure_ascii=False)); return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
