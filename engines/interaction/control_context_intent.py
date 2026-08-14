#!/usr/bin/env python3
"""State-bound Interaction assessment for hierarchical project-control events.

The module resolves human meaning and state-backed selectors into a derived
assessment.  It deliberately has no mutation port and cannot authorize a control
transition; the canonical MCP resolver remains the only control owner.
"""

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
CONTEXT_TOOLING = SOURCE_ROOT / "tooling" / "context"
if str(CONTEXT_TOOLING) not in sys.path:
    sys.path.insert(0, str(CONTEXT_TOOLING))

from control_context_registry import (  # noqa: E402
    DIRECTIVE_SCHEMA,
    apply_transition,
    bind_control_session,
    bootstrap_project_state,
    validate_project_state,
    validate_session_state,
)


ASSESSMENT_SCHEMA = "cerebro-control-context-intent-assessment/v1"
NAVIGATION_SCHEMA = "cerebro-navigation-trigger-assessment/v1"
PROJECT_RELATIONS = {"ACTIVE_PROJECT", "NEW_PROJECT", "NONPROJECT", "CROSS_PROJECT", "UNRESOLVED"}
INTENT_CANDIDATES = {
    "CONTINUE_CURRENT",
    "QUESTION_OR_OBSERVATION",
    "FORK_CANDIDATE",
    "SWITCH_REQUEST",
    "PAUSE_REQUEST",
    "RESUME_REQUEST",
    "RETURN_REQUEST",
    "CONSOLIDATE_REQUEST",
    "CANCEL_REQUEST",
    "OBJECTIVE_CHANGE",
}
MATERIALITY = {"MATERIAL", "NONMATERIAL", "UNKNOWN"}
EXPLICITNESS = {"EXPLICIT", "INFERRED"}
ROUTES = {"HANDLE_INLINE", "TRANSIENT_DETOUR", "CREATE_CHILD", "CONTROL_TRANSITION"}
SELECTOR_KINDS = {"AUTO", "CONTEXT_REF", "CONTEXT_ALIAS", "TOPIC", "MARKER"}
FORK_JUSTIFICATIONS = {
    "BOUNDED_DISTINCT_OBJECTIVE",
    "MULTISTEP_CONTINUITY_USEFUL",
    "INDEPENDENT_ANALYSIS_PATH_USEFUL",
    "OWN_EVIDENCE_PATH_USEFUL",
    "OWN_RECOVERY_SCOPE_USEFUL",
    "SEPARATION_IMPROVES_REASONING",
}


class ControlContextIntentError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlContextIntentError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _fingerprint(value: dict[str, Any], field: str) -> str:
    subject = copy.deepcopy(value)
    subject.pop(field, None)
    return hashlib.sha256(_canonical(subject)).hexdigest()


