#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable


SOURCE_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_TOOLING = SOURCE_ROOT / "tooling" / "context"
MCP_ROOT = SOURCE_ROOT / "mcp"
if str(CONTEXT_TOOLING) not in sys.path:
    sys.path.insert(0, str(CONTEXT_TOOLING))
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from control_context_registry import (  # noqa: E402
    DIRECTIVE_SCHEMA,
    ControlContextError,
    ancestor_chain,
    apply_transition,
    lowest_common_ancestor,
    refresh_project_fingerprints,
    refresh_session_fingerprint,
    validate_project_state,
    validate_session_state,
    validate_transition_receipt,
)
from control_context_state_port import (  # noqa: E402
    BEGIN_SCHEMA,
    InMemoryControlContextStatePort,
    StateAuthorizationError,
    StateBindingError,
    StateConflict,
    StatePortError,
    StateServiceUnavailable,
)
from control_owner_effect_receipt import build_owner_effect_receipt  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json-object-required")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _root() -> dict[str, Any]:
    return {
        "context_id": "CTX-ROOT",
        "human_label": "Hovedspor",
        "objective_ref": "OBJ-ROOT",
        "scope_ref": "SCOPE-ROOT",
        "basis_refs": ["BASIS-ROOT"],
        "project_basis_ref": "PROJECT-BASIS-1",
        "quality_trace_ref": "QUALITY-DEEP-1",
        "completion_criteria_refs": ["ROOT-DONE"],
    }


def _child(context_id: str, parent: str, objective: str | None = None) -> dict[str, Any]:
    return {
        "operation": "CREATE_CHILD",
        "parent_context_ref": parent,
        "context_id": context_id,
        "human_label": context_id.replace("CTX-", "Gren "),
        "objective_ref": objective or f"OBJ-{context_id}",
        "scope_ref": f"SCOPE-{context_id}",
        "basis_refs": [f"BASIS-{context_id}"],
        "project_basis_ref": "PROJECT-BASIS-1",
        "quality_trace_ref": "QUALITY-DEEP-1",
        "completion_criteria_refs": [f"DONE-{context_id}"],
    }


def _binding(context_ref: str, binding_id: str | None = None, alias: str = "Fortsett denne grenen") -> dict[str, Any]:
    return {
        "binding_id": binding_id or f"BIND-{context_ref}",
        "surface_kind": "HNS",
        "alias": alias,
        "operation": "CONTINUE_CURRENT",
        "target_ref": context_ref,
        "context_ref": context_ref,
    }


def _return_operation(begin: dict[str, Any], context_ref: str, result_ref: str) -> dict[str, Any]:
    context = next(item for item in begin["project"]["contexts"] if item["context_id"] == context_ref)
    return {
        "operation": "RETURN_CONTEXT",
        "context_ref": context_ref,
        "result_ref": result_ref,
        "evidence_refs": [f"EVIDENCE-{context_ref}"],
        "unresolved_refs": [],
        "completion_proof_ref": f"COMPLETION-{context_ref}",
        "completion_criteria_satisfied": True,
        "child_basis_fingerprint": context["basis_fingerprint"],
    }


def _join_and_close(context_ref: str, disposition: str, owner_receipt_ref: str) -> list[dict[str, Any]]:
    return [
        {
            "operation": "APPLY_JOIN_DISPOSITION",
            "context_ref": context_ref,
            "disposition": disposition,
            "dependent_owner_state_current": True,
            "owner_effect_receipt_ref": owner_receipt_ref,
        },
        {"operation": "CLOSE_CONTEXT", "context_ref": context_ref},
    ]


def _begin(port: InMemoryControlContextStatePort, chat: str, event_id: str, key: str) -> dict[str, Any]:
    return port.begin_event(
        {
            "schema": BEGIN_SCHEMA,
            "tenant_ref": "TENANT-1",
            "workspace_ref": "WORKSPACE-1",
            "principal_ref": "ADMIN-1",
            "consumer_ref": "CHATGPT",
            "session_ref": chat,
            "event_id": event_id,
            "idempotency_key": key,
        },
        scopes={"project_state:read", "project_state:transition"},
    )


