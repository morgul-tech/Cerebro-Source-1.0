#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_TOOLING = SOURCE_ROOT / "tooling" / "context"
if str(CONTEXT_TOOLING) not in sys.path:
    sys.path.insert(0, str(CONTEXT_TOOLING))

from control_context_registry import (  # noqa: E402
    DIRECTIVE_SCHEMA,
    ancestor_chain,
    apply_transition,
    bind_control_session,
    bootstrap_project_state,
    validate_transition_receipt,
    validate_alias,
    validate_project_state,
    validate_session_state,
)


CANDIDATE_SCHEMA = "cerebro-human-navigation-surface-candidate/v1"
OPTIONS_SCHEMA = "cerebro-mcp-context-navigation-options/v1"
OPTIONS_CANDIDATE_SCHEMA = "cerebro-mcp-context-navigation-options-candidate/v1"
VALIDATION_SCHEMA = "cerebro-human-navigation-surface-validation/v1"
BOUNDARIES = {"HNS", "HCS", "EXACT_SHELL", "BOOT_NO_PROJECT", "TERMINAL", "FAIL_CLOSED", "MACHINE_CONTINUES"}
HNS_OPERATIONS = {"CONTINUE_CURRENT", "RETURN_PARENT", "RETURN_ROOT", "CONSOLIDATE_WITH_ROOT", "SWITCH_CONTEXT", "PAUSE"}
HNS_RENDER_PRECONDITION = "COMMIT_RECEIPT_AND_STATE_MATCH_VERIFIED"
HNS_CANDIDATE_ACTIVATION_PRECONDITION = "ACTUAL_TRANSITION_RECEIPT_AND_COMMITTED_STATE_EXACTLY_MATCH_PREDICTION"


class HumanNavigationSurfaceError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HumanNavigationSurfaceError(message)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), "json-object-required")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _fingerprint(value: dict[str, Any], field: str) -> str:
    subject = copy.deepcopy(value)
    subject.pop(field, None)
    raw = json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _terminal_trigger(text: str) -> str:
    stripped = text.rstrip()
    match = re.search(r"(?s)(?:^|\n)```(?:text)?\s*\n([^`\r\n]+)\r?\n```$", stripped)
    _require(match is not None, "terminal-copyable-trigger-block-required")
    return match.group(1).strip()


def _validate_action(action: Any, approved: set[str]) -> dict[str, Any]:
    _require(isinstance(action, dict), "navigation-action-object-required")
    for field in ("action_id", "alias", "operation", "target_ref"):
        _require(isinstance(action.get(field), str) and bool(action[field].strip()), f"navigation-{field}-required")
    _require(action.get("action_id") in approved, "presentation-invented-unapproved-action")
    _require(action.get("approved_by_mcp") is True, "navigation-action-mcp-approval-required")
    _require(action.get("operation") in HNS_OPERATIONS, "navigation-operation-invalid")
    validate_alias(action.get("alias"))
    return action


