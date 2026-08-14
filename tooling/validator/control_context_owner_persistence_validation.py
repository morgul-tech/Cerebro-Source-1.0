#!/usr/bin/env python3
"""Contract tests for Context owner commit promotion and trusted verification."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable


SOURCE_ROOT = Path(__file__).resolve().parents[2]
for path in (SOURCE_ROOT / "mcp", SOURCE_ROOT / "tooling" / "context"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_context_owner_persistence import (  # noqa: E402
    ContextOwnerPersistenceError,
    PostgresContextOwnerPersistenceVerifier,
    promote_context_owner_effect_receipt,
)
from control_context_registry import DIRECTIVE_SCHEMA, bind_control_session, bootstrap_project_state  # noqa: E402
from control_context_state_postgres import (  # noqa: E402
    build_state_commit_receipt,
    validate_context_owner_candidate_binding,
)
from control_owner_effect import apply_context_refresh_effect  # noqa: E402
from control_owner_effect_receipt import build_owner_effect_receipt  # noqa: E402


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


class EvidenceReader:
    def __init__(self, bundle: dict[str, Any]):
        self.bundle = copy.deepcopy(bundle)
        self.calls: list[dict[str, Any]] = []

    def read_state_commit_evidence(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(copy.deepcopy(kwargs))
        return copy.deepcopy(self.bundle)


def _fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    project, _ = bootstrap_project_state(
        aggregate_id="AGG-CONTEXT-PERSISTENCE",
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        project_ref="TOTAL_MCP_REVISION",
        source_revision="contract-fixture",
        event_id="EVENT-BOOTSTRAP",
        decision_ref="MCPD-BOOTSTRAP",
        root={
            "context_id": "CTX-ROOT",
            "human_label": "Fortsett hovedsporet",
            "objective_ref": "OBJ-TOTAL-MCP-REVISION",
            "scope_ref": "SCOPE-TOTAL-MCP-REVISION",
            "basis_refs": ["BASIS-OLD"],
            "project_basis_ref": "PB-OLD",
            "quality_trace_ref": "QT-OLD",
            "completion_criteria_refs": ["DONE"],
        },
    )
    session = bind_control_session(
        project,
        session_binding_id="CSB-1",
        principal_ref="PRINCIPAL-1",
        consumer_ref="CONSUMER-1",
        session_ref="SESSION-1",
    )
    consolidation_ref = "CCR-000000000000000000000001"
    directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "EVENT-CONTEXT-1",
        "decision_ref": "MCPD-CONTEXT-1",
        "expected_project_revision": project["revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"],
        "expected_session_fingerprint": session["fingerprint"],
        "project_operations": [
            {
                "operation": "REFRESH_GOVERNING_REFS",
                "context_ref": "CTX-ROOT",
                "basis_refs": ["BASIS-NEW"],
                "project_basis_ref": "PB-NEW",
                "quality_trace_ref": "QT-NEW",
            }
        ],
        "session_operations": [],
    }
    project_after, session_after, transition, candidate = apply_context_refresh_effect(
        owner_effect={
            "owner": "context",
            "effect": "REFRESH_GOVERNING_REFS",
            "candidate_ref": consolidation_ref,
            "state_mutation_by_MCP": False,
        },
        control_decision_ref="MCPD-CONTEXT-1",
        consolidation_result_ref=consolidation_ref,
        project=project,
        session=session,
        directive=directive,
        evidence_refs=["PROJECT-RECEIPT", "QUALITY-RECEIPT"],
    )
    state_commit = build_state_commit_receipt(
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="PRINCIPAL-1",
        consumer_ref="CONSUMER-1",
        session_ref="SESSION-1",
        project_ref=project_after["project_ref"],
        event_id="EVENT-CONTEXT-1",
        directive=directive,
        owner_effect_candidate=candidate,
        transition_receipt=transition,
        project=project_after,
        session=session_after,
    )
    completion = {
        "schema": "cerebro-control-context-event-completion/v1",
        "event_id": "EVENT-CONTEXT-1",
        "result": "PASS",
        "receipt": transition,
        "state_commit": state_commit,
        "project": project_after,
        "session": session_after,
        "repository_permission_required": False,
    }
    bundle = {
        "schema": "cerebro-state-service-commit-evidence-bundle/v1",
        "commit": state_commit,
        "transition_receipt": transition,
        "directive": directive,
        "owner_effect_candidate": candidate,
        "completion": completion,
        "current_project": project_after,
    }
    return project, candidate, completion, bundle


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    project_before, candidate, completion, bundle = _fixture()
    check(
        "semantic-context-consumer-remains-precommit-candidate",
        candidate["result"] == "CANDIDATE"
        and candidate["current"] is False
        and candidate["persistence_evidence_ref"] is None,
    )
    check(
        "state-commit-binds-exact-context-owner-candidate",
        completion["state_commit"]["owner_effect_candidate_ref"] == candidate["receipt_ref"]
        and completion["state_commit"]["owner_effect_candidate_fingerprint"] == candidate["receipt_fingerprint"],
    )
    precommit_binding = validate_context_owner_candidate_binding(
        candidate,
        directive=bundle["directive"],
        project_before=project_before,
        project_after=completion["project"],
        transition_receipt=completion["receipt"],
    )
    check(
        "context-owner-candidate-is-validated-against-transition-before-persistence",
        precommit_binding["result"] == "PASS"
        and precommit_binding["candidate_ref"] == candidate["receipt_ref"],
    )
    wrong_affected_candidate = build_owner_effect_receipt(
        owner="context",
        control_decision_ref=candidate["control_decision_ref"],
        consolidation_result_ref=candidate["consolidation_result_ref"],
        effect=candidate["effect"],
        input_state_ref=candidate["input_state_ref"],
        input_state_fingerprint=candidate["input_state_fingerprint"],
        output_state_ref=candidate["output_state_ref"],
        output_state_fingerprint=candidate["output_state_fingerprint"],
        affected_refs=["CTX-WRONG"],
        evidence_refs=candidate["evidence_refs"],
        unaffected_state_preserved=True,
        state_mutated=True,
    )
    check(
        "validly-fingerprinted-but-wrong-context-candidate-fails-before-persistence",
        _expect_error(
            lambda: validate_context_owner_candidate_binding(
                wrong_affected_candidate,
                directive=bundle["directive"],
                project_before=project_before,
                project_after=completion["project"],
                transition_receipt=completion["receipt"],
            ),
            Exception,
        ),
    )
    promoted = promote_context_owner_effect_receipt(
        candidate=candidate,
        completion=completion,
        directive=bundle["directive"],
    )
    check(
        "exact-durable-commit-promotes-context-receipt-to-current-PASS",
        promoted["result"] == "PASS"
        and promoted["current"] is True
        and promoted["persistence_evidence_ref"] == completion["state_commit"]["commit_ref"],
    )
    reader = EvidenceReader(bundle)
    verifier = PostgresContextOwnerPersistenceVerifier(
        state_port=reader,
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="PRINCIPAL-1",
        consumer_ref="CONSUMER-1",
        session_ref="SESSION-1",
        scopes={"project_state:read"},
    )
    verification = verifier.verify(receipt=promoted)
    check(
        "trusted-constructor-bound-verifier-emits-exact-owner-binding",
        verification["result"] == "PASS"
        and verification["owner_effect_receipt_ref"] == promoted["receipt_ref"]
        and verification["owner_effect_receipt_fingerprint"] == promoted["receipt_fingerprint"],
    )
    check(
        "verifier-uses-bound-identity-not-owner-receipt-identity",
        reader.calls == [
            {
                "tenant_ref": "TENANT-1",
                "workspace_ref": "WORKSPACE-1",
                "principal_ref": "PRINCIPAL-1",
                "consumer_ref": "CONSUMER-1",
                "session_ref": "SESSION-1",
                "commit_ref": completion["state_commit"]["commit_ref"],
                "scopes": {"project_state:read"},
            }
        ],
    )
    wrong_evidence_receipt = build_owner_effect_receipt(
        owner="context",
        control_decision_ref=promoted["control_decision_ref"],
        consolidation_result_ref=promoted["consolidation_result_ref"],
        effect=promoted["effect"],
        input_state_ref=promoted["input_state_ref"],
        input_state_fingerprint=promoted["input_state_fingerprint"],
        output_state_ref=promoted["output_state_ref"],
        output_state_fingerprint=promoted["output_state_fingerprint"],
        affected_refs=promoted["affected_refs"],
        evidence_refs=promoted["evidence_refs"],
        unaffected_state_preserved=True,
        state_mutated=True,
        persistence_evidence_ref="SSC-WRONG-EVIDENCE",
    )
    check(
        "unbound-persistence-reference-cannot-pass-exact-verification",
        _expect_error(lambda: verifier.verify(receipt=wrong_evidence_receipt), ContextOwnerPersistenceError),
    )
    stale_bundle = copy.deepcopy(bundle)
    stale_bundle["current_project"] = project_before
    stale_verifier = PostgresContextOwnerPersistenceVerifier(
        state_port=EvidenceReader(stale_bundle),
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="PRINCIPAL-1",
        consumer_ref="CONSUMER-1",
        session_ref="SESSION-1",
        scopes={"project_state:read"},
    )
    check(
        "historical-commit-cannot-claim-output-is-still-current",
        _expect_error(lambda: stale_verifier.verify(receipt=promoted), ContextOwnerPersistenceError),
    )
    check(
        "verifier-construction-fails-without-read-scope",
        _expect_error(
            lambda: PostgresContextOwnerPersistenceVerifier(
                state_port=EvidenceReader(bundle),
                tenant_ref="TENANT-1",
                workspace_ref="WORKSPACE-1",
                principal_ref="PRINCIPAL-1",
                consumer_ref="CONSUMER-1",
                session_ref="SESSION-1",
                scopes=set(),
            ),
            ContextOwnerPersistenceError,
        ),
    )
    result = "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL"
    return {
        "schema": "cerebro-context-owner-persistence-contract-selftest/v1",
        "result": result,
        "evidence_class": "LOCAL_CONTRACT_NOT_LIVE_POSTGRESQL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["selftest"])
    parser.parse_args()
    result = selftest()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
