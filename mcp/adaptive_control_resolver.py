#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_ID = "cerebro-adaptive-control-resolution/v0.1"
DECISION_SCHEMA = "cerebro-mcp-control-decision/adaptive-candidate-v0.1"
PROFILE_SCHEMA = "cerebro-execution-profile/adaptive-candidate-v0.1"
ENGINE_VERSION = "0.1.1"
CONTROL_OUTCOMES = {"CONTINUE", "REMEDIATE", "RETRY", "REORIENT", "USER_DECISION_REQUIRED", "BLOCK"}
DEPTHS = ("LIGHT", "STANDARD", "DEEP")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def upper(value: Any, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text.upper()


def bool_value(value: Any) -> bool:
    return value is True


def evidence_is_current(observation: dict[str, Any]) -> bool:
    freshness = observation.get("freshness")
    if isinstance(freshness, dict):
        if freshness.get("current") is True:
            return True
        if upper(freshness.get("state")) in {"CURRENT", "FRESH", "VALID"}:
            return True
        return False
    return upper(freshness) in {"CURRENT", "FRESH", "VALID", "STATE_BOUND_CURRENT"}


def capability_state_from_observations(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        if not isinstance(item, dict):
            continue
        capability = str(item.get("capability") or item.get("subject_ref") or "").strip()
        if not capability:
            continue
        grouped.setdefault(capability, []).append(item)

    resolved: dict[str, dict[str, Any]] = {}
    for capability, items in sorted(grouped.items()):
        current_results: list[str] = []
        evidence_refs: list[str] = []
        for item in items:
            evidence_ref = str(item.get("evidence_id") or item.get("probe_ref") or "").strip()
            if evidence_ref:
                evidence_refs.append(evidence_ref)
            if not evidence_is_current(item):
                continue
            result = upper(item.get("result"), "UNKNOWN")
            if result in {"PASS", "FAIL", "UNKNOWN"}:
                current_results.append(result)

        distinct = set(current_results)
        if "PASS" in distinct and "FAIL" in distinct:
            state = "UNKNOWN"
            reason = "CONFLICTING_FRESH_EVIDENCE"
        elif "PASS" in distinct:
            state = "AVAILABLE"
            reason = "FRESH_PASS_EVIDENCE"
        elif "FAIL" in distinct:
            state = "UNAVAILABLE"
            reason = "FRESH_FAIL_EVIDENCE"
        else:
            state = "UNKNOWN"
            reason = "NO_FRESH_RESOLVING_EVIDENCE"

        resolved[capability] = {
            "state": state,
            "reason": reason,
            "evidence_refs": sorted(set(evidence_refs)),
            "fresh_observation_count": len(current_results),
        }
    return resolved


def resolve_capabilities(request: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    observations = [item for item in request.get("capability_observations", []) if isinstance(item, dict)]
    observed = capability_state_from_observations(observations)
    resolutions: dict[str, Any] = {}
    blockers: list[str] = []

    for spec in request.get("requested_capabilities", []):
        if not isinstance(spec, dict):
            continue
        capability = str(spec.get("id") or "").strip()
        if not capability:
            continue
        state = deepcopy(observed.get(capability, {
            "state": "UNKNOWN",
            "reason": "NO_FRESH_RESOLVING_EVIDENCE",
            "evidence_refs": [],
            "fresh_observation_count": 0,
        }))
        required = bool_value(spec.get("required"))
        fallback = str(spec.get("fallback") or "").strip()
        fallback_proven = bool_value(spec.get("fallback_proven"))

        if state["state"] == "AVAILABLE":
            action = "USE_CAPABILITY"
            selected = capability
        elif fallback and fallback_proven:
            action = "USE_PROVEN_FALLBACK"
            selected = fallback
        elif required:
            action = "BLOCK_REQUIRED_CAPABILITY_UNRESOLVED"
            selected = None
            blockers.append(f"REQUIRED_CAPABILITY_NOT_AVAILABLE:{capability}:{state['state']}")
        else:
            action = "SKIP_OPTIONAL_CAPABILITY"
            selected = None

        state.update({
            "required": required,
            "fallback": fallback or None,
            "fallback_proven": fallback_proven,
            "action": action,
            "selected": selected,
        })
        resolutions[capability] = state

    return resolutions, blockers


def choose_analysis_depth(request: dict[str, Any]) -> str:
    score = 0
    consequence = upper(request.get("consequence"), "LOW")
    uncertainty = upper(request.get("uncertainty"), "LOW")
    reversibility = upper(request.get("reversibility"), "REVERSIBLE")
    if consequence in {"MODERATE", "MEDIUM"}:
        score += 1
    elif consequence in {"HIGH", "CRITICAL"}:
        score += 2
    if uncertainty in {"MODERATE", "MEDIUM"}:
        score += 1
    elif uncertainty == "HIGH":
        score += 2
    if reversibility in {"BOUNDED", "PARTIAL"}:
        score += 1
    elif reversibility in {"IRREVERSIBLE", "HARD_TO_REVERSE"}:
        score += 2
    if bool_value(request.get("architecture_material")):
        score += 3
    if bool_value(request.get("material")):
        score += 1
    if score <= 1:
        return "LIGHT"
    if score <= 4:
        return "STANDARD"
    return "DEEP"


def choose_verification_depth(request: dict[str, Any], analysis_depth: str) -> str:
    depth = analysis_depth
    if bool_value(request.get("mandatory_verification")) or bool_value(request.get("architecture_material")):
        depth = "DEEP"
    elif bool_value(request.get("material")) and depth == "LIGHT":
        depth = "STANDARD"
    return depth


def continuation_effect(request: dict[str, Any]) -> str:
    if bool_value(request.get("objective_changed")):
        return "REPLACE"
    if bool_value(request.get("material_user_insight")):
        return "RERESOLVE"
    if bool_value(request.get("active_continuation")):
        return "PRESERVE"
    return "NONE"


def human_boundary(request: dict[str, Any]) -> str:
    if bool_value(request.get("authorization_required")):
        return "EXPLICIT_AUTHORIZATION_REQUIRED"
    if upper(request.get("human_decision_value"), "LOW") == "HIGH":
        return "HIGH_VALUE_HUMAN_DECISION"
    if bool_value(request.get("required_user_fact_missing")):
        return "REQUIRED_USER_FACT"
    if bool_value(request.get("next_action_requires_local_user_execution")):
        return "LOCAL_USER_EXECUTION"
    return "NONE"


def resolve_outcome(request: dict[str, Any], capability_blockers: list[str], boundary: str) -> tuple[str, list[str]]:
    blockers = list(capability_blockers)
    if bool_value(request.get("authority_block")):
        blockers.append("AUTHORITY_BLOCK")
    if bool_value(request.get("safety_block")):
        blockers.append("SAFETY_BLOCK")
    if blockers:
        return "BLOCK", blockers
    if boundary != "NONE":
        return "USER_DECISION_REQUIRED", []
    if bool_value(request.get("materially_different_path_required")):
        delta_state = reorientation_delta_state(request)
        if delta_state == "UNCHANGED":
            blockers.append("REORIENTATION_INVALID_UNCHANGED_PATH")
            return "BLOCK", blockers
        if delta_state == "UNRESOLVED":
            blockers.append("REORIENTATION_DELTA_UNRESOLVED")
            return "BLOCK", blockers
        return "REORIENT", []
    if bool_value(request.get("failure_recovery_needed")):
        if bool_value(request.get("retry_has_material_delta")):
            return "RETRY", []
        return "REMEDIATE", []
    return "CONTINUE", []


REORIENTATION_DELTA_PAIRS = (
    ("current_execution_mechanism", "proposed_execution_mechanism"),
    ("current_execution_profile_ref", "proposed_execution_profile_ref"),
    ("current_delivery_path", "proposed_delivery_path"),
    ("current_recovery_strategy", "proposed_recovery_strategy"),
    ("current_bounded_scope", "proposed_bounded_scope"),
)


def reorientation_delta_state(request: dict[str, Any]) -> str:
    comparisons: list[bool] = []
    for current_key, proposed_key in REORIENTATION_DELTA_PAIRS:
        current_present = current_key in request
        proposed_present = proposed_key in request
        if not current_present and not proposed_present:
            continue
        current = str(request.get(current_key) or "").strip()
        proposed = str(request.get(proposed_key) or "").strip()
        if not current or not proposed:
            return "UNRESOLVED"
        comparisons.append(current != proposed)
    if not comparisons:
        return "DECLARED_MATERIAL_DELTA"
    return "MATERIAL_DELTA" if any(comparisons) else "UNCHANGED"


def efficiency_resolution(request: dict[str, Any]) -> dict[str, str]:
    resource_pressure = upper(request.get("resource_pressure"), "UNKNOWN")
    human_time_priority = upper(request.get("human_time_priority"), "NORMAL")
    conserve = resource_pressure in {"AMBER", "RED"} or human_time_priority == "HIGH"
    return {
        "resource_pressure": resource_pressure,
        "human_time_priority": human_time_priority,
        "efficiency_bias": "CONSERVE_OPTIONAL_WORK" if conserve else "BALANCED",
        "mandatory_assurance_effect": "NONE",
    }


def resolve(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("request-must-be-object")

    analysis_depth = choose_analysis_depth(request)
    verification_depth = choose_verification_depth(request, analysis_depth)
    capabilities, capability_blockers = resolve_capabilities(request)
    boundary = human_boundary(request)
    outcome, blockers = resolve_outcome(request, capability_blockers, boundary)
    continuation = continuation_effect(request)
    efficiency = efficiency_resolution(request)

    basis_material = {
        "objective_ref": str(request.get("objective_ref") or "UNSPECIFIED"),
        "analysis_depth": analysis_depth,
        "verification_depth": verification_depth,
        "capabilities": capabilities,
        "human_boundary": boundary,
        "continuation_effect": continuation,
        "outcome": outcome,
        "blockers": blockers,
        "efficiency": efficiency,
        "governing_basis_refs": sorted(str(x) for x in request.get("governing_basis_refs", [])),
        "source_identity": str(request.get("authoritative_source_commit") or "UNKNOWN"),
    }
    basis_fingerprint = fingerprint(basis_material)
    control_state_id = "CTRL-AA1-" + basis_fingerprint[:16].upper()
    decision_id = "MCPD-AA1-" + fingerprint({"state": control_state_id, "outcome": outcome})[:16].upper()
    profile_id = "EXECP-AA1-" + fingerprint({"state": control_state_id, "analysis": analysis_depth, "verification": verification_depth})[:16].upper()

    selected_mechanisms = [value.get("selected") for value in capabilities.values() if value.get("selected")]
    execution_mechanism = str(request.get("execution_mechanism") or (selected_mechanisms[0] if len(selected_mechanisms) == 1 else "UNRESOLVED"))

    control_state = {
        "control_state_id": control_state_id,
        "domain": str(request.get("domain") or "CEREBRO"),
        "objective_ref": str(request.get("objective_ref") or "UNSPECIFIED"),
        "governing_basis_refs": sorted(str(x) for x in request.get("governing_basis_refs", [])),
        "effective_user_configuration": str(request.get("effective_user_configuration") or "CURRENT_EFFECTIVE_CONFIGURATION"),
        "execution_profile_ref": profile_id,
        "applicable_wisdom_refs": sorted(str(x) for x in request.get("applicable_wisdom_refs", [])),
        "applicable_knowledge_refs": sorted(str(x) for x in request.get("applicable_knowledge_refs", [])),
        "applicable_history_refs": sorted(str(x) for x in request.get("applicable_history_refs", [])),
        "coverage_state": str(request.get("coverage_state") or "CURRENT_SCOPE_ONLY"),
        "conflict_state": str(request.get("conflict_state") or "NONE_FOUND"),
        "semantic_resolution_state": str(request.get("semantic_resolution_state") or "RESOLVED"),
        "progress_state": str(request.get("progress_state") or "ADAPTIVE_RESOLUTION"),
        "failure_state": str(request.get("failure_state") or "NONE"),
        "verification_state": "ADAPTIVE_CANDIDATE_RESOLVED",
        "capability_state": capabilities,
        "human_boundary": boundary,
        "basis_fingerprint": basis_fingerprint,
    }

    execution_profile = {
        "schema": PROFILE_SCHEMA,
        "execution_profile_id": profile_id,
        "working_mode": "TASK_NATIVE",
        "delivery_mode": str(request.get("delivery_mode") or "NONE"),
        "autonomy": "HUMAN_BOUNDARY_NOW" if boundary != "NONE" else "AUTONOMOUS_UNTIL_BOUNDARY",
        "analysis_depth": analysis_depth,
        "verification_depth": verification_depth,
        "execution_mechanism": execution_mechanism,
        "human_boundary": boundary,
        "mutation_scope": str(request.get("mutation_scope") or "NONE"),
        "publication_path": str(request.get("publication_path") or "NONE"),
        "basis_fingerprint": basis_fingerprint,
        "efficiency": efficiency,
    }

    decision = {
        "schema": DECISION_SCHEMA,
        "control_decision_id": decision_id,
        "control_state_ref": control_state_id,
        "objective_ref": control_state["objective_ref"],
        "basis_refs": control_state["governing_basis_refs"],
        "basis_fingerprint": basis_fingerprint,
        "effective_user_config_ref": control_state["effective_user_configuration"],
        "execution_profile_ref": profile_id,
        "applicable_control_refs": [
            "CEREBRO-CONTROL-ARCHITECTURE-001",
            "CEREBRO-ADAPTIVE-CONTROL-RESOLVER-001",
            "CEREBRO-ADAPTIVE-QUALITY-WORKFORM-001",
        ],
        "outcome": outcome,
        "invalidates": blockers,
        "verification_requirement": verification_depth,
        "human_boundary": boundary,
        "evidence_scope": "CURRENT_CONTROL_AND_FRESH_CAPABILITY_EVIDENCE",
        "resolved_at": utc_now(),
    }
    if decision["outcome"] not in CONTROL_OUTCOMES:
        raise AssertionError("noncanonical-control-outcome")

    return {
        "schema": SCHEMA_ID,
        "resolver_version": ENGINE_VERSION,
        "authority": "DERIVED_CANDIDATE_CONTROL_EVIDENCE",
        "live_control_authority": False,
        "shadow_validation_required_before_promotion": True,
        "promotion_patch_ref": "PATCH-AA-004",
        "control_state": control_state,
        "mcp_control_decision": decision,
        "execution_profile": execution_profile,
        "continuation_effect": continuation,
        "capability_resolution": capabilities,
        "required_existing_control_calls": [
            "MATERIAL_COMMITMENT_PREFLIGHT_WHEN_MATERIAL",
            "RELEVANCE_RETRIEVAL_WHEN_APPLICABLE",
            "HUMAN_CONTINUATION_SURFACE_WHEN_HUMAN_ACTION_IS_NEXT",
        ],
    }


def selftest() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL", "detail": detail})

    simple = resolve({"objective_ref": "SIMPLE", "consequence": "LOW", "uncertainty": "LOW", "reversibility": "REVERSIBLE"})
    check("simple-selects-light", simple["execution_profile"]["analysis_depth"] == "LIGHT")
    check("simple-continues-autonomously", simple["mcp_control_decision"]["outcome"] == "CONTINUE")

    architecture = resolve({"objective_ref": "ARCH", "architecture_material": True, "material": True, "consequence": "HIGH", "uncertainty": "HIGH", "mandatory_verification": True})
    check("architecture-selects-deep", architecture["execution_profile"]["analysis_depth"] == "DEEP")
    check("architecture-verification-deep", architecture["execution_profile"]["verification_depth"] == "DEEP")

    human = resolve({"objective_ref": "HUMAN", "human_decision_value": "HIGH"})
    check("high-value-human-boundary", human["mcp_control_decision"]["outcome"] == "USER_DECISION_REQUIRED")

    detour = resolve({"objective_ref": "DET", "active_continuation": True})
    check("nonmaterial-detour-preserves", detour["continuation_effect"] == "PRESERVE")
    material_insight = resolve({"objective_ref": "INSIGHT", "active_continuation": True, "material_user_insight": True})
    check("material-insight-reresolves", material_insight["continuation_effect"] == "RERESOLVE")
    changed = resolve({"objective_ref": "CHANGED", "active_continuation": True, "objective_changed": True})
    check("new-objective-replaces", changed["continuation_effect"] == "REPLACE")

    cpatch_available = resolve({
        "objective_ref": "CPASS",
        "requested_capabilities": [{"id": "cpatch", "required": False, "fallback": "self-contained-standard-launcher", "fallback_proven": True}],
        "capability_observations": [{"capability": "cpatch", "evidence_id": "E1", "result": "PASS", "freshness": "CURRENT"}],
    })
    check("fresh-cpatch-pass-is-available", cpatch_available["capability_resolution"]["cpatch"]["action"] == "USE_CAPABILITY")

    cpatch_unknown = resolve({
        "objective_ref": "CUNKNOWN",
        "requested_capabilities": [{"id": "cpatch", "required": False, "fallback": "self-contained-standard-launcher", "fallback_proven": True}],
    })
    check("missing-cpatch-evidence-is-unknown", cpatch_unknown["capability_resolution"]["cpatch"]["state"] == "UNKNOWN")
    check("unknown-cpatch-uses-fallback", cpatch_unknown["capability_resolution"]["cpatch"]["action"] == "USE_PROVEN_FALLBACK")
    check("unknown-cpatch-does-not-interrupt-user", cpatch_unknown["mcp_control_decision"]["outcome"] == "CONTINUE")

    cpatch_stale = resolve({
        "objective_ref": "CSTALE",
        "requested_capabilities": [{"id": "cpatch", "required": False, "fallback": "self-contained-standard-launcher", "fallback_proven": True}],
        "capability_observations": [{"capability": "cpatch", "evidence_id": "OLD", "result": "PASS", "freshness": "STALE"}],
    })
    check("stale-pass-does-not-prove-callability", cpatch_stale["capability_resolution"]["cpatch"]["state"] == "UNKNOWN")
    check("stale-pass-falls-back", cpatch_stale["capability_resolution"]["cpatch"]["selected"] == "self-contained-standard-launcher")

    required_unknown = resolve({"objective_ref": "REQ", "requested_capabilities": [{"id": "mandatory-x", "required": True}]})
    check("required-unknown-capability-blocks", required_unknown["mcp_control_decision"]["outcome"] == "BLOCK")

    reorient_delta = resolve({
        "objective_ref": "REORIENT-DELTA",
        "materially_different_path_required": True,
        "current_execution_mechanism": "launcher-A",
        "proposed_execution_mechanism": "launcher-B",
    })
    check("reorientation-with-material-path-delta", reorient_delta["mcp_control_decision"]["outcome"] == "REORIENT")
    reorient_same = resolve({
        "objective_ref": "REORIENT-SAME",
        "materially_different_path_required": True,
        "current_execution_mechanism": "launcher-A",
        "proposed_execution_mechanism": "launcher-A",
    })
    check("unchanged-path-cannot-reorient", reorient_same["mcp_control_decision"]["outcome"] == "BLOCK" and "REORIENTATION_INVALID_UNCHANGED_PATH" in reorient_same["mcp_control_decision"]["invalidates"])
    reorient_unresolved = resolve({
        "objective_ref": "REORIENT-UNRESOLVED",
        "materially_different_path_required": True,
        "current_execution_mechanism": "launcher-A",
        "proposed_execution_mechanism": "",
    })
    check("incomplete-reorientation-delta-blocks", reorient_unresolved["mcp_control_decision"]["outcome"] == "BLOCK" and "REORIENTATION_DELTA_UNRESOLVED" in reorient_unresolved["mcp_control_decision"]["invalidates"])

    resource = resolve({
        "objective_ref": "RESOURCE", "resource_pressure": "RED", "human_time_priority": "HIGH",
        "architecture_material": True, "mandatory_verification": True,
    })
    check("resource-pressure-conserves-optional-work", resource["execution_profile"]["efficiency"]["efficiency_bias"] == "CONSERVE_OPTIONAL_WORK")
    check("resource-pressure-never-reduces-mandatory-verification", resource["execution_profile"]["verification_depth"] == "DEEP")
    check("candidate-not-live-control", resource["live_control_authority"] is False and resource["shadow_validation_required_before_promotion"] is True)

    passed = all(item["result"] == "PASS" for item in tests)
    return {"schema": "cerebro-adaptive-control-resolver-selftest/v0.1", "result": "PASS" if passed else "FAIL", "tests": tests}



def source_state_fingerprint(source_root: Path) -> str:
    paths = [
        source_root / "mcp/adaptive_control_resolver.py",
        source_root / "mcp/adaptive-control-resolver.yaml",
        source_root / "mcp/manifest.yaml",
    ]
    material = []
    for path in paths:
        if not path.is_file():
            raise ValueError(f"activation-basis-file-missing:{path}")
        material.append(f"{path.relative_to(source_root).as_posix()}|{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def activation_probe(source_root: Path) -> dict[str, Any]:
    tests = selftest()
    return {
        "schema": "cerebro-adaptive-control-resolver-activation-proof/v0.1",
        "result": "PASS" if tests.get("result") == "PASS" else "FAIL",
        "implementation_ref": "mcp/adaptive_control_resolver.py",
        "contract_ref": "mcp/adaptive-control-resolver.yaml",
        "live_control_authority": False,
        "shadow_validation_required": True,
        "selftest_count": len(tests.get("tests", [])),
        "selftest_result": tests.get("result"),
        "source_state_fingerprint": source_state_fingerprint(source_root),
        "observed_at": utc_now(),
    }

def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro AA-001 adaptive MCP candidate resolver")
    parser.add_argument("command", nargs="?", choices=["resolve", "activation-probe"])
    parser.add_argument("--request", help="JSON request path")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        result = selftest()
    elif args.command == "activation-probe":
        result = activation_probe(Path(args.source_root).resolve())
    else:
        if not args.request:
            parser.error("resolve requires --request, or use --selftest / activation-probe")
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = resolve(request)

    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result.get("result", "PASS") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
