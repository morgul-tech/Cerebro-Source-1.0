#!/usr/bin/env python3
"""Context owner consumer for MCP-routed governing-reference refresh effects."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


sys.dont_write_bytecode = True
SOURCE_ROOT = Path(__file__).resolve().parents[2]
MCP_ROOT = SOURCE_ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from control_context_registry import (  # noqa: E402
    DIRECTIVE_SCHEMA,
    apply_transition,
    bind_control_session,
    bootstrap_project_state,
    validate_transition_receipt,
)
from control_owner_effect_receipt import build_owner_effect_receipt  # noqa: E402


class ContextOwnerEffectError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContextOwnerEffectError(message)


def apply_context_refresh_effect(
    *,
    owner_effect: dict[str, Any],
    control_decision_ref: str,
    consolidation_result_ref: str,
    project: dict[str, Any],
    session: dict[str, Any],
    directive: dict[str, Any],
    evidence_refs: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Consume a Context effect through the existing atomic transition domain."""

    _require(owner_effect.get("owner") == "context", "context-owner-effect-owner-mismatch")
    _require(owner_effect.get("effect") == "REFRESH_GOVERNING_REFS", "context-owner-effect-type-mismatch")
    _require(owner_effect.get("state_mutation_by_MCP") is False, "MCP-cannot-mutate-context-state")
    _require(owner_effect.get("candidate_ref") == consolidation_result_ref, "context-owner-effect-candidate-ref-mismatch")
    _require(directive.get("schema") == DIRECTIVE_SCHEMA, "context-owner-directive-schema-mismatch")
    _require(directive.get("decision_ref") == control_decision_ref, "context-owner-directive-decision-mismatch")
    project_operations = directive.get("project_operations")
    _require(isinstance(project_operations, list) and bool(project_operations), "context-refresh-operations-required")
    _require(
        all(isinstance(item, dict) and item.get("operation") == "REFRESH_GOVERNING_REFS" for item in project_operations),
        "context-owner-effect-allows-only-governing-ref-refresh",
    )
    _require(directive.get("session_operations") == [], "context-owner-refresh-cannot-change-session-focus")
    project_after, session_after, transition_receipt = apply_transition(project, session, directive)
    validate_transition_receipt(transition_receipt)
    affected_refs = sorted({item["context_ref"] for item in project_operations})
    owner_receipt = build_owner_effect_receipt(
        owner="context",
        control_decision_ref=control_decision_ref,
        consolidation_result_ref=consolidation_result_ref,
        effect="REFRESH_GOVERNING_REFS",
        input_state_ref=project["project_ref"],
        input_state_fingerprint=project["fingerprint"],
        output_state_ref=project_after["project_ref"],
        output_state_fingerprint=project_after["fingerprint"],
        affected_refs=affected_refs,
        evidence_refs=sorted(set(evidence_refs + [transition_receipt["receipt_id"]])),
        unaffected_state_preserved=True,
        state_mutated=transition_receipt["mutated"],
    )
    return project_after, session_after, transition_receipt, owner_receipt


def selftest() -> dict[str, Any]:
    project, _ = bootstrap_project_state(
        aggregate_id="AGG-CONTEXT-OWNER", tenant_ref="TENANT-1", workspace_ref="WORKSPACE-1",
        project_ref="TOTAL_MCP_REVISION", source_revision="fixture", event_id="E0", decision_ref="D0",
        root={
            "context_id": "CTX-ROOT", "human_label": "Hovedspor", "objective_ref": "OBJ", "scope_ref": "SCOPE",
            "basis_refs": ["BASIS-OLD"], "project_basis_ref": "PB-OLD", "quality_trace_ref": "QT-OLD",
            "completion_criteria_refs": ["DONE"],
        },
    )
    session = bind_control_session(project, session_binding_id="SB", principal_ref="USER", consumer_ref="TEST", session_ref="S")
    consolidation_ref = "CCR-000000000000000000000001"
    effect = {"owner": "context", "effect": "REFRESH_GOVERNING_REFS", "candidate_ref": consolidation_ref, "state_mutation_by_MCP": False}
    directive = {
        "schema": DIRECTIVE_SCHEMA, "event_id": "E1", "decision_ref": "MCPD-OWNER-1",
        "expected_project_revision": project["revision"], "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"], "expected_session_fingerprint": session["fingerprint"],
        "project_operations": [{
            "operation": "REFRESH_GOVERNING_REFS", "context_ref": "CTX-ROOT",
            "project_basis_ref": "PB-NEW", "quality_trace_ref": "QT-NEW", "basis_refs": ["BASIS-NEW"],
        }],
        "session_operations": [],
    }
    project_after, session_after, transition_receipt, owner_receipt = apply_context_refresh_effect(
        owner_effect=effect, control_decision_ref="MCPD-OWNER-1", consolidation_result_ref=consolidation_ref,
        project=project, session=session, directive=directive, evidence_refs=["PROJECT-RECEIPT", "QUALITY-RECEIPT"],
    )
    tests = [
        {"name": "R34-context-consumer-refreshes-governing-refs", "result": "PASS" if project_after["contexts"][0]["project_basis_ref"] == "PB-NEW" else "FAIL"},
        {"name": "R34-context-refresh-preserves-session-focus", "result": "PASS" if session_after["active_context_ref"] == session["active_context_ref"] else "FAIL"},
        {"name": "context-consumer-binds-owner-receipt-to-transition-receipt", "result": "PASS" if transition_receipt["receipt_id"] in owner_receipt["evidence_refs"] else "FAIL"},
        {"name": "context-consumer-remains-candidate-without-durable-commit", "result": "PASS" if owner_receipt["result"] == "CANDIDATE" and owner_receipt["current"] is False and owner_receipt["persistence_evidence_ref"] is None else "FAIL"},
    ]
    return {"schema": "cerebro-context-owner-effect-selftest/v1", "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL", "test_count": len(tests), "failures": [item for item in tests if item["result"] != "PASS"], "tests": tests}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["selftest"]); parser.parse_args()
    result = selftest(); print(json.dumps(result, indent=2, ensure_ascii=False)); return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