def _text(value: Any, field: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{field}-required")
    return " ".join(value.strip().split())


def _human_key(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _context_mapping(project: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["context_id"]: item for item in project["contexts"]}


def _root_ref(project: dict[str, Any]) -> str:
    return next(item["context_id"] for item in project["contexts"] if item["parent_context_ref"] is None)


def resolve_context_selector(
    selector: dict[str, Any],
    project: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    """Resolve only state-backed context selectors; topics and markers stay semantic."""

    _require(isinstance(selector, dict), "selector-object-required")
    kind = selector.get("kind", "AUTO")
    _require(kind in SELECTOR_KINDS, "selector-kind-invalid")
    value = _text(selector.get("value"), "selector-value")
    if kind in {"TOPIC", "MARKER"}:
        return {"kind": kind, "value": value, "resolution": "SEMANTIC_SELECTOR"}

    mapping = _context_mapping(project)
    key = _human_key(value)
    matches: list[str] = []
    if kind in {"AUTO", "CONTEXT_REF"} and value in mapping:
        matches = [value]
    elif kind == "CONTEXT_REF":
        matches = []
    else:
        special: str | None = None
        if key in {"aktiv kontekst", "gjeldende kontekst", "current context", "active context"}:
            special = session["active_context_ref"]
        elif key in {"hovedsporet", "hovedspor", "root", "root context", "main path"}:
            special = _root_ref(project)
        elif key in {"forelder", "parent", "parent context"}:
            special = mapping[session["active_context_ref"]].get("parent_context_ref")
        if special is not None:
            matches = [special]
        else:
            matches = [
                item["context_id"]
                for item in project["contexts"]
                if _human_key(item["human_label"]) == key
            ]

    if len(matches) == 1:
        context = mapping[matches[0]]
        return {
            "kind": "CONTROL_CONTEXT",
            "value": value,
            "resolution": "RESOLVED",
            "project_ref": project["project_ref"],
            "context_ref": context["context_id"],
            "context_fingerprint": context["context_fingerprint"],
            "lifecycle": context["lifecycle"],
        }
    return {
        "kind": "CONTROL_CONTEXT_CANDIDATE",
        "value": value,
        "resolution": "AMBIGUOUS" if len(matches) > 1 else "UNRESOLVED",
        "candidate_context_refs": sorted(matches),
    }


def resolve_navigation_trigger(
    utterance: str,
    project: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    """Match a visible phrase to committed state without treating it as state."""

    validate_session_state(session, project)
    normalized = _human_key(_text(utterance, "navigation-utterance"))
    binding = session.get("active_continuation_binding")
    if not isinstance(binding, dict) or _human_key(binding["alias"]) != normalized:
        return {
            "schema": NAVIGATION_SCHEMA,
            "message_kind": "NAVIGATION_TRIGGER_ASSESSMENT",
            "producer_ref": "interaction",
            "authority": "DERIVED_ASSESSMENT",
            "result": "UNRESOLVED",
            "visible_phrase_is_machine_state": False,
            "state_mutation_authorized": False,
        }
    result = {
        "schema": NAVIGATION_SCHEMA,
        "message_kind": "NAVIGATION_TRIGGER_ASSESSMENT",
        "producer_ref": "interaction",
        "authority": "DERIVED_ASSESSMENT",
        "result": "MATCH",
        "project_ref": project["project_ref"],
        "session_ref": session["session_ref"],
        "project_revision": project["revision"],
        "session_revision": session["session_revision"],
        "project_fingerprint": project["fingerprint"],
        "session_fingerprint": session["fingerprint"],
        "binding_id": binding["binding_id"],
        "operation_candidate": binding["operation"],
        "target_ref": binding["target_ref"],
        "visible_phrase_is_machine_state": False,
        "state_mutation_authorized": False,
        "requires_canonical_mcp_resolution": True,
    }
    result["assessment_fingerprint"] = _fingerprint(result, "assessment_fingerprint")
    return result


def validate_control_context_intent_assessment(
    assessment: dict[str, Any],
    project: dict[str, Any],
    session: dict[str, Any],
    event_ref: str,
) -> dict[str, Any]:
    """Validate a fingerprinted assessment against the committed event binding."""

    validate_session_state(session, project)
    _require(assessment.get("schema") == ASSESSMENT_SCHEMA, "intent-assessment-schema-mismatch")
    _require(assessment.get("message_kind") == "CONTROL_CONTEXT_INTENT_ASSESSMENT", "intent-assessment-message-kind-mismatch")
    _require(assessment.get("producer_ref") == "interaction", "intent-assessment-producer-mismatch")
    _require(assessment.get("authority") == "DERIVED_ASSESSMENT", "intent-assessment-authority-invalid")
    _require(assessment.get("subject_ref") == "control-event", "intent-assessment-subject-invalid")
    _require(assessment.get("correlation_ref") == event_ref, "intent-assessment-event-correlation-mismatch")
    _require(assessment.get("project_relation") in PROJECT_RELATIONS, "intent-assessment-project-relation-invalid")
    _require(assessment.get("intent_candidate") in INTENT_CANDIDATES, "intent-assessment-intent-invalid")
    _require(assessment.get("materiality_hint") in MATERIALITY, "intent-assessment-materiality-invalid")
    _require(assessment.get("explicitness") in EXPLICITNESS, "intent-assessment-explicitness-invalid")
    _require(assessment.get("route_candidate") in ROUTES, "intent-assessment-route-invalid")
    _require(assessment.get("state_mutation_authorized") is False, "interaction-cannot-authorize-state-mutation")
    _require(assessment.get("requires_canonical_mcp_resolution") is True, "project-intent-requires-MCP-resolution")
    for field, expected in (
        ("project_ref", project["project_ref"]),
        ("session_ref", session["session_ref"]),
        ("active_context_ref", session["active_context_ref"]),
        ("project_fingerprint", project["fingerprint"]),
        ("session_fingerprint", session["fingerprint"]),
    ):
        _require(assessment.get(field) == expected, f"intent-assessment-{field}-stale")
    _require(isinstance(assessment.get("target_selectors"), list), "intent-assessment-target-selectors-required")
    _require(isinstance(assessment.get("operation_candidates"), list), "intent-assessment-operation-candidates-required")
    _require(
        assessment.get("assessment_fingerprint") == _fingerprint(assessment, "assessment_fingerprint"),
        "intent-assessment-fingerprint-mismatch",
    )
    return {
        "result": "PASS",
        "event_ref": event_ref,
        "intent_candidate": assessment["intent_candidate"],
        "route_candidate": assessment["route_candidate"],
        "assessment_fingerprint": assessment["assessment_fingerprint"],
    }


def assess_control_context_intent(
    candidate: dict[str, Any],
    project: dict[str, Any] | None,
    session: dict[str, Any] | None,
) -> dict[str, Any]:
    """Finalize one human-aligned, non-authoritative Interaction assessment."""

    _require(isinstance(candidate, dict), "intent-candidate-object-required")
    event_ref = _text(candidate.get("event_ref"), "event-ref")
    project_relation = candidate.get("project_relation")
    intent = candidate.get("intent_candidate")
    materiality = candidate.get("materiality_hint", "UNKNOWN")
    explicitness = candidate.get("explicitness", "INFERRED")
    _require(project_relation in PROJECT_RELATIONS, "project-relation-invalid")
    _require(intent in INTENT_CANDIDATES, "intent-candidate-invalid")
    _require(materiality in MATERIALITY, "materiality-hint-invalid")
    _require(explicitness in EXPLICITNESS, "explicitness-invalid")

    project_bound = project_relation in {"ACTIVE_PROJECT", "CROSS_PROJECT"}
    if project_bound:
        _require(project is not None and session is not None, "project-bound-intent-requires-validated-binding")
        validate_project_state(project)
        validate_session_state(session, project)
    elif project is not None or session is not None:
        _require(project is not None and session is not None, "partial-project-binding-prohibited")
        validate_session_state(session, project)

    selectors = candidate.get("target_selectors", [])
    _require(isinstance(selectors, list) and all(isinstance(item, dict) for item in selectors), "target-selectors-array-required")
    resolved = [resolve_context_selector(item, project, session) for item in selectors] if project_bound else copy.deepcopy(selectors)
    unresolved = [item for item in resolved if item.get("resolution") in {"AMBIGUOUS", "UNRESOLVED"}]
    clarification_required = bool(unresolved) and intent in {
        "SWITCH_REQUEST", "RETURN_REQUEST", "CONSOLIDATE_REQUEST", "CANCEL_REQUEST"
    }

    human_meaning = candidate.get("human_meaning", {})
    _require(isinstance(human_meaning, dict), "human-meaning-object-required")
    speech_to_text_observed = human_meaning.get("speech_to_text_observed", False)
    objective_delta = human_meaning.get("material_objective_delta", False)
    _require(isinstance(speech_to_text_observed, bool), "speech-to-text-observed-boolean-required")
    _require(isinstance(objective_delta, bool), "material-objective-delta-boolean-required")

    route = "CONTROL_TRANSITION"
    fork_reasons = candidate.get("fork_justifications", [])
    _require(isinstance(fork_reasons, list), "fork-justifications-array-required")
    _require(all(reason in FORK_JUSTIFICATIONS for reason in fork_reasons), "fork-justification-invalid")
    if intent == "QUESTION_OR_OBSERVATION":
        route = "CONTROL_TRANSITION" if objective_delta else (
            "TRANSIENT_DETOUR" if materiality == "NONMATERIAL" or speech_to_text_observed else "HANDLE_INLINE"
        )
    elif intent == "CONTINUE_CURRENT":
        route = "HANDLE_INLINE"
    elif intent == "FORK_CANDIDATE":
        route = "CREATE_CHILD" if materiality == "MATERIAL" and bool(fork_reasons) else "HANDLE_INLINE"
    _require(route in ROUTES, "route-invalid")
    if clarification_required:
        route = "CONTROL_TRANSITION"

    active_context_ref = session["active_context_ref"] if isinstance(session, dict) else None
    operation_candidates: list[dict[str, Any]] = []
    resolved_contexts = [item for item in resolved if item.get("resolution") == "RESOLVED"]
    target_ref = resolved_contexts[0]["context_ref"] if len(resolved_contexts) == 1 else active_context_ref
    if not clarification_required and project_bound:
        if intent == "SWITCH_REQUEST" and target_ref:
            _require(_context_mapping(project)[target_ref]["lifecycle"] == "OPEN", "switch-target-must-be-open")
            operation_candidates.append({"operation": "SET_ACTIVE", "context_ref": target_ref})
        elif intent == "PAUSE_REQUEST":
            operation_candidates.append({"operation": "SET_CONTROL_CONDITION", "context_ref": active_context_ref, "control_condition": "PAUSED_BY_USER"})
        elif intent == "RESUME_REQUEST":
            operation_candidates.append({"operation": "SET_CONTROL_CONDITION", "context_ref": target_ref, "control_condition": "READY"})
        elif intent == "CANCEL_REQUEST":
            operation_candidates.append({"operation": "CANCEL_CONTEXT", "context_ref": target_ref})
        elif intent == "RETURN_REQUEST":
            operation_candidates.append({"operation": "RETURN_CONTEXT_REQUEST", "context_ref": target_ref})
        elif intent == "CONSOLIDATE_REQUEST":
            operation_candidates.append({
                "operation": "CONSOLIDATION_REQUEST",
                "selected_context_refs": [item["context_ref"] for item in resolved_contexts],
            })

    assessment: dict[str, Any] = {
        "schema": ASSESSMENT_SCHEMA,
        "message_kind": "CONTROL_CONTEXT_INTENT_ASSESSMENT",
        "producer_ref": "interaction",
        "authority": "DERIVED_ASSESSMENT",
        "subject_ref": "control-event",
        "correlation_ref": event_ref,
        "project_relation": project_relation,
        "intent_candidate": intent,
        "target_selectors": resolved,
        "materiality_hint": materiality,
        "explicitness": explicitness,
        "route_candidate": route,
        "operation_candidates": operation_candidates,
        "clarification_required": clarification_required,
        "clarification_reason": "MATERIAL_SELECTOR_AMBIGUITY" if clarification_required else None,
        "human_meaning": {
            "speech_to_text_observed": speech_to_text_observed,
            "material_objective_delta": objective_delta,
            "objective_preserved": not objective_delta,
        },
        "state_mutation_authorized": False,
        "requires_canonical_mcp_resolution": project_bound,
        "project_ref": project.get("project_ref") if isinstance(project, dict) else None,
        "session_ref": session.get("session_ref") if isinstance(session, dict) else None,
        "active_context_ref": active_context_ref,
        "project_fingerprint": project.get("fingerprint") if isinstance(project, dict) else None,
        "session_fingerprint": session.get("fingerprint") if isinstance(session, dict) else None,
        "assessment_fingerprint": "",
    }
    assessment["assessment_fingerprint"] = _fingerprint(assessment, "assessment_fingerprint")
    return assessment


def _fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    project, _ = bootstrap_project_state(
        aggregate_id="AGG-INTENT",
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        project_ref="TOTAL_MCP_REVISION",
        source_revision="fixture",
        event_id="E0",
        decision_ref="D0",
        root={
            "context_id": "CTX-ROOT", "human_label": "Hovedspor total revisjon",
            "objective_ref": "OBJ-ROOT", "scope_ref": "SCOPE-ROOT", "basis_refs": ["BASIS-ROOT"],
            "project_basis_ref": "PB-1", "quality_trace_ref": "QT-1", "completion_criteria_refs": ["DONE"],
        },
    )
    session = bind_control_session(
        project, session_binding_id="SB-1", principal_ref="USER-1", consumer_ref="CHATGPT", session_ref="CHAT-1"
    )
    directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "E1", "decision_ref": "D1",
        "expected_project_revision": project["revision"], "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"], "expected_session_fingerprint": session["fingerprint"],
        "project_operations": [{
            "operation": "CREATE_CHILD", "parent_context_ref": "CTX-ROOT", "context_id": "CTX-ARCH",
            "human_label": "Universell state arkitektur", "objective_ref": "OBJ-ARCH", "scope_ref": "SCOPE-ARCH",
            "basis_refs": ["BASIS-ARCH"], "project_basis_ref": "PB-1", "quality_trace_ref": "QT-1",
            "completion_criteria_refs": ["DONE-ARCH"],
        }],
        "session_operations": [{
            "operation": "SET_CONTINUATION_BINDING",
            "binding": {
                "binding_id": "BIND-ROOT", "surface_kind": "HNS", "alias": "Fortsett hovedsporet nå",
                "operation": "CONTINUE_CURRENT", "target_ref": "CTX-ROOT", "context_ref": "CTX-ROOT",
            },
        }],
    }
    project, session, _ = apply_transition(project, session, directive)
    return project, session


def selftest() -> dict[str, Any]:
    project, session = _fixture()
    tests: list[dict[str, str]] = []

    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})

    base = {
        "event_ref": "EVENT-1", "project_relation": "ACTIVE_PROJECT", "target_selectors": [],
        "explicitness": "INFERRED", "human_meaning": {"speech_to_text_observed": False, "material_objective_delta": False},
    }
    detour = assess_control_context_intent(
        {**base, "intent_candidate": "QUESTION_OR_OBSERVATION", "materiality_hint": "NONMATERIAL"}, project, session
    )
    check("R06-ordinary-question-is-nonmutating-detour", detour["route_candidate"] == "TRANSIENT_DETOUR" and not detour["operation_candidates"])
    stt = assess_control_context_intent(
        {**base, "intent_candidate": "QUESTION_OR_OBSERVATION", "materiality_hint": "UNKNOWN", "human_meaning": {"speech_to_text_observed": True, "material_objective_delta": False}},
        project, session,
    )
    check("R07-speech-to-text-does-not-imply-objective-change", stt["route_candidate"] == "TRANSIENT_DETOUR" and stt["human_meaning"]["objective_preserved"] is True)
    fork = assess_control_context_intent(
        {**base, "intent_candidate": "FORK_CANDIDATE", "materiality_hint": "MATERIAL", "fork_justifications": ["MULTISTEP_CONTINUITY_USEFUL"]},
        project, session,
    )
    check("R08-material-fork-is-only-an-assessment", fork["route_candidate"] == "CREATE_CHILD" and fork["state_mutation_authorized"] is False)
    trivial_fork = assess_control_context_intent(
        {**base, "intent_candidate": "FORK_CANDIDATE", "materiality_hint": "NONMATERIAL", "fork_justifications": []}, project, session
    )
    check("R12-trivial-fork-stays-inline", trivial_fork["route_candidate"] == "HANDLE_INLINE")
    switch = assess_control_context_intent(
        {**base, "intent_candidate": "SWITCH_REQUEST", "materiality_hint": "MATERIAL", "explicitness": "EXPLICIT", "target_selectors": [{"kind": "CONTEXT_ALIAS", "value": "Universell state arkitektur"}]},
        project, session,
    )
    check("R13-state-backed-alias-resolves-switch-candidate", switch["operation_candidates"] == [{"operation": "SET_ACTIVE", "context_ref": "CTX-ARCH"}])
    nav = resolve_navigation_trigger("Fortsett hovedsporet nå", project, session)
    check("R41-visible-trigger-resolves-from-committed-binding", nav["result"] == "MATCH" and nav["visible_phrase_is_machine_state"] is False)
    check("unrecognized-visible-phrase-does-not-reconstruct-state", resolve_navigation_trigger("Fortsett noe annet", project, session)["result"] == "UNRESOLVED")
    missing_blocked = False
    try:
        assess_control_context_intent({**base, "intent_candidate": "CONTINUE_CURRENT", "materiality_hint": "NONMATERIAL"}, None, None)
    except ControlContextIntentError:
        missing_blocked = True
    check("R03-project-intent-requires-binding-before-interpretation", missing_blocked)
    tampered = copy.deepcopy(detour)
    tampered["route_candidate"] = "CREATE_CHILD"
    tamper_blocked = False
    try:
        validate_control_context_intent_assessment(tampered, project, session, "EVENT-1")
    except ControlContextIntentError:
        tamper_blocked = True
    check("fingerprinted-intent-assessment-rejects-tampering", tamper_blocked)
    return {
        "schema": "cerebro-control-context-intent-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["selftest"])
    args = parser.parse_args()
    result = selftest() if args.command == "selftest" else {"result": "BLOCK"}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