def _directive(
    begin: dict[str, Any],
    event_id: str,
    project_operations: list[dict[str, Any]] | None = None,
    session_operations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": event_id,
        "decision_ref": f"DEC-{event_id}",
        "expected_project_revision": begin["expected_project_revision"],
        "expected_project_fingerprint": begin["expected_project_fingerprint"],
        "expected_session_revision": begin["expected_session_revision"],
        "expected_session_fingerprint": begin["expected_session_fingerprint"],
        "project_operations": project_operations or [],
        "session_operations": session_operations or [],
    }


def _complete(
    port: InMemoryControlContextStatePort,
    chat: str,
    event_id: str,
    directive: dict[str, Any],
    *,
    owner_effect_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = {
        "tenant_ref": "TENANT-1",
        "workspace_ref": "WORKSPACE-1",
        "principal_ref": "ADMIN-1",
        "consumer_ref": "CHATGPT",
        "session_ref": chat,
        "event_id": event_id,
        "directive": directive,
    }
    if owner_effect_candidate is not None:
        request["owner_effect_candidate"] = owner_effect_candidate
    return port.complete_event(
        request,
        scopes={"project_state:read", "project_state:transition"},
    )


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


def selftest() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def check(name: str, condition: bool, evidence: Any = None) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL", "evidence": evidence})

    scopes = {"project_state:read", "project_state:transition"}
    port = InMemoryControlContextStatePort()
    boot = port.bootstrap_project(
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="ADMIN-1",
        project_ref="TOTAL_MCP_REVISION",
        aggregate_id="AGG-TOTAL-MCP-REVISION",
        source_revision="b49110d16f363f58d1cd79432acb236ab3ac3014",
        event_id="E00",
        decision_ref="DEC-PROJECT-START",
        root=_root(),
        scopes=scopes,
    )
    project = boot["project"]
    check("R02-new-project-has-exactly-one-root", validate_project_state(project)["context_count"] == 1)
    check("R60-new-project-keeps-distinct-identity", project["project_ref"] != "CEREBRO-WORKPLAN")

    for chat in ("CHAT-A", "CHAT-B"):
        port.bind_session(
            tenant_ref="TENANT-1",
            workspace_ref="WORKSPACE-1",
            principal_ref="ADMIN-1",
            consumer_ref="CHATGPT",
            session_ref=chat,
            session_binding_id=f"SESSION-{chat}",
            scopes=scopes,
        )

    begin_a = _begin(port, "CHAT-A", "E01", "K01")
    create_a = _directive(
        begin_a,
        "E01",
        [_child("CTX-A", "CTX-ROOT")],
        [
            {"operation": "SET_ACTIVE", "context_ref": "CTX-A"},
            {"operation": "SET_CONTINUATION_BINDING", "binding": _binding("CTX-A", "BIND-A")},
        ],
    )
    complete_a = _complete(port, "CHAT-A", "E01", create_a)
    check("R08-material-fork-creates-one-child", len(complete_a["project"]["contexts"]) == 2)
    check("R13-switch-is-session-scoped", complete_a["session"]["active_context_ref"] == "CTX-A")
    check("R55-transition-needs-no-repository-permission", complete_a["repository_permission_required"] is False)
    check(
        "R36-transition-receipt-is-valid-and-effect-bound",
        validate_transition_receipt(complete_a["receipt"])["mutated"] is True
        and complete_a["receipt"]["active_context_ref_after"] == "CTX-A",
    )

    check(
        "stale-read-does-not-silently-mutate-session",
        _expect_error(
            lambda: port.read_session(
                tenant_ref="TENANT-1",
                workspace_ref="WORKSPACE-1",
                principal_ref="ADMIN-1",
                consumer_ref="CHATGPT",
                session_ref="CHAT-B",
                scopes=scopes,
            ),
            StateBindingError,
        ),
    )
    begin_b = _begin(port, "CHAT-B", "E02", "K02")
    check("R54A-other-session-focus-unchanged", begin_b["session"]["active_context_ref"] == "CTX-ROOT")
    check("stale-session-rehydration-is-receipted", isinstance(begin_b["rehydration_receipt"], dict))

    before_detour = copy.deepcopy(complete_a["session"])
    begin_detour = _begin(port, "CHAT-A", "E03", "K03")
    detour = _complete(port, "CHAT-A", "E03", _directive(begin_detour, "E03"))
    check("R06-ordinary-question-keeps-context", detour["session"]["active_context_ref"] == "CTX-A")
    check("R07-transient-detour-does-not-mutate-tree", detour["receipt"]["project_revision_before"] == detour["receipt"]["project_revision_after"])
    check("R52-detour-preserves-binding", detour["session"] == before_detour)

    begin_grandchild = _begin(port, "CHAT-A", "E04", "K04")
    grandchild = _complete(
        port,
        "CHAT-A",
        "E04",
        _directive(
            begin_grandchild,
            "E04",
            [_child("CTX-A1", "CTX-A")],
            [
                {"operation": "SET_ACTIVE", "context_ref": "CTX-A1"},
                {"operation": "SET_CONTINUATION_BINDING", "binding": _binding("CTX-A1", "BIND-A1")},
            ],
        ),
    )
    check("R09-grandchild-parent-chain", ancestor_chain(grandchild["project"], "CTX-A1") == ["CTX-A1", "CTX-A", "CTX-ROOT"])

    begin_bad_return = _begin(port, "CHAT-A", "E05", "K05")
    bad_return = _directive(
        begin_bad_return,
        "E05",
        [_return_operation(begin_bad_return, "CTX-A", "RESULT-A")],
        [],
    )
    check("R18-return-with-open-descendant-blocks", _expect_error(lambda: _complete(port, "CHAT-A", "E05", bad_return), StateConflict))

    begin_return_a1 = _begin(port, "CHAT-A", "E06", "K06")
    returned_a1 = _complete(
        port,
        "CHAT-A",
        "E06",
        _directive(
            begin_return_a1,
            "E06",
            [_return_operation(begin_return_a1, "CTX-A1", "RESULT-A1")],
            [
                {"operation": "SET_ACTIVE", "context_ref": "CTX-A"},
                {"operation": "SET_CONTINUATION_BINDING", "binding": _binding("CTX-A", "BIND-A-RETURN")},
            ],
        ),
    )
    mapping = {item["context_id"]: item for item in returned_a1["project"]["contexts"]}
    check("R20-valid-return-is-pending-join", mapping["CTX-A1"]["lifecycle"] == "RETURNED" and mapping["CTX-A1"]["disposition"] == "PENDING_JOIN")
    parent_before_return = next(item for item in begin_return_a1["project"]["contexts"] if item["context_id"] == "CTX-A")
    check(
        "R51-child-return-does-not-silently-redefine-parent",
        mapping["CTX-A"] == parent_before_return,
    )
    invalid_active = copy.deepcopy(returned_a1["session"])
    invalid_active["active_context_ref"] = "CTX-A1"
    invalid_active = refresh_session_fingerprint(invalid_active)
    check(
        "R21-active-context-cannot-point-to-returned",
        _expect_error(lambda: validate_session_state(invalid_active, returned_a1["project"]), ControlContextError),
    )

    begin_pending = _begin(port, "CHAT-A", "E07", "K07")
    pending_return = _directive(
        begin_pending,
        "E07",
        [_return_operation(begin_pending, "CTX-A", "RESULT-A")],
        [],
    )
    check("R19-return-with-pending-descendant-blocks", _expect_error(lambda: _complete(port, "CHAT-A", "E07", pending_return), StateConflict))

    begin_close = _begin(port, "CHAT-A", "E08", "K08")
    closed = _complete(
        port,
        "CHAT-A",
        "E08",
        _directive(
            begin_close,
            "E08",
            _join_and_close("CTX-A1", "INCORPORATED", "OWNER-RECEIPT-A1"),
            [],
        ),
    )
    check("returned-child-closes-with-explicit-disposition", {item["context_id"]: item for item in closed["project"]["contexts"]}["CTX-A1"]["disposition"] == "INCORPORATED")

    begin_other_active = _begin(port, "CHAT-B", "E09", "K09")
    switched_b = _complete(
        port,
        "CHAT-B",
        "E09",
        _directive(begin_other_active, "E09", [], [{"operation": "SET_ACTIVE", "context_ref": "CTX-A"}]),
    )
    check("focus-only-switch-does-not-change-project-revision", switched_b["receipt"]["project_revision_before"] == switched_b["receipt"]["project_revision_after"])

    begin_return_a = _begin(port, "CHAT-A", "E10", "K10")
    return_a = _directive(
        begin_return_a,
        "E10",
        [_return_operation(begin_return_a, "CTX-A", "RESULT-A")],
        [
            {"operation": "SET_ACTIVE", "context_ref": "CTX-ROOT"},
            {"operation": "SET_CONTINUATION_BINDING", "binding": _binding("CTX-ROOT", "BIND-ROOT")},
        ],
    )
    check("other-session-active-context-blocks-return", _expect_error(lambda: _complete(port, "CHAT-A", "E10", return_a), StateConflict))

    begin_b_root = _begin(port, "CHAT-B", "E11", "K11")
    _complete(port, "CHAT-B", "E11", _directive(begin_b_root, "E11", [], [{"operation": "SET_ACTIVE", "context_ref": "CTX-ROOT"}]))
    begin_return_a2 = _begin(port, "CHAT-A", "E12", "K12")
    returned_a = _complete(port, "CHAT-A", "E12", _directive(begin_return_a2, "E12", return_a["project_operations"], return_a["session_operations"]))
    check("R20-valid-parent-return-after-clean-descendants", {item["context_id"]: item for item in returned_a["project"]["contexts"]}["CTX-A"]["lifecycle"] == "RETURNED")

    begin_close_a = _begin(port, "CHAT-A", "E13", "K13")
    _complete(
        port,
        "CHAT-A",
        "E13",
        _directive(begin_close_a, "E13", _join_and_close("CTX-A", "PRESERVED", "OWNER-RECEIPT-A"), []),
    )
    begin_derived = _begin(port, "CHAT-A", "E14", "K14")
    derived_op = _child("CTX-A2", "CTX-ROOT", "OBJ-DERIVED")
    derived_op["operation"] = "CREATE_DERIVED_CONTEXT"
    derived_op["derived_from_context_ref"] = "CTX-A"
    derived = _complete(
        port,
        "CHAT-A",
        "E14",
        _directive(
            begin_derived,
            "E14",
            [derived_op],
            [
                {"operation": "SET_ACTIVE", "context_ref": "CTX-A2"},
                {"operation": "SET_CONTINUATION_BINDING", "binding": _binding("CTX-A2", "BIND-A2")},
            ],
        ),
    )
    check("R22-historical-reopen-creates-derived-context", {item["context_id"]: item for item in derived["project"]["contexts"]}["CTX-A2"]["derived_from_context_ref"] == "CTX-A")

    tampered = copy.deepcopy(derived["project"])
    mapping = {item["context_id"]: item for item in tampered["contexts"]}
    mapping["CTX-ROOT"]["parent_context_ref"] = "CTX-A2"
    tampered = refresh_project_fingerprints(tampered)
    check("R11-cycle-is-rejected", _expect_error(lambda: validate_project_state(tampered), ControlContextError))
    check("R26-lca-is-deterministic", lowest_common_ancestor(derived["project"], ["CTX-A1", "CTX-A2"]) == "CTX-ROOT")

    before_invalid = port.read_project(tenant_ref="TENANT-1", workspace_ref="WORKSPACE-1", project_ref="TOTAL_MCP_REVISION", scopes=scopes)
    begin_invalid = _begin(port, "CHAT-A", "E15", "K15")
    invalid = _directive(begin_invalid, "E15", [{"operation": "UNKNOWN"}], [])
    check("R35-invalid-directive-blocks", _expect_error(lambda: _complete(port, "CHAT-A", "E15", invalid), StateConflict))
    after_invalid = port.read_project(tenant_ref="TENANT-1", workspace_ref="WORKSPACE-1", project_ref="TOTAL_MCP_REVISION", scopes=scopes)
    check("R35-invalid-directive-is-atomic", before_invalid == after_invalid)

    begin_duplicate = _begin(port, "CHAT-A", "E16", "K16")
    first = _complete(port, "CHAT-A", "E16", _directive(begin_duplicate, "E16"))
    second = _complete(port, "CHAT-A", "E16", _directive(begin_duplicate, "E16"))
    check("R58-duplicate-event-is-idempotent", first == second)
    altered_completion = _directive(begin_duplicate, "E16")
    altered_completion["decision_ref"] = "DEC-DIFFERENT"
    check(
        "R58-idempotency-does-not-hide-a-different-completion",
        _expect_error(lambda: _complete(port, "CHAT-A", "E16", altered_completion), StateConflict),
    )
    check(
        "R58-idempotency-key-reuse-with-different-begin-request-conflicts",
        _expect_error(lambda: _begin(port, "CHAT-A", "E16-OTHER", "K16"), StateConflict),
    )

    begin_c1 = _begin(port, "CHAT-A", "E17", "K17")
    begin_c2 = _begin(port, "CHAT-B", "E18", "K18")
    create_c1 = _child("CTX-C1", "CTX-ROOT")
    create_c2 = _child("CTX-C2", "CTX-ROOT")
    _complete(port, "CHAT-A", "E17", _directive(begin_c1, "E17", [create_c1], []))
    check(
        "R54-concurrent-project-write-conflicts",
        _expect_error(lambda: _complete(port, "CHAT-B", "E18", _directive(begin_c2, "E18", [create_c2], [])), StateConflict),
    )

    begin_deep = _begin(port, "CHAT-A", "E20", "K20")
    deep_operations: list[dict[str, Any]] = []
    deep_parent = "CTX-C1"
    for depth in range(2, 9):
        context_id = f"CTX-DEEP-{depth}"
        deep_operations.append(_child(context_id, deep_parent))
        deep_parent = context_id
    deep = _complete(port, "CHAT-A", "E20", _directive(begin_deep, "E20", deep_operations, []))
    check(
        "R10-deep-nesting-uses-one-parent-link-algorithm",
        len(ancestor_chain(deep["project"], "CTX-DEEP-8")) == 9
        and ancestor_chain(deep["project"], "CTX-DEEP-8")[-1] == "CTX-ROOT",
    )

    begin_equivalent = _begin(port, "CHAT-A", "E21", "K21")
    existing_c1 = next(item for item in begin_equivalent["project"]["contexts"] if item["context_id"] == "CTX-C1")
    equivalent = {
        "operation": "CREATE_CHILD",
        "parent_context_ref": existing_c1["parent_context_ref"],
        "context_id": "CTX-C1-EQUIVALENT",
        "human_label": "Lik gren",
        "objective_ref": existing_c1["objective_ref"],
        "scope_ref": existing_c1["scope_ref"],
        "basis_refs": existing_c1["basis_refs"],
        "project_basis_ref": existing_c1["project_basis_ref"],
        "quality_trace_ref": existing_c1["quality_trace_ref"],
        "completion_criteria_refs": ["DONE-EQUIVALENT"],
    }
    project_before_equivalent = copy.deepcopy(begin_equivalent["project"])
    check(
        "R12-equivalent-fork-without-material-delta-is-rejected",
        _expect_error(
            lambda: _complete(port, "CHAT-A", "E21", _directive(begin_equivalent, "E21", [equivalent], [])),
            StateConflict,
        ),
    )
    project_after_equivalent = port.read_project(
        tenant_ref="TENANT-1", workspace_ref="WORKSPACE-1", project_ref="TOTAL_MCP_REVISION", scopes=scopes
    )
    check("R12-equivalent-fork-rejection-is-atomic", project_after_equivalent == project_before_equivalent)
    begin_recursive = _begin(port, "CHAT-A", "E21A", "K21A")
    recursive = copy.deepcopy(equivalent)
    recursive.update(context_id="CTX-C1-RECURSIVE", parent_context_ref="CTX-C1", human_label="Rekursiv gren")
    check(
        "R12-equivalent-ancestor-recursion-is-rejected",
        _expect_error(
            lambda: _complete(port, "CHAT-A", "E21A", _directive(begin_recursive, "E21A", [recursive], [])),
            StateConflict,
        ),
    )

    session_before_correction = port.read_session(
        tenant_ref="TENANT-1", workspace_ref="WORKSPACE-1", principal_ref="ADMIN-1",
        consumer_ref="CHATGPT", session_ref="CHAT-A", scopes=scopes,
    )
    begin_correction = _begin(port, "CHAT-A", "E22", "K22")
    corrected = _complete(
        port,
        "CHAT-A",
        "E22",
        _directive(
            begin_correction,
            "E22",
            [],
            [{
                "operation": "SET_CONTINUATION_BINDING",
                "binding": _binding(
                    session_before_correction["active_context_ref"],
                    "BIND-CORRECTED",
                    "Fortsett korrigert gren",
                ),
            }],
        ),
    )
    check(
        "R53-material-correction-replaces-binding-atomically",
        corrected["project"] == begin_correction["project"]
        and corrected["session"]["active_continuation_binding"]["binding_id"] == "BIND-CORRECTED"
        and corrected["session"]["fingerprint"] != session_before_correction["fingerprint"],
    )

    active_ref = corrected["session"]["active_context_ref"]
    condition_states: dict[str, dict[str, Any]] = {}
    for event_number, condition in ((23, "PAUSED_BY_USER"), (24, "WAITING_HUMAN"), (25, "STALLED"), (26, "SAFE_HOLD"), (27, "READY")):
        event_id = f"E{event_number}"
        begin_condition = _begin(port, "CHAT-A", event_id, f"K{event_number}")
        condition_states[condition] = _complete(
            port,
            "CHAT-A",
            event_id,
            _directive(
                begin_condition,
                event_id,
                [{
                    "operation": "SET_CONTROL_CONDITION",
                    "context_ref": active_ref,
                    "control_condition": condition,
                }],
                [],
            ),
        )
    check(
        "R14-paused-by-user-preserves-open-context",
        next(item for item in condition_states["PAUSED_BY_USER"]["project"]["contexts"] if item["context_id"] == active_ref)["lifecycle"] == "OPEN",
    )
    check(
        "R15-waiting-human-remains-active-session-focus",
        condition_states["WAITING_HUMAN"]["session"]["active_context_ref"] == active_ref
        and next(item for item in condition_states["WAITING_HUMAN"]["project"]["contexts"] if item["context_id"] == active_ref)["control_condition"] == "WAITING_HUMAN",
    )
    check(
        "R17-safe-hold-preserves-state-until-verified-resume",
        next(item for item in condition_states["SAFE_HOLD"]["project"]["contexts"] if item["context_id"] == active_ref)["control_condition"] == "SAFE_HOLD"
        and next(item for item in condition_states["READY"]["project"]["contexts"] if item["context_id"] == active_ref)["control_condition"] == "READY",
    )

    begin_refresh = _begin(port, "CHAT-A", "E28", "K28")
    active_before_refresh = next(item for item in begin_refresh["project"]["contexts"] if item["context_id"] == active_ref)
    refreshed = _complete(
        port,
        "CHAT-A",
        "E28",
        _directive(
            begin_refresh,
            "E28",
            [{
                "operation": "REFRESH_GOVERNING_REFS",
                "context_ref": active_ref,
                "project_basis_ref": "PROJECT-BASIS-2",
                "quality_trace_ref": "QUALITY-DEEP-2",
                "basis_refs": ["BASIS-REFRESHED"],
            }],
            [],
        ),
    )
    active_after_refresh = next(item for item in refreshed["project"]["contexts"] if item["context_id"] == active_ref)
    check(
        "R34-material-owner-revision-refreshes-governing-context-refs",
        active_after_refresh["project_basis_ref"] == "PROJECT-BASIS-2"
        and active_after_refresh["quality_trace_ref"] == "QUALITY-DEEP-2"
        and active_after_refresh["basis_fingerprint"] != active_before_refresh["basis_fingerprint"],
    )

    begin_bound_refresh = _begin(port, "CHAT-A", "E28B", "K28B")
    bound_refresh_directive = _directive(
        begin_bound_refresh,
        "E28B",
        [{
            "operation": "REFRESH_GOVERNING_REFS",
            "context_ref": active_ref,
            "project_basis_ref": "PROJECT-BASIS-3",
            "quality_trace_ref": "QUALITY-DEEP-3",
            "basis_refs": ["BASIS-REFRESHED-3"],
        }],
        [],
    )
    predicted_project, _, predicted_transition = apply_transition(
        begin_bound_refresh["project"],
        begin_bound_refresh["session"],
        bound_refresh_directive,
    )
    candidate_args = {
        "owner": "context",
        "control_decision_ref": bound_refresh_directive["decision_ref"],
        "consolidation_result_ref": "CCR-AAAAAAAAAAAAAAAAAAAAAAAA",
        "effect": "REFRESH_GOVERNING_REFS",
        "input_state_ref": begin_bound_refresh["project"]["project_ref"],
        "input_state_fingerprint": begin_bound_refresh["project"]["fingerprint"],
        "output_state_ref": predicted_project["project_ref"],
        "output_state_fingerprint": predicted_project["fingerprint"],
        "evidence_refs": [predicted_transition["receipt_id"]],
        "unaffected_state_preserved": True,
        "state_mutated": True,
    }
    wrong_candidate = build_owner_effect_receipt(
        **candidate_args,
        affected_refs=["CTX-NOT-TARGET"],
    )
    rejected_wrong_candidate = _expect_error(
        lambda: _complete(
            port,
            "CHAT-A",
            "E28B",
            bound_refresh_directive,
            owner_effect_candidate=wrong_candidate,
        ),
        StatePortError,
    )
    project_after_rejection = port.read_project(
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="ADMIN-1",
        project_ref="TOTAL_MCP_REVISION",
        scopes={"project_state:read"},
    )
    check(
        "validly-fingerprinted-wrong-context-candidate-is-atomic-in-memory",
        rejected_wrong_candidate
        and project_after_rejection["fingerprint"] == begin_bound_refresh["project"]["fingerprint"],
    )
    correct_candidate = build_owner_effect_receipt(
        **candidate_args,
        affected_refs=[active_ref],
    )
    bound_refresh = _complete(
        port,
        "CHAT-A",
        "E28B",
        bound_refresh_directive,
        owner_effect_candidate=correct_candidate,
    )
    check(
        "exact-context-candidate-is-accepted-before-in-memory-mutation",
        bound_refresh["project"]["fingerprint"] == predicted_project["fingerprint"],
    )

    final_mapping = {item["context_id"]: item for item in bound_refresh["project"]["contexts"]}
    check(
        "R50-no-created-branch-disappears-without-lifecycle-and-disposition",
        {"CTX-A", "CTX-A1", "CTX-A2", "CTX-C1"}.issubset(final_mapping)
        and final_mapping["CTX-A"]["lifecycle"] == "CLOSED"
        and final_mapping["CTX-A"]["disposition"] == "PRESERVED"
        and final_mapping["CTX-A1"]["lifecycle"] == "CLOSED"
        and final_mapping["CTX-A1"]["disposition"] == "INCORPORATED"
        and final_mapping["CTX-A2"]["lifecycle"] == "OPEN",
    )

    port.set_available(False)
    check("R57-outage-fails-closed", _expect_error(lambda: _begin(port, "CHAT-A", "E29", "K29"), StateServiceUnavailable))
    port.set_available(True)
    check(
        "state-write-requires-bounded-scope",
        _expect_error(
            lambda: port.set_default_project(
                tenant_ref="TENANT-1", workspace_ref="WORKSPACE-1", principal_ref="ADMIN-1",
                project_ref="TOTAL_MCP_REVISION", scopes=set()
            ),
            StateAuthorizationError,
        ),
    )

    manifest = _read_json(SOURCE_ROOT / "tooling/validator/control_context_scenarios.json")
    required_ids = manifest.get("required_scenario_ids", [])
    implemented_ids = set(manifest.get("implemented_selftest_bindings", {}))
    pending_ids = set(manifest.get("pending_integration_or_owner_scenarios", {}))
    check("scenario-manifest-ids-are-unique", len(required_ids) == len(set(required_ids)))
    check("scenario-manifest-has-no-claim-gap", set(required_ids) == implemented_ids | pending_ids)
    check("scenario-manifest-statuses-do-not-overlap", not implemented_ids.intersection(pending_ids))

    return {
        "schema": "cerebro-control-context-validation-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_project = sub.add_parser("validate-project")
    p_project.add_argument("--project", required=True)
    p_project.add_argument("--output")
    p_session = sub.add_parser("validate-session")
    p_session.add_argument("--project", required=True)
    p_session.add_argument("--session", required=True)
    p_session.add_argument("--output")
    p_transition = sub.add_parser("validate-transition")
    p_transition.add_argument("--project", required=True)
    p_transition.add_argument("--session", required=True)
    p_transition.add_argument("--directive", required=True)
    p_transition.add_argument("--output")
    p_selftest = sub.add_parser("selftest")
    p_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "validate-project":
            result = validate_project_state(_read_json(Path(args.project)))
        elif args.command == "validate-session":
            project = _read_json(Path(args.project))
            result = validate_session_state(_read_json(Path(args.session)), project)
        elif args.command == "validate-transition":
            project = _read_json(Path(args.project))
            session = _read_json(Path(args.session))
            project_after, session_after, receipt = apply_transition(project, session, _read_json(Path(args.directive)))
            result = {"result": "PASS", "project": project_after, "session": session_after, "receipt": receipt}
        else:
            result = selftest()
        output = getattr(args, "output", None)
        if output:
            _write_json(Path(output), result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("result") == "PASS" else 1
    except Exception as exc:
        result = {"result": "BLOCK", "error": str(exc)}
        output = getattr(args, "output", None)
        if output:
            _write_json(Path(output), result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