def _validate_option_state_and_actions(
    options: dict[str, Any], project: dict[str, Any], session: dict[str, Any]
) -> dict[str, Any]:
    for field, expected in (
        ("project_ref", project["project_ref"]),
        ("session_ref", session["session_ref"]),
        ("source_context_ref", session["active_context_ref"]),
        ("project_revision", project["revision"]),
        ("session_revision", session["session_revision"]),
        ("project_fingerprint", project["fingerprint"]),
        ("session_fingerprint", session["fingerprint"]),
    ):
        _require(options.get(field) == expected, f"hns-options-{field}-stale")
    primary = options.get("primary")
    optional = options.get("optional")
    _require(isinstance(primary, dict), "hns-options-primary-required")
    _require(isinstance(optional, list), "hns-options-optional-array-required")
    _require(len(optional) <= 3, "hns-options-optional-maximum-three")
    actions = [primary, *optional]
    ids: list[str] = []
    mapping = {item["context_id"]: item for item in project["contexts"]}
    source_ref = session["active_context_ref"]
    source_ancestors = set(ancestor_chain(project, source_ref))
    for action in actions:
        _require(isinstance(action, dict), "hns-option-action-object-required")
        for field in ("action_id", "alias", "operation", "target_ref"):
            _require(isinstance(action.get(field), str) and bool(action[field].strip()), f"hns-option-{field}-required")
        _require(action.get("approved_by_mcp") is True, "hns-option-mcp-approval-required")
        _require(action.get("surface_kind") == "HNS", "hns-option-surface-kind-required")
        _require(action.get("operation") in HNS_OPERATIONS, "hns-option-operation-invalid")
        validate_alias(action["alias"])
        ids.append(action["action_id"])
        target_ref = action["target_ref"]
        _require(target_ref in mapping and mapping[target_ref].get("lifecycle") == "OPEN", "hns-option-target-must-be-open")
        if action["operation"] in {"CONTINUE_CURRENT", "PAUSE"}:
            _require(target_ref == source_ref, "hns-current-operation-target-must-be-active")
        elif action["operation"] in {"RETURN_PARENT", "RETURN_ROOT", "CONSOLIDATE_WITH_ROOT"}:
            _require(target_ref in source_ancestors, "hns-return-or-root-target-must-be-ancestor")
    _require(len(ids) == len(set(ids)), "hns-option-action-ids-must-be-unique")
    aliases = [action["alias"] for action in actions]
    _require(len(aliases) == len(set(aliases)), "hns-option-aliases-must-be-unique")

    binding = session.get("active_continuation_binding")
    _require(isinstance(binding, dict), "hns-options-primary-requires-committed-binding")
    for action_field, binding_field in (
        ("binding_id", "binding_id"),
        ("alias", "alias"),
        ("operation", "operation"),
        ("target_ref", "target_ref"),
    ):
        _require(primary.get(action_field) == binding.get(binding_field), f"hns-options-primary-{action_field}-mismatch")
    return {
        "approved_action_refs": ids,
        "primary": copy.deepcopy(primary),
        "optional": copy.deepcopy(optional),
    }


def validate_navigation_options_candidate(
    candidate: dict[str, Any],
    project: dict[str, Any],
    session: dict[str, Any],
    predicted_receipt: dict[str, Any],
) -> dict[str, Any]:
    _require(candidate.get("schema") == OPTIONS_CANDIDATE_SCHEMA, "hns-options-candidate-schema-mismatch")
    _require(candidate.get("authority") == "MCP", "hns-options-candidate-authority-must-be-MCP")
    _require(candidate.get("state_basis") == "PREDICTED_POST_COMMIT_STATE", "hns-options-candidate-state-basis-invalid")
    _require(candidate.get("render_authorized") is False, "hns-options-candidate-cannot-authorize-render")
    _require(
        candidate.get("activation_precondition") == HNS_CANDIDATE_ACTIVATION_PRECONDITION,
        "hns-options-candidate-activation-precondition-invalid",
    )
    validate_transition_receipt(predicted_receipt)
    _require(candidate.get("expected_transition_receipt_ref") == predicted_receipt["receipt_id"], "hns-options-candidate-receipt-ref-mismatch")
    _require(
        candidate.get("expected_transition_receipt_fingerprint") == predicted_receipt["receipt_fingerprint"],
        "hns-options-candidate-receipt-fingerprint-mismatch",
    )
    _require(
        isinstance(candidate.get("control_decision_ref"), str) and bool(candidate["control_decision_ref"].strip()),
        "hns-options-candidate-control-decision-ref-required",
    )
    _require(
        candidate.get("candidate_fingerprint") == _fingerprint(candidate, "candidate_fingerprint"),
        "hns-options-candidate-fingerprint-mismatch",
    )
    action_result = _validate_option_state_and_actions(candidate, project, session)
    return {
        "result": "PASS",
        **action_result,
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "render_authorized": False,
    }


