#!/usr/bin/env python3
"""Interaction-owned orchestration for Context-selected CONSOLIDATE results.

This extends the existing CONSOLIDATE workform without creating a new engine or
turning synthesis into a control decision.  Context supplies validated snapshots;
Interaction emits a fingerprinted derived assessment for canonical MCP routing.
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
    ancestor_chain,
    apply_transition,
    bind_control_session,
    bootstrap_project_state,
    lowest_common_ancestor,
    validate_project_state,
)


REQUEST_SCHEMA = "cerebro-consolidation-request/context-extension-v1"
RESULT_SCHEMA = "cerebro-context-consolidation-result/v1"
SCOPE_SCHEMA = "cerebro-consolidation-scope-resolution/v1"
TARGET_KINDS = {"AUTO", "SEMANTIC", "CONTROL_CONTEXTS"}
SELECTOR_KINDS = {"AUTO", "CONTEXT_REF", "CONTEXT_ALIAS", "TOPIC", "MARKER"}
DISPOSITION_CANDIDATES = {"INCORPORATED", "PRESERVED", "SUPERSEDED", "PENDING_JOIN"}
EFFECT_CANDIDATES = {
    "CONTEXT_ENRICHMENT",
    "PROJECT_REVISION_REQUIRED",
    "QUALITY_INVALIDATION_REQUIRED",
    "CONVERGENCE_REVALIDATION_REQUIRED",
    "HUMAN_DECISION_REQUIRED",
}


class ContextConsolidationError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContextConsolidationError(message)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _fingerprint(value: dict[str, Any], *fields: str) -> str:
    subject = copy.deepcopy(value)
    for field in fields:
        subject.pop(field, None)
    return hashlib.sha256(_canonical(subject)).hexdigest()


def _text(value: Any, field: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{field}-required")
    return " ".join(value.strip().split())


def _refs(value: Any, field: str) -> list[str]:
    _require(isinstance(value, list), f"{field}-array-required")
    refs = [_text(item, field) for item in value]
    _require(len(refs) == len(set(refs)), f"{field}-duplicate")
    return refs


def _project_index(projects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    _require(isinstance(projects, list), "projects-array-required")
    result: dict[str, dict[str, Any]] = {}
    for project in projects:
        validate_project_state(project)
        ref = project["project_ref"]
        _require(ref not in result, f"duplicate-project-ref:{ref}")
        result[ref] = project
    return result


def _all_context_matches(
    selector: dict[str, Any], projects: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    kind = selector.get("kind", "AUTO")
    _require(kind in SELECTOR_KINDS, "consolidation-selector-kind-invalid")
    value = _text(selector.get("value"), "consolidation-selector-value")
    project_filter = selector.get("project_ref")
    if project_filter is not None:
        project_filter = _text(project_filter, "selector-project-ref")
        _require(project_filter in projects, "selector-project-not-found")
    if kind in {"TOPIC", "MARKER"}:
        return []
    matches: list[dict[str, str]] = []
    key = value.casefold()
    for project_ref, project in projects.items():
        if project_filter is not None and project_ref != project_filter:
            continue
        for context in project["contexts"]:
            exact_ref = value == context["context_id"]
            exact_alias = key == context["human_label"].casefold()
            if (kind == "CONTEXT_REF" and exact_ref) or (kind == "CONTEXT_ALIAS" and exact_alias) or (
                kind == "AUTO" and (exact_ref or exact_alias)
            ):
                matches.append({"project_ref": project_ref, "context_ref": context["context_id"]})
    return matches


def resolve_consolidation_scope(
    request: dict[str, Any], projects: list[dict[str, Any]]
) -> dict[str, Any]:
    """Route plain CONSOLIDATE compatibly and resolve explicit Context selectors."""

    _require(isinstance(request, dict), "consolidation-request-object-required")
    _require(request.get("schema") == REQUEST_SCHEMA, "consolidation-request-schema-mismatch")
    event_ref = _text(request.get("event_ref"), "consolidation-event-ref")
    target_kind = request.get("target_kind", "AUTO")
    _require(target_kind in TARGET_KINDS, "consolidation-target-kind-invalid")
    selectors = request.get("selectors", [])
    _require(isinstance(selectors, list) and all(isinstance(item, dict) for item in selectors), "consolidation-selectors-array-required")
    structural = request.get("structural_join_requested", False)
    _require(isinstance(structural, bool), "structural-join-requested-boolean-required")
    index = _project_index(projects)

    if target_kind in {"AUTO", "SEMANTIC"} and not selectors and not structural:
        return {
            "schema": SCOPE_SCHEMA,
            "result": "PASS",
            "mode": "LEGACY_SEMANTIC_CONSOLIDATE",
            "event_ref": event_ref,
            "delegate_contract_ref": "standards/development/consolidate.yaml",
            "selected_contexts": [],
            "clarification_required": False,
            "structural_join_requested": False,
        }

    selected: list[dict[str, str]] = []
    semantic_selectors: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for selector in selectors:
        kind = selector.get("kind", "AUTO")
        if kind in {"TOPIC", "MARKER"}:
            semantic_selectors.append(copy.deepcopy(selector))
            continue
        matches = _all_context_matches(selector, index)
        if len(matches) == 1:
            selected.extend(matches)
        else:
            ambiguous.append({
                "selector": copy.deepcopy(selector),
                "reason": "AMBIGUOUS" if len(matches) > 1 else "UNRESOLVED",
                "candidate_contexts": matches,
            })

    identities = [(item["project_ref"], item["context_ref"]) for item in selected]
    _require(len(identities) == len(set(identities)), "duplicate-selected-context")
    mixed_semantic_and_context = bool(selected) and bool(semantic_selectors) and target_kind == "AUTO"
    clarification = bool(ambiguous) or mixed_semantic_and_context
    if target_kind == "CONTROL_CONTEXTS":
        _require(not semantic_selectors, "explicit-context-consolidation-cannot-contain-semantic-selector")
        _require(bool(selected) or clarification, "context-consolidation-requires-selected-context")
        mode = "CONTROL_CONTEXT_CONSOLIDATE"
    elif selected and not semantic_selectors:
        mode = "CONTROL_CONTEXT_CONSOLIDATE"
    elif semantic_selectors and not selected:
        mode = "LEGACY_SEMANTIC_CONSOLIDATE"
    else:
        mode = "UNRESOLVED"

    return {
        "schema": SCOPE_SCHEMA,
        "result": "CLARIFICATION_REQUIRED" if clarification else "PASS",
        "mode": mode,
        "event_ref": event_ref,
        "delegate_contract_ref": "standards/development/consolidate.yaml" if mode == "LEGACY_SEMANTIC_CONSOLIDATE" else None,
        "selected_contexts": selected,
        "semantic_selectors": semantic_selectors,
        "ambiguities": ambiguous,
        "clarification_required": clarification,
        "structural_join_requested": structural,
    }


def _selected_snapshots(
    selected: list[dict[str, str]], index: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for item in selected:
        project = index.get(item["project_ref"])
        _require(project is not None, "selected-project-not-found")
        context = next((value for value in project["contexts"] if value["context_id"] == item["context_ref"]), None)
        _require(context is not None, "selected-context-not-found")
        snapshots.append({
            "project_ref": project["project_ref"],
            "project_revision": project["revision"],
            "project_fingerprint": project["fingerprint"],
            "context_ref": context["context_id"],
            "context_fingerprint": context["context_fingerprint"],
            "lifecycle": context["lifecycle"],
            "disposition": context["disposition"],
        })
    return snapshots


def _join_target(
    request: dict[str, Any], selected: list[dict[str, str]], index: dict[str, dict[str, Any]]
) -> tuple[str | None, str | None]:
    if not request.get("structural_join_requested", False):
        return None, None
    projects = {item["project_ref"] for item in selected}
    _require(len(projects) == 1, "cross-project-structural-join-prohibited")
    project_ref = next(iter(projects))
    project = index[project_ref]
    selected_refs = [item["context_ref"] for item in selected]
    default_target = lowest_common_ancestor(project, selected_refs)
    explicit = request.get("explicit_join_target")
    if explicit is None:
        target_ref = default_target
    else:
        _require(isinstance(explicit, dict), "explicit-join-target-object-required")
        _require(explicit.get("project_ref", project_ref) == project_ref, "explicit-join-target-project-mismatch")
        target_ref = _text(explicit.get("context_ref"), "explicit-join-target-context-ref")
        mapping = {item["context_id"]: item for item in project["contexts"]}
        _require(target_ref in mapping and mapping[target_ref]["lifecycle"] == "OPEN", "explicit-join-target-must-be-open")
        _require(
            all(target_ref in ancestor_chain(project, context_ref) for context_ref in selected_refs),
            "explicit-join-target-must-be-common-ancestor",
        )
    return project_ref, target_ref


def validate_context_consolidation_result(result: dict[str, Any]) -> dict[str, Any]:
    _require(result.get("schema") == RESULT_SCHEMA, "context-consolidation-result-schema-mismatch")
    _require(result.get("message_kind") == "CONTEXT_CONSOLIDATION_RESULT", "context-consolidation-message-kind-mismatch")
    _require(result.get("producer_ref") == "interaction", "context-consolidation-producer-mismatch")
    _require(result.get("authority") == "DERIVED_ASSESSMENT", "context-consolidation-authority-invalid")
    _require(result.get("automatic_control_decision") is False, "consolidation-cannot-be-automatic-decision")
    _require(result.get("automatic_state_mutation") is False, "consolidation-cannot-mutate-state")
    selected = result.get("selected_contexts")
    _require(isinstance(selected, list) and bool(selected), "selected-contexts-required")
    identities = [(item.get("project_ref"), item.get("context_ref")) for item in selected if isinstance(item, dict)]
    _require(len(identities) == len(selected) and len(identities) == len(set(identities)), "selected-context-identity-invalid")
    effects = result.get("effect_candidates")
    _require(isinstance(effects, list) and len(effects) == len(set(effects)), "effect-candidates-invalid")
    _require(all(value in EFFECT_CANDIDATES for value in effects), "effect-candidate-unknown")
    dispositions = result.get("branch_disposition_candidates")
    _require(isinstance(dispositions, list), "branch-disposition-candidates-array-required")
    disposition_ids: set[tuple[str, str]] = set()
    for item in dispositions:
        _require(isinstance(item, dict), "branch-disposition-candidate-object-required")
        identity = (item.get("project_ref"), item.get("context_ref"))
        _require(identity in set(identities), "branch-disposition-target-not-selected")
        _require(identity not in disposition_ids, "duplicate-branch-disposition-candidate")
        disposition_ids.add(identity)
        _require(item.get("candidate") in DISPOSITION_CANDIDATES, "branch-disposition-candidate-invalid")
        _require(item.get("application_authorized") is False, "branch-disposition-candidate-cannot-authorize")
    if result.get("structural_join_requested") is True:
        _require(isinstance(result.get("join_target_candidate_ref"), str), "structural-join-target-required")
        target_identity = (result.get("join_target_project_ref"), result.get("join_target_candidate_ref"))
        required = {identity for identity in identities if identity != target_identity}
        _require(required.issubset(disposition_ids), "structural-join-cannot-silently-drop-selected-branch")
    expected = _fingerprint(result, "result_ref", "basis_fingerprint")
    _require(result.get("basis_fingerprint") == expected, "context-consolidation-basis-fingerprint-mismatch")
    _require(result.get("result_ref") == "CCR-" + expected[:24].upper(), "context-consolidation-result-ref-mismatch")
    return {
        "result": "PASS",
        "result_ref": result["result_ref"],
        "selected_context_count": len(selected),
        "effect_candidate_count": len(effects),
        "structural_join_requested": result["structural_join_requested"],
    }


def build_context_consolidation_result(
    request: dict[str, Any],
    projects: list[dict[str, Any]],
    synthesis: dict[str, Any],
) -> dict[str, Any]:
    """Build a derived result; owner effects remain candidates until MCP routes them."""

    scope = resolve_consolidation_scope(request, projects)
    _require(scope["result"] == "PASS", "consolidation-scope-requires-clarification")
    _require(scope["mode"] == "CONTROL_CONTEXT_CONSOLIDATE", "legacy-consolidate-must-use-existing-pipeline")
    _require(isinstance(synthesis, dict), "consolidation-synthesis-object-required")
    index = _project_index(projects)
    selected = scope["selected_contexts"]
    snapshots = _selected_snapshots(selected, index)
    join_project, join_target = _join_target(request, selected, index)

    synthesis_ref = _text(synthesis.get("synthesis_ref"), "consolidation-synthesis-ref")
    evidence_refs = _refs(synthesis.get("evidence_refs", []), "consolidation-evidence-refs")
    material_conflicts = _refs(synthesis.get("material_conflicts", []), "consolidation-material-conflicts")
    effects = synthesis.get("effect_candidates", [])
    _require(isinstance(effects, list) and len(effects) == len(set(effects)), "effect-candidates-must-be-unique-array")
    _require(all(value in EFFECT_CANDIDATES for value in effects), "effect-candidate-invalid")
    raw_dispositions = synthesis.get("branch_disposition_candidates", [])
    _require(isinstance(raw_dispositions, list), "branch-disposition-candidates-array-required")
    dispositions: list[dict[str, Any]] = []
    lifecycle_by_identity = {
        (item["project_ref"], item["context_ref"]): item["lifecycle"] for item in snapshots
    }
    for item in raw_dispositions:
        _require(isinstance(item, dict), "branch-disposition-candidate-object-required")
        project_ref = _text(item.get("project_ref"), "branch-disposition-project-ref")
        context_ref = _text(item.get("context_ref"), "branch-disposition-context-ref")
        candidate = item.get("candidate")
        _require(candidate in DISPOSITION_CANDIDATES, "branch-disposition-candidate-invalid")
        identity = (project_ref, context_ref)
        _require(identity in lifecycle_by_identity, "branch-disposition-target-not-selected")
        dispositions.append({
            "project_ref": project_ref,
            "context_ref": context_ref,
            "candidate": candidate,
            "application_authorized": False,
            "requires_return_before_application": lifecycle_by_identity[identity] == "OPEN" and candidate != "PENDING_JOIN",
            "requires_current_dependent_owner_state": candidate != "PENDING_JOIN",
        })

    final_candidates = {item["candidate"] for item in dispositions}
    partial_join = bool(final_candidates.intersection({"INCORPORATED", "PRESERVED", "SUPERSEDED"})) and (
        "PENDING_JOIN" in final_candidates or bool(material_conflicts)
    )
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "message_kind": "CONTEXT_CONSOLIDATION_RESULT",
        "producer_ref": "interaction",
        "authority": "DERIVED_ASSESSMENT",
        "subject_ref": "selected-control-contexts",
        "correlation_ref": request["event_ref"],
        "event_ref": request["event_ref"],
        "selected_contexts": snapshots,
        "join_target_project_ref": join_project,
        "join_target_candidate_ref": join_target,
        "structural_join_requested": request.get("structural_join_requested", False),
        "synthesis_ref": synthesis_ref,
        "evidence_refs": evidence_refs,
        "material_conflicts": material_conflicts,
        "branch_disposition_candidates": dispositions,
        "effect_candidates": effects,
        "partial_join": partial_join,
        "automatic_control_decision": False,
        "automatic_state_mutation": False,
        "requires_canonical_mcp_owner_routing": True,
        "basis_fingerprint": "",
        "result_ref": "",
    }
    result["basis_fingerprint"] = _fingerprint(result, "result_ref", "basis_fingerprint")
    result["result_ref"] = "CCR-" + result["basis_fingerprint"][:24].upper()
    validate_context_consolidation_result(result)
    return result


def _project(project_ref: str, aggregate_id: str, root_ref: str, child_prefix: str) -> dict[str, Any]:
    project, _ = bootstrap_project_state(
        aggregate_id=aggregate_id, tenant_ref="TENANT-1", workspace_ref="WORKSPACE-1",
        project_ref=project_ref, source_revision="fixture", event_id=f"E0-{project_ref}", decision_ref=f"D0-{project_ref}",
        root={
            "context_id": root_ref, "human_label": f"Hovedspor {project_ref}", "objective_ref": f"OBJ-{root_ref}",
            "scope_ref": f"SCOPE-{root_ref}", "basis_refs": [f"BASIS-{root_ref}"], "project_basis_ref": f"PB-{project_ref}",
            "quality_trace_ref": f"QT-{project_ref}", "completion_criteria_refs": ["DONE"],
        },
    )
    session = bind_control_session(
        project, session_binding_id=f"SB-{project_ref}", principal_ref="USER-1", consumer_ref="TEST", session_ref=f"S-{project_ref}"
    )
    operations = []
    for suffix in ("B", "C"):
        context_ref = f"{child_prefix}-{suffix}"
        operations.append({
            "operation": "CREATE_CHILD", "parent_context_ref": root_ref, "context_id": context_ref,
            "human_label": f"Gren {suffix} {project_ref}", "objective_ref": f"OBJ-{context_ref}", "scope_ref": f"SCOPE-{context_ref}",
            "basis_refs": [f"BASIS-{context_ref}"], "project_basis_ref": f"PB-{project_ref}",
            "quality_trace_ref": f"QT-{project_ref}", "completion_criteria_refs": ["DONE"],
        })
    directive = {
        "schema": DIRECTIVE_SCHEMA, "event_id": f"E1-{project_ref}", "decision_ref": f"D1-{project_ref}",
        "expected_project_revision": project["revision"], "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"], "expected_session_fingerprint": session["fingerprint"],
        "project_operations": operations, "session_operations": [],
    }
    project, _, _ = apply_transition(project, session, directive)
    return project


def selftest() -> dict[str, Any]:
    project_a = _project("PROJECT-A", "AGG-A", "A", "A")
    project_x = _project("PROJECT-X", "AGG-X", "X", "X")
    tests: list[dict[str, str]] = []

    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})

    legacy_request = {"schema": REQUEST_SCHEMA, "event_ref": "E-LEGACY", "target_kind": "AUTO", "selectors": [], "structural_join_requested": False}
    legacy = resolve_consolidation_scope(legacy_request, [project_a])
    check("R23-plain-consolidate-preserves-existing-semantic-pipeline", legacy["mode"] == "LEGACY_SEMANTIC_CONSOLIDATE")

    request = {
        "schema": REQUEST_SCHEMA, "event_ref": "E-CONTEXT", "target_kind": "CONTROL_CONTEXTS",
        "selectors": [
            {"kind": "CONTEXT_REF", "project_ref": "PROJECT-A", "value": "A-B"},
            {"kind": "CONTEXT_REF", "project_ref": "PROJECT-A", "value": "A-C"},
        ],
        "structural_join_requested": True,
    }
    synthesis = {
        "synthesis_ref": "SYNTH-A", "evidence_refs": ["EV-A", "EV-B"], "material_conflicts": ["UNRESOLVED-C"],
        "branch_disposition_candidates": [
            {"project_ref": "PROJECT-A", "context_ref": "A-B", "candidate": "INCORPORATED"},
            {"project_ref": "PROJECT-A", "context_ref": "A-C", "candidate": "PENDING_JOIN"},
        ],
        "effect_candidates": ["CONTEXT_ENRICHMENT", "PROJECT_REVISION_REQUIRED", "QUALITY_INVALIDATION_REQUIRED"],
    }
    result = build_context_consolidation_result(request, [project_a], synthesis)
    check("R24-context-selection-produces-structural-result", result["join_target_candidate_ref"] == "A")
    check("R25-result-is-derived-assessment-not-decision", result["authority"] == "DERIVED_ASSESSMENT" and result["automatic_control_decision"] is False)
    check("R26-lowest-common-ancestor-is-deterministic", result["join_target_candidate_ref"] == lowest_common_ancestor(project_a, ["A-B", "A-C"]))
    check("R29-partial-join-keeps-unresolved-branch-visible", result["partial_join"] is True and any(item["candidate"] == "PENDING_JOIN" for item in result["branch_disposition_candidates"]))

    bad_target = copy.deepcopy(request)
    bad_target["explicit_join_target"] = {"project_ref": "PROJECT-A", "context_ref": "A-B"}
    rejected_bad_target = False
    try:
        build_context_consolidation_result(bad_target, [project_a], synthesis)
    except ContextConsolidationError:
        rejected_bad_target = True
    check("invalid-explicit-structural-target-blocked", rejected_bad_target)

    cross_request = {
        "schema": REQUEST_SCHEMA, "event_ref": "E-CROSS", "target_kind": "CONTROL_CONTEXTS",
        "selectors": [
            {"kind": "CONTEXT_REF", "project_ref": "PROJECT-A", "value": "A-B"},
            {"kind": "CONTEXT_REF", "project_ref": "PROJECT-X", "value": "X-B"},
        ],
        "structural_join_requested": True,
    }
    cross_synthesis = {
        "synthesis_ref": "SYNTH-CROSS", "evidence_refs": ["EV-CROSS"], "material_conflicts": [],
        "branch_disposition_candidates": [], "effect_candidates": ["HUMAN_DECISION_REQUIRED"],
    }
    cross_blocked = False
    try:
        build_context_consolidation_result(cross_request, [project_a, project_x], cross_synthesis)
    except ContextConsolidationError:
        cross_blocked = True
    check("R27-cross-project-structural-join-blocked", cross_blocked)
    cross_request["structural_join_requested"] = False
    cross_result = build_context_consolidation_result(cross_request, [project_a, project_x], cross_synthesis)
    check("R28-cross-project-semantic-synthesis-allowed", cross_result["join_target_candidate_ref"] is None and cross_result["automatic_state_mutation"] is False)
    return {
        "schema": "cerebro-context-consolidation-selftest/v1",
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
