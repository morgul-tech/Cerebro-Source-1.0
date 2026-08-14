#!/usr/bin/env python3
"""Context-owner persistence promotion and trusted commit verification.

The semantic Context consumer emits a non-current candidate.  The candidate is
bound into the exact State Service commit receipt before it can be promoted to a
PASS/current owner receipt.  The verifier independently reloads that durable
evidence through a constructor-bound state port; tool payloads cannot inject the
verifier or its identity scope.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[2]
MCP_ROOT = SOURCE_ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

try:
    from .control_context_registry import DIRECTIVE_SCHEMA, validate_transition_receipt
    from .control_context_state_postgres import validate_state_commit_receipt
except ImportError:
    from control_context_registry import DIRECTIVE_SCHEMA, validate_transition_receipt
    from control_context_state_postgres import validate_state_commit_receipt

from control_owner_effect_receipt import (  # noqa: E402
    build_owner_effect_receipt,
    validate_owner_effect_receipt,
)


VERIFICATION_SCHEMA = "cerebro-owner-state-persistence-verification/v1"
VERIFIER_REF = "CEREBRO-CONTEXT-POSTGRES-PERSISTENCE-VERIFIER-V1"


class ContextOwnerPersistenceError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContextOwnerPersistenceError(message)


def _context_refresh_refs(directive: dict[str, Any]) -> list[str]:
    _require(directive.get("schema") == DIRECTIVE_SCHEMA, "context-persistence-directive-schema-mismatch")
    project_operations = directive.get("project_operations")
    _require(isinstance(project_operations, list) and bool(project_operations), "context-persistence-refresh-operations-required")
    refs: list[str] = []
    for operation in project_operations:
        _require(
            isinstance(operation, dict) and operation.get("operation") == "REFRESH_GOVERNING_REFS",
            "context-persistence-allows-only-governing-ref-refresh",
        )
        context_ref = operation.get("context_ref")
        _require(isinstance(context_ref, str) and bool(context_ref), "context-persistence-context-ref-required")
        refs.append(context_ref)
    _require(len(refs) == len(set(refs)), "context-persistence-duplicate-context-ref")
    _require(directive.get("session_operations") == [], "context-persistence-cannot-change-session-focus")
    return sorted(refs)


def promote_context_owner_effect_receipt(
    *,
    candidate: dict[str, Any],
    completion: dict[str, Any],
    directive: dict[str, Any],
) -> dict[str, Any]:
    """Promote an exact precommit candidate after a verified State Service commit."""

    validated = validate_owner_effect_receipt(
        candidate,
        expected_owner="context",
        expected_control_decision_ref=directive.get("decision_ref"),
        expected_effect="REFRESH_GOVERNING_REFS",
    )
    _require(validated["current"] is False and candidate.get("result") == "CANDIDATE", "context-persistence-precommit-candidate-required")
    _require(isinstance(completion, dict) and completion.get("result") == "PASS", "context-persistence-completion-PASS-required")
    transition = completion.get("receipt")
    state_commit = completion.get("state_commit")
    project = completion.get("project")
    session = completion.get("session")
    _require(all(isinstance(value, dict) for value in (transition, state_commit, project, session)), "context-persistence-completion-bundle-required")
    validate_transition_receipt(transition)
    validate_state_commit_receipt(
        state_commit,
        directive=directive,
        owner_effect_candidate=candidate,
        transition_receipt=transition,
        project=project,
        session=session,
    )
    _require(directive.get("event_id") == completion.get("event_id"), "context-persistence-event-mismatch")
    _require(candidate.get("control_decision_ref") == transition.get("decision_ref"), "context-persistence-decision-mismatch")
    _require(candidate.get("input_state_ref") == project.get("project_ref"), "context-persistence-input-state-ref-mismatch")
    _require(candidate.get("output_state_ref") == project.get("project_ref"), "context-persistence-output-state-ref-mismatch")
    _require(candidate.get("input_state_fingerprint") == transition.get("project_fingerprint_before"), "context-persistence-input-fingerprint-mismatch")
    _require(candidate.get("output_state_fingerprint") == transition.get("project_fingerprint_after"), "context-persistence-transition-output-fingerprint-mismatch")
    _require(candidate.get("output_state_fingerprint") == project.get("fingerprint"), "context-persistence-project-output-fingerprint-mismatch")
    _require(candidate.get("affected_refs") == _context_refresh_refs(directive), "context-persistence-affected-refs-mismatch")
    _require(transition.get("receipt_id") in candidate.get("evidence_refs", []), "context-persistence-transition-evidence-ref-required")
    _require(candidate.get("state_mutated") is True and transition.get("mutated") is True, "context-persistence-material-mutation-required")
    _require(candidate.get("unaffected_state_preserved") is True, "context-persistence-unaffected-state-preservation-required")
    promoted = build_owner_effect_receipt(
        owner="context",
        control_decision_ref=candidate["control_decision_ref"],
        consolidation_result_ref=candidate["consolidation_result_ref"],
        effect="REFRESH_GOVERNING_REFS",
        input_state_ref=candidate["input_state_ref"],
        input_state_fingerprint=candidate["input_state_fingerprint"],
        output_state_ref=candidate["output_state_ref"],
        output_state_fingerprint=candidate["output_state_fingerprint"],
        affected_refs=copy.deepcopy(candidate["affected_refs"]),
        evidence_refs=copy.deepcopy(candidate["evidence_refs"]),
        unaffected_state_preserved=True,
        state_mutated=True,
        persistence_evidence_ref=state_commit["commit_ref"],
    )
    validate_owner_effect_receipt(
        promoted,
        expected_owner="context",
        expected_control_decision_ref=candidate["control_decision_ref"],
        expected_consolidation_result_ref=candidate["consolidation_result_ref"],
        expected_effect="REFRESH_GOVERNING_REFS",
    )
    return promoted


class PostgresContextOwnerPersistenceVerifier:
    """Trusted verifier bound to one authenticated consumer/session scope."""

    def __init__(
        self,
        *,
        state_port: Any,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        scopes: set[str],
    ):
        _require(callable(getattr(state_port, "read_state_commit_evidence", None)), "context-persistence-evidence-reader-required")
        for field, value in (
            ("tenant-ref", tenant_ref),
            ("workspace-ref", workspace_ref),
            ("principal-ref", principal_ref),
            ("consumer-ref", consumer_ref),
            ("session-ref", session_ref),
        ):
            _require(isinstance(value, str) and bool(value.strip()), f"context-persistence-{field}-required")
        _require(isinstance(scopes, set) and "project_state:read" in scopes, "context-persistence-read-scope-required")
        self._state_port = state_port
        self._identity = {
            "tenant_ref": tenant_ref,
            "workspace_ref": workspace_ref,
            "principal_ref": principal_ref,
            "consumer_ref": consumer_ref,
            "session_ref": session_ref,
        }
        self._scopes = set(scopes)

    def verify(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
        validated = validate_owner_effect_receipt(
            receipt,
            expected_owner="context",
            expected_effect="REFRESH_GOVERNING_REFS",
        )
        _require(validated["current"] is True and receipt.get("result") == "PASS", "context-persistence-current-PASS-receipt-required")
        commit_ref = receipt.get("persistence_evidence_ref")
        _require(isinstance(commit_ref, str) and bool(commit_ref), "context-persistence-evidence-ref-required")
        bundle = self._state_port.read_state_commit_evidence(
            **self._identity,
            commit_ref=commit_ref,
            scopes=set(self._scopes),
        )
        _require(isinstance(bundle, dict), "context-persistence-evidence-bundle-required")
        candidate = bundle.get("owner_effect_candidate")
        completion = bundle.get("completion")
        directive = bundle.get("directive")
        current_project = bundle.get("current_project")
        _require(all(isinstance(value, dict) for value in (candidate, completion, directive, current_project)), "context-persistence-evidence-bundle-invalid")
        expected = promote_context_owner_effect_receipt(
            candidate=candidate,
            completion=completion,
            directive=directive,
        )
        _require(receipt == expected, "context-persistence-owner-receipt-exact-match-required")
        _require(current_project.get("project_ref") == receipt.get("output_state_ref"), "context-persistence-current-project-ref-mismatch")
        _require(current_project.get("fingerprint") == receipt.get("output_state_fingerprint"), "context-persistence-output-state-no-longer-current")
        return {
            "schema": VERIFICATION_SCHEMA,
            "result": "PASS",
            "verifier_ref": VERIFIER_REF,
            "owner": "context",
            "owner_effect_receipt_ref": receipt["receipt_ref"],
            "owner_effect_receipt_fingerprint": receipt["receipt_fingerprint"],
            "persistence_evidence_ref": commit_ref,
            "output_state_ref": receipt["output_state_ref"],
            "output_state_fingerprint": receipt["output_state_fingerprint"],
        }