def validate_navigation_options(
    options: dict[str, Any],
    project: dict[str, Any],
    session: dict[str, Any],
    completion_receipt: dict[str, Any],
) -> dict[str, Any]:
    _require(options.get("schema") == OPTIONS_SCHEMA, "hns-options-schema-mismatch")
    _require(options.get("authority") == "MCP", "hns-options-authority-must-be-MCP")
    _require(options.get("state_basis") == "COMMITTED_STATE", "hns-options-state-basis-invalid")
    _require(options.get("render_precondition") == HNS_RENDER_PRECONDITION, "hns-options-render-precondition-invalid")
    _require(options.get("commit_verified") is True, "hns-options-commit-verification-required")
    validate_transition_receipt(completion_receipt)
    _require(options.get("commit_receipt_ref") == completion_receipt["receipt_id"], "hns-options-commit-receipt-ref-mismatch")
    _require(
        options.get("commit_receipt_fingerprint") == completion_receipt["receipt_fingerprint"],
        "hns-options-commit-receipt-fingerprint-mismatch",
    )
    for field, receipt_field in (
        ("project_revision", "project_revision_after"),
        ("session_revision", "session_revision_after"),
        ("project_fingerprint", "project_fingerprint_after"),
        ("session_fingerprint", "session_fingerprint_after"),
    ):
        _require(options.get(field) == completion_receipt.get(receipt_field), f"hns-options-{field}-receipt-mismatch")
    _require(
        isinstance(options.get("control_decision_ref"), str) and bool(options["control_decision_ref"].strip()),
        "hns-options-control-decision-ref-required",
    )
    _require(
        options.get("options_fingerprint") == _fingerprint(options, "options_fingerprint"),
        "hns-options-fingerprint-mismatch",
    )
    action_result = _validate_option_state_and_actions(options, project, session)
    return {
        "result": "PASS",
        **action_result,
        "options_fingerprint": options["options_fingerprint"],
        "commit_receipt_ref": options["commit_receipt_ref"],
    }


def validate_surface(
    candidate: dict[str, Any],
    project: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    completion_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require(candidate.get("schema") == CANDIDATE_SCHEMA, "hns-candidate-schema-mismatch")
    boundary = candidate.get("boundary")
    _require(boundary in BOUNDARIES, "hns-boundary-invalid")
    _require(isinstance(candidate.get("human_action_is_next"), bool), "human-action-is-next-boolean-required")
    _require(isinstance(candidate.get("machine_action_pending"), bool), "machine-action-pending-boolean-required")
    optional = candidate.get("optional")
    _require(isinstance(optional, list), "optional-navigation-array-required")
    _require(len(optional) <= 3, "optional-navigation-maximum-three")
    approved_values = candidate.get("mcp_approved_action_refs")
    _require(isinstance(approved_values, list), "mcp-approved-action-refs-required")
    approved = {str(value) for value in approved_values}
    _require(len(approved) == len(approved_values), "mcp-approved-action-refs-duplicate")
    primary = candidate.get("primary")
    response_text = candidate.get("response_text")
    _require(isinstance(response_text, str), "response-text-required")

    if boundary in {"BOOT_NO_PROJECT", "TERMINAL", "FAIL_CLOSED", "MACHINE_CONTINUES", "HCS", "EXACT_SHELL"}:
        _require(options is None, f"hns-options-suppressed-at-{boundary.lower()}")
        _require(completion_receipt is None, f"hns-completion-receipt-suppressed-at-{boundary.lower()}")
        _require(primary is None, f"hns-primary-suppressed-at-{boundary.lower()}")
        _require(not optional, f"hns-optional-suppressed-at-{boundary.lower()}")
        _require(not approved, f"hns-approved-actions-suppressed-at-{boundary.lower()}")
        if boundary == "MACHINE_CONTINUES":
            _require(candidate["machine_action_pending"] is True, "machine-continues-requires-pending-machine-action")
            _require(candidate["human_action_is_next"] is False, "machine-continues-cannot-have-human-action-next")
        if boundary in {"HCS", "EXACT_SHELL"}:
            _require(candidate["human_action_is_next"] is True, "human-boundary-requires-human-action-next")
        if boundary == "BOOT_NO_PROJECT":
            _require(project is None and session is None, "boot-no-project-must-not-require-project-state")
        if boundary in {"BOOT_NO_PROJECT", "TERMINAL", "FAIL_CLOSED"}:
            _require(candidate["human_action_is_next"] is False, f"{boundary.lower()}-cannot-require-human-action")
            _require(candidate["machine_action_pending"] is False, f"{boundary.lower()}-cannot-claim-pending-machine-action")
        return {
            "schema": VALIDATION_SCHEMA,
            "result": "PASS",
            "boundary": boundary,
            "hns_suppressed": True,
            "primary_count": 0,
            "optional_count": 0,
        }

    _require(boundary == "HNS", "unsupported-hns-boundary")
    _require(project is not None and session is not None, "hns-project-and-session-state-required")
    _require(isinstance(options, dict), "hns-MCP-navigation-options-required")
    _require(isinstance(completion_receipt, dict), "hns-completion-receipt-required")
    _require(candidate["human_action_is_next"] is True, "hns-requires-human-action-next")
    project_result = validate_project_state(project)
    session_result = validate_session_state(session, project)
    options_result = validate_navigation_options(options, project, session, completion_receipt)
    _require(approved == set(options_result["approved_action_refs"]), "rendered-approved-actions-do-not-match-MCP-options")
    _require(candidate.get("project_ref") == project["project_ref"], "hns-project-ref-mismatch")
    _require(candidate.get("session_ref") == session["session_ref"], "hns-session-ref-mismatch")
    _require(candidate.get("project_revision") == project["revision"], "hns-project-revision-stale")
    _require(candidate.get("session_revision") == session["session_revision"], "hns-session-revision-stale")
    _require(candidate.get("project_fingerprint") == project["fingerprint"], "hns-project-fingerprint-stale")
    _require(candidate.get("session_fingerprint") == session["fingerprint"], "hns-session-fingerprint-stale")
    _require(candidate["machine_action_pending"] is False, "hns-cannot-replace-pending-machine-action")

    action = _validate_action(primary, approved)
    _require(action == options_result["primary"], "rendered-primary-does-not-match-MCP-option")
    visible = _terminal_trigger(response_text)
    _require(visible == action["alias"], "visible-trigger-does-not-match-hns-primary")
    _require(response_text.rstrip().count(visible) == 1, "exactly-one-visible-primary-trigger-required")

    actions = [_validate_action(item, approved) for item in optional]
    _require(actions == options_result["optional"], "rendered-optional-actions-do-not-match-MCP-options")
    action_ids = [item["action_id"] for item in actions]
    _require(len(action_ids) == len(set(action_ids)), "duplicate-optional-action")
    if isinstance(primary, dict):
        _require(primary["action_id"] not in set(action_ids), "primary-duplicated-as-optional")
    represented = set(action_ids)
    if isinstance(primary, dict):
        represented.add(primary["action_id"])
    _require(represented == approved, "approved-action-set-not-fully-rendered")
    for optional_action in actions:
        _require(
            response_text.count(optional_action["alias"]) == 1,
            f"optional-action-not-rendered-exactly-once:{optional_action['action_id']}",
        )
        _require(
            response_text.index(optional_action["alias"]) < response_text.rfind(visible),
            f"optional-action-must-precede-primary:{optional_action['action_id']}",
        )
    return {
        "schema": VALIDATION_SCHEMA,
        "result": "PASS",
        "boundary": boundary,
        "project_validation": project_result,
        "session_validation": session_result,
        "options_validation": options_result,
        "hns_suppressed": False,
        "primary_count": 1,
        "optional_count": len(optional),
        "absolute_response_end": True,
    }


def validate_detour_continuity(
    before_project: dict[str, Any],
    before_session: dict[str, Any],
    after_project: dict[str, Any],
    after_session: dict[str, Any],
    candidate: dict[str, Any],
    options: dict[str, Any],
    completion_receipt: dict[str, Any],
) -> dict[str, Any]:
    _require(before_project == after_project, "nonmaterial-detour-mutated-project-state")
    _require(before_session == after_session, "nonmaterial-detour-mutated-session-or-binding-state")
    result = validate_surface(candidate, after_project, after_session, options, completion_receipt)
    _require(result["primary_count"] == 1, "preserved-human-next-step-must-be-rerendered")
    return {
        "schema": "cerebro-human-navigation-detour-validation/v1",
        "result": "PASS",
        "binding_preserved": True,
        "same_trigger_rerendered": True,
        "project_and_session_state_unchanged": True,
    }


def _fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    project, _ = bootstrap_project_state(
        aggregate_id="AGG-HNS",
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        project_ref="TOTAL_MCP_REVISION",
        source_revision="fixture",
        event_id="E0",
        decision_ref="D0",
        root={
            "context_id": "CTX-ROOT",
            "human_label": "Hovedspor",
            "objective_ref": "OBJ-ROOT",
            "scope_ref": "SCOPE-ROOT",
            "basis_refs": ["BASIS-ROOT"],
            "project_basis_ref": "PB-1",
            "quality_trace_ref": "QT-DEEP",
            "completion_criteria_refs": ["DONE"],
        },
    )
    session = bind_control_session(
        project,
        session_binding_id="SESSION-1",
        principal_ref="USER-1",
        consumer_ref="CHATGPT",
        session_ref="CHAT-1",
    )
    directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "E1",
        "decision_ref": "D1",
        "expected_project_revision": project["revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"],
        "expected_session_fingerprint": session["fingerprint"],
        "project_operations": [],
        "session_operations": [
            {
                "operation": "SET_CONTINUATION_BINDING",
                "binding": {
                    "binding_id": "BIND-ROOT",
                    "surface_kind": "HNS",
                    "alias": "Fortsett hovedsporet nå",
                    "operation": "CONTINUE_CURRENT",
                    "target_ref": "CTX-ROOT",
                    "context_ref": "CTX-ROOT",
                },
            }
        ],
    }
    project, session, _ = apply_transition(project, session, directive)
    noop = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "E-HNS-COMMIT",
        "decision_ref": "MCPD-HNS-SELFTEST",
        "expected_project_revision": project["revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"],
        "expected_session_fingerprint": session["fingerprint"],
        "project_operations": [],
        "session_operations": [],
    }
    project, session, receipt = apply_transition(project, session, noop)
    return project, session, receipt


def _options(
    project: dict[str, Any],
    session: dict[str, Any],
    receipt: dict[str, Any],
    optional: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    binding = session["active_continuation_binding"]
    primary = {
        "action_id": "ACTION-CONTINUE-ROOT",
        "surface_kind": "HNS",
        "binding_id": binding["binding_id"],
        "alias": binding["alias"],
        "operation": binding["operation"],
        "target_ref": binding["target_ref"],
        "approved_by_mcp": True,
    }
    value = {
        "schema": OPTIONS_SCHEMA,
        "authority": "MCP",
        "state_basis": "COMMITTED_STATE",
        "render_precondition": HNS_RENDER_PRECONDITION,
        "commit_verified": True,
        "commit_receipt_ref": receipt["receipt_id"],
        "commit_receipt_fingerprint": receipt["receipt_fingerprint"],
        "control_decision_ref": "MCPD-HNS-SELFTEST",
        "project_ref": project["project_ref"],
        "session_ref": session["session_ref"],
        "source_context_ref": session["active_context_ref"],
        "project_revision": project["revision"],
        "session_revision": session["session_revision"],
        "project_fingerprint": project["fingerprint"],
        "session_fingerprint": session["fingerprint"],
        "primary": primary,
        "optional": copy.deepcopy(optional or []),
        "options_fingerprint": "",
    }
    value["options_fingerprint"] = _fingerprint(value, "options_fingerprint")
    return value


def _candidate(project: dict[str, Any], session: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    primary = copy.deepcopy(options["primary"])
    optional = copy.deepcopy(options["optional"])
    optional_text = "".join(f"- {action['alias']}\n" for action in optional)
    body = "Spørsmålet er besvart."
    if optional_text:
        body += "\n\nAndre gyldige valg:\n" + optional_text.rstrip()
    return {
        "schema": CANDIDATE_SCHEMA,
        "boundary": "HNS",
        "human_action_is_next": True,
        "machine_action_pending": False,
        "project_ref": project["project_ref"],
        "session_ref": session["session_ref"],
        "project_revision": project["revision"],
        "session_revision": session["session_revision"],
        "project_fingerprint": project["fingerprint"],
        "session_fingerprint": session["fingerprint"],
        "primary": primary,
        "optional": optional,
        "mcp_approved_action_refs": [action["action_id"] for action in [primary, *optional]],
        "response_text": f"{body}\n\n```text\n{primary['alias']}\n```",
    }


def _rejects(
    candidate: dict[str, Any],
    project: dict[str, Any] | None,
    session: dict[str, Any] | None,
    options: dict[str, Any] | None = None,
    completion_receipt: dict[str, Any] | None = None,
) -> bool:
    try:
        validate_surface(candidate, project, session, options, completion_receipt)
    except HumanNavigationSurfaceError:
        return True
    return False


def selftest() -> dict[str, Any]:
    project, session, receipt = _fixture()
    options = _options(project, session, receipt)
    candidate = _candidate(project, session, options)
    tests: list[dict[str, str]] = []

    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})

    check("R39-one-primary-when-human-action-is-next", validate_surface(candidate, project, session, options, receipt)["primary_count"] == 1)
    check("R52-detour-preserves-and-rerenders", validate_detour_continuity(project, session, project, session, candidate, options, receipt)["same_trigger_rerendered"] is True)
    stale = copy.deepcopy(candidate)
    stale["session_revision"] -= 1
    check("R42-stale-trigger-state-rejected", _rejects(stale, project, session, options, receipt))
    stale_options = copy.deepcopy(options)
    stale_options["session_revision"] -= 1
    stale_options["options_fingerprint"] = _fingerprint(stale_options, "options_fingerprint")
    check("R42-stale-MCP-option-projection-rejected", _rejects(candidate, project, session, stale_options, receipt))
    precommit_candidate = copy.deepcopy(options)
    precommit_candidate.update(
        schema=OPTIONS_CANDIDATE_SCHEMA,
        state_basis="PREDICTED_POST_COMMIT_STATE",
        render_authorized=False,
        activation_precondition=HNS_CANDIDATE_ACTIVATION_PRECONDITION,
        expected_transition_receipt_ref=precommit_candidate.pop("commit_receipt_ref"),
        expected_transition_receipt_fingerprint=precommit_candidate.pop("commit_receipt_fingerprint"),
    )
    precommit_candidate.pop("render_precondition", None)
    precommit_candidate.pop("commit_verified", None)
    precommit_candidate.pop("options_fingerprint", None)
    precommit_candidate["candidate_fingerprint"] = _fingerprint(precommit_candidate, "candidate_fingerprint")
    check(
        "R42-precommit-option-candidate-validates-but-cannot-render",
        validate_navigation_options_candidate(precommit_candidate, project, session, receipt)["render_authorized"] is False
        and _rejects(candidate, project, session, precommit_candidate, receipt),
    )
    check("R42-missing-completion-receipt-cannot-render", _rejects(candidate, project, session, options, None))
    invented = copy.deepcopy(candidate)
    invented["primary"]["action_id"] = "ACTION-INVENTED"
    invented["mcp_approved_action_refs"] = ["ACTION-INVENTED"]
    check("R41-presentation-invented-action-rejected", _rejects(invented, project, session, options, receipt))
    too_many_actions = [
        {
            "action_id": f"O{i}",
            "surface_kind": "HNS",
            "alias": f"Velg gren {i}",
            "operation": "SWITCH_CONTEXT",
            "target_ref": "CTX-ROOT",
            "approved_by_mcp": True,
        }
        for i in range(4)
    ]
    too_many_options = _options(project, session, receipt, too_many_actions)
    too_many = _candidate(project, session, too_many_options)
    check("R40-optional-maximum-three", _rejects(too_many, project, session, too_many_options, receipt))
    pause_action = {
        "action_id": "ACTION-PAUSE",
        "surface_kind": "HNS",
        "alias": "Pause denne grenen",
        "operation": "PAUSE",
        "target_ref": "CTX-ROOT",
        "approved_by_mcp": True,
    }
    optional_options = _options(project, session, receipt, [pause_action])
    optional_candidate = _candidate(project, session, optional_options)
    check(
        "R40-one-optional-MCP-derived-action-is-rendered-before-primary",
        validate_surface(optional_candidate, project, session, optional_options, receipt)["optional_count"] == 1,
    )
    machine = {
        "schema": CANDIDATE_SCHEMA,
        "boundary": "MACHINE_CONTINUES",
        "human_action_is_next": False,
        "machine_action_pending": True,
        "primary": None,
        "optional": [],
        "mcp_approved_action_refs": [],
        "response_text": "Arbeidet fortsetter.",
    }
    check("R43-machine-work-suppresses-hns", validate_surface(machine)["hns_suppressed"] is True)
    hcs = copy.deepcopy(machine)
    hcs.update(boundary="HCS", human_action_is_next=True, machine_action_pending=False)
    check("R44-hcs-precedes-hns", validate_surface(hcs)["hns_suppressed"] is True)
    shell = copy.deepcopy(hcs)
    shell["boundary"] = "EXACT_SHELL"
    check("R45-shell-handoff-suppresses-hns", validate_surface(shell)["hns_suppressed"] is True)
    boot = copy.deepcopy(machine)
    boot.update(boundary="BOOT_NO_PROJECT", machine_action_pending=False, response_text="Cerebro er klar.")
    check("R46-boot-no-project-needs-no-second-command", validate_surface(boot)["primary_count"] == 0)
    nonterminal = copy.deepcopy(candidate)
    nonterminal["response_text"] += "\nMer tekst"
    check("primary-trigger-must-be-absolute-response-end", _rejects(nonterminal, project, session, options, receipt))
    false_hns = copy.deepcopy(candidate)
    false_hns["human_action_is_next"] = False
    check("HNS-cannot-exist-without-human-action-next", _rejects(false_hns, project, session, options, receipt))
    return {
        "schema": "cerebro-human-navigation-surface-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--candidate", required=True)
    p_validate.add_argument("--project")
    p_validate.add_argument("--session")
    p_validate.add_argument("--options")
    p_validate.add_argument("--completion-receipt")
    p_validate.add_argument("--output")
    p_selftest = sub.add_parser("selftest")
    p_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            project = _read_json(Path(args.project)) if args.project else None
            session = _read_json(Path(args.session)) if args.session else None
            options = _read_json(Path(args.options)) if args.options else None
            completion_receipt = _read_json(Path(args.completion_receipt)) if args.completion_receipt else None
            result = validate_surface(_read_json(Path(args.candidate)), project, session, options, completion_receipt)
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
