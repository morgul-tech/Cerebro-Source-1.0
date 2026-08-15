#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _text(value).upper()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _refs(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({_text(x) for x in values if _text(x)})


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module-load-failed:{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validated_interaction_evidence(request: dict[str, Any], root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    assessment = request.get("control_context_intent_assessment")
    envelope = request.get("control_context_binding")
    if not isinstance(assessment, dict) or not isinstance(envelope, dict):
        return None, []
    project = envelope.get("project")
    session = envelope.get("session")
    if not isinstance(project, dict) or not isinstance(session, dict):
        return None, []
    try:
        interaction = _load(root / "engines/interaction/control_context_intent.py", "cerebro_integrity_intent_validation")
        validated = interaction.validate_control_context_intent_assessment(
            assessment, project, session, envelope.get("event_id")
        )
        if not isinstance(validated, dict) or validated.get("result") != "PASS":
            return None, []
    except Exception:
        return None, []
    fingerprint = _text(assessment.get("assessment_fingerprint"))
    return assessment, (["INTERACTION-ASSESSMENT:" + fingerprint] if fingerprint else [])


def _quality_trace_projection(request: dict[str, Any], candidate_profile: dict[str, Any], basis_fingerprint: str) -> dict[str, Any]:
    trace = request.get("quality_trace")
    required_depth = _upper(request.get("required_workform_depth")) or _upper(candidate_profile.get("analysis_depth"))
    selected_depth = _upper(candidate_profile.get("analysis_depth"))
    out = {
        "required_depth": required_depth,
        "selected_depth": selected_depth,
        "required_operations": [],
        "completed_operations": [],
        "quality_trace_current": None,
        "silent_downgrade_known": request.get("silent_workform_downgrade_known") is True,
        "evidence_refs": [],
        "effect_evidence_refs": [],
        "owner_refs": ["quality", "MCP"],
        "mechanism_refs": ["CEREBRO-ADAPTIVE-QUALITY-WORKFORM-001"],
    }
    if not isinstance(trace, dict) or trace.get("schema") != "cerebro-quality-trace/v0.2":
        return out
    trace_basis = _text(trace.get("basis_fingerprint"))
    current = trace_basis == basis_fingerprint
    out["quality_trace_current"] = current
    required_depth = _upper(trace.get("required_depth")) or required_depth
    out["required_depth"] = required_depth
    required = {
        "LIGHT": ["UNDERSTAND_FRAME", "EXECUTE_GENERATE", "VERIFY"],
        "STANDARD": ["UNDERSTAND_FRAME", "EXPLORE_RESEARCH", "REFINE", "CRITIQUE", "COMPARE_CONVERGE", "EXECUTE_GENERATE", "VERIFY"],
        "DEEP": ["UNDERSTAND_FRAME", "EXPLORE_RESEARCH", "REFINE", "CRITIQUE", "COMPARE_CONVERGE", "DECIDE", "EXECUTE_GENERATE", "VERIFY", "LEARN"],
    }.get(required_depth, [])
    out["required_operations"] = required
    stages = trace.get("stages") if isinstance(trace.get("stages"), dict) else {}
    completed: list[str] = []
    evidence: list[str] = []
    for stage in required:
        state = stages.get(stage) if isinstance(stages.get(stage), dict) else {}
        if state.get("state") == "PASS" and _text(state.get("basis_fingerprint")) == trace_basis:
            refs = _refs(state.get("evidence_refs"))
            if refs:
                completed.append(stage)
                evidence.extend(refs)
    out["completed_operations"] = completed
    out["evidence_refs"] = sorted(set(evidence))
    out["effect_evidence_refs"] = sorted(set(evidence))
    return out


def _gate_profile(request: dict[str, Any]) -> str | None:
    explicit = _upper(request.get("integrity_gate_profile"))
    if explicit:
        return explicit
    stage = _upper(request.get("stage"))
    if stage in {"DECIDE", "LOCK"}:
        return "DECISION_READINESS"
    if request.get("patch_handoff_requested") is True:
        return "PATCH_READINESS"
    if request.get("operational_claim_requested") is True:
        return "OPERATIONAL_INTEGRATION"
    if request.get("delivery_requested") is True:
        return "DELIVERY_CONFORMITY"
    if request.get("implementation_requested") is True:
        return "IMPLEMENTATION_READINESS"
    return None


def _gate_projection(request: dict[str, Any], candidate_decision: dict[str, Any], basis_fingerprint: str) -> dict[str, Any]:
    profile = _gate_profile(request)
    next_event = _text(request.get("next_control_event_ref"))
    if not next_event:
        next_event = {
            "DECISION_READINESS": "DECIDE_OR_LOCK",
            "IMPLEMENTATION_READINESS": "IMPLEMENT",
            "PATCH_READINESS": "HUMAN_PATCH_HANDOFF",
            "OPERATIONAL_INTEGRATION": "OPERATIONAL_CLAIM",
            "DELIVERY_CONFORMITY": "DELIVER",
        }.get(profile or "", "")
    gate = request.get("integrity_gate_evidence")
    gate = gate if isinstance(gate, dict) else {}
    evidence = _refs(gate.get("evidence_refs"))
    return {
        "gate_profile": profile,
        "next_control_event_ref": next_event,
        "stage": request.get("stage"),
        "implementation_requested": request.get("implementation_requested") is True,
        "patch_handoff_requested": request.get("patch_handoff_requested") is True,
        "operational_claim_requested": request.get("operational_claim_requested") is True,
        "delivery_requested": request.get("delivery_requested") is True,
        "required_checks": _refs(gate.get("required_checks")),
        "passed_checks": _refs(gate.get("passed_checks")),
        "failed_checks": _refs(gate.get("failed_checks")),
        "unavailable_checks": _refs(gate.get("unavailable_checks")),
        "evidence_refs": evidence,
        "effect_evidence_refs": _refs(gate.get("effect_evidence_refs")) or evidence,
        "owner_refs": _refs(gate.get("owner_refs")) or ["MCP"],
        "mechanism_refs": _refs(gate.get("mechanism_refs")),
    }


def build_integrity_request(
    request: dict[str, Any],
    candidate: dict[str, Any],
    context_binding: dict[str, Any] | None,
    promotion_basis: dict[str, Any],
    preflight_result: dict[str, Any] | None,
    delivery_resolution: dict[str, Any] | None,
    phase_transition: dict[str, Any] | None,
    invocation: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    candidate_decision = candidate.get("mcp_control_decision") if isinstance(candidate.get("mcp_control_decision"), dict) else {}
    candidate_state = candidate.get("control_state") if isinstance(candidate.get("control_state"), dict) else {}
    candidate_profile = candidate.get("execution_profile") if isinstance(candidate.get("execution_profile"), dict) else {}
    basis_fingerprint = _text(candidate_decision.get("basis_fingerprint")) or _text(candidate_state.get("basis_fingerprint"))
    if not basis_fingerprint:
        raise ValueError("integrity-canonical-basis-fingerprint-missing")
    event_ref = _text(request.get("control_event_ref"))
    if not event_ref and isinstance(context_binding, dict):
        event_ref = _text(context_binding.get("event_id"))
    event_ref = event_ref or _text(candidate_decision.get("control_decision_id"))
    if not event_ref:
        raise ValueError("integrity-control-event-ref-missing")

    interaction_assessment, interaction_refs = _validated_interaction_evidence(request, root)
    preflight = preflight_result if isinstance(preflight_result, dict) else {}
    preflight_receipt = preflight.get("receipt") if isinstance(preflight.get("receipt"), dict) else {}
    preflight_ref = _text(preflight_receipt.get("control_decision_ref"))
    preflight_evidence = ["MATERIAL-PREFLIGHT:" + preflight_ref] if preflight_ref else []

    resolved_objective = _text(request.get("resolved_objective") or request.get("resolved_objective_ref"))
    current_objective = _text(request.get("current_objective") or candidate_decision.get("objective_ref"))
    resolved_scope = _text(request.get("resolved_scope"))
    current_scope = _text(request.get("current_scope"))
    objective_evidence = list(interaction_refs)
    if preflight.get("result") == "PASS" and preflight_evidence:
        objective_evidence.extend(preflight_evidence)
        resolved_objective = resolved_objective or _text(preflight_receipt.get("resolved_objective"))
        resolved_scope = resolved_scope or _text(preflight_receipt.get("resolved_scope"))
    objective_effect = list(objective_evidence)

    previous_ref = ""
    previous_evidence: list[str] = []
    if isinstance(context_binding, dict):
        previous_ref = "CONTROL-CONTEXT:" + _text(context_binding.get("active_context_ref"))
        binding_fp = _text(context_binding.get("binding_fingerprint"))
        if binding_fp:
            previous_evidence = ["CONTROL-CONTEXT-BINDING:" + binding_fp]
    else:
        supplied_previous = _text(request.get("previous_control_state_ref"))
        previous_evidence = _refs(request.get("previous_control_state_evidence_refs"))
        if supplied_previous and previous_evidence:
            previous_ref = supplied_previous

    current_ref = _text(candidate_decision.get("control_state_ref")) or _text(candidate_state.get("control_state_id"))
    canonical_ref = _text(candidate_decision.get("control_decision_id"))
    current_evidence = ["CANONICAL-MCP-CANDIDATE:" + canonical_ref] if canonical_ref else []

    next_ref = ""
    next_evidence: list[str] = []
    supplied_next = _text(request.get("next_control_event_ref"))
    supplied_next_evidence = _refs(request.get("next_control_event_evidence_refs"))
    if supplied_next and supplied_next_evidence:
        next_ref = supplied_next
        next_evidence = supplied_next_evidence
    elif _text(request.get("commitment_target")) and preflight.get("result") == "PASS" and preflight_evidence:
        next_ref = _text(request.get("commitment_target"))
        next_evidence = list(preflight_evidence)

    mcp_evidence = list(current_evidence)
    mcp_evidence.extend(previous_evidence)
    mcp_evidence.extend(next_evidence)
    basis_digest = _hash(promotion_basis)
    if promotion_basis.get("promotion_basis_verified") is True:
        mcp_evidence.append("PROMOTION-BASIS:" + basis_digest)
    assurance_refs = _refs(request.get("assurance_evidence_refs"))
    mcp_evidence.extend(assurance_refs)

    if isinstance(context_binding, dict):
        position_ref = _text(context_binding.get("binding_fingerprint"))
        position_evidence = ["CONTROL-CONTEXT-BINDING:" + position_ref] if position_ref else []
        work_position = {
            "applicable": True,
            "binding_validated": True,
            "position_coherent": True,
            "side_branch_drift_known": request.get("side_branch_drift_known") is True,
            "project_ref": context_binding.get("project_ref"),
            "session_ref": context_binding.get("session_ref"),
            "active_context_ref": context_binding.get("active_context_ref"),
            "evidence_refs": position_evidence,
            "effect_evidence_refs": position_evidence,
            "owner_refs": ["project", "context"],
            "mechanism_refs": ["CEREBRO-CONTROL-CONTEXT-HIERARCHY-001"],
        }
    else:
        work_position = {
            "applicable": False,
            "evidence_refs": ["PROJECT-CONTEXT:NOT_APPLICABLE"],
            "owner_refs": ["project", "context"],
        }

    quality = _quality_trace_projection(request, candidate_profile, basis_fingerprint)

    applicable_prior = _refs(candidate_state.get("applicable_wisdom_refs")) + _refs(candidate_state.get("applicable_knowledge_refs")) + _refs(candidate_state.get("applicable_history_refs"))
    prior_refs = sorted(set(applicable_prior))
    preflight_complete = preflight.get("result") == "PASS" and preflight_receipt.get("basis_fingerprint") is not None
    consumed_basis_refs = set(_refs(candidate_state.get("governing_basis_refs")) + _refs(candidate_decision.get("basis_refs")))
    prior_learning_consumed = set(prior_refs).issubset(consumed_basis_refs) if prior_refs else True

    basis_evidence = ["PROMOTION-BASIS:" + basis_digest] if promotion_basis.get("promotion_basis_verified") is True else []
    basis_evidence.extend(preflight_evidence)
    observed_source_head = _text(promotion_basis.get("observed_source_head"))
    supplied_source_commit = _text(request.get("authoritative_source_commit"))
    source_commit = observed_source_head or supplied_source_commit
    source_current = bool(
        promotion_basis.get("promotion_basis_verified") is True
        and observed_source_head
        and (not supplied_source_commit or supplied_source_commit == observed_source_head)
    )
    if source_current:
        basis_evidence.append("SOURCE-HEAD-VERIFIED:" + observed_source_head)

    failure_refs = _refs(request.get("applicable_failure_family_refs"))
    failure_applied_refs = _refs(request.get("applied_failure_family_refs"))
    architecture_assessment_refs = _refs(request.get("architecture_assessment_evidence_refs"))
    architecture_assessment_current = bool(architecture_assessment_refs)
    mechanism_assessed = request.get("existing_mechanism_assessed") is True and architecture_assessment_current
    owner_assessed = request.get("existing_owner_assessed") is True and architecture_assessment_current
    parallel_assessed = request.get("parallel_mechanism_risk_assessed") is True and architecture_assessment_current

    prior_consumption_evidence = ["MCP-BASIS-CONSUMED:" + ref for ref in prior_refs if ref in consumed_basis_refs]
    basis_effect = list(basis_evidence) + prior_consumption_evidence + architecture_assessment_refs

    gate = _gate_projection(request, candidate_decision, basis_fingerprint)
    project_context_refs: list[str] = []
    if isinstance(context_binding, dict):
        project_context_refs = _refs([
            context_binding.get("project_ref"), context_binding.get("session_ref"), context_binding.get("active_context_ref")
        ])

    snapshot = {
        "objective_alignment": {
            "resolved_objective_ref": resolved_objective,
            "current_objective_ref": current_objective,
            "resolved_scope_ref": resolved_scope,
            "current_scope_ref": current_scope,
            "material_assumptions_exposed": request.get("material_assumptions_exposed"),
            "silent_material_reframe_known": request.get("silent_material_reframe_known") is True,
            "evidence_refs": sorted(set(objective_evidence)),
            "effect_evidence_refs": sorted(set(objective_effect)),
            "owner_refs": ["interaction"],
            "mechanism_refs": ["CONTROL_CONTEXT_INTENT_ASSESSMENT"] if interaction_assessment else [],
        },
        "mcp_loop_integrity": {
            "previous_state_ref": previous_ref,
            "current_state_ref": current_ref,
            "next_control_event_ref": next_ref,
            "mcp_governs_current_work": True,
            "governing_contracts_preserved": promotion_basis.get("promotion_basis_verified") is True,
            "assurance_preserved": True if assurance_refs else request.get("assurance_not_applicable") is True,
            "known_control_bypass": request.get("known_control_bypass") is True,
            "evidence_refs": sorted(set(mcp_evidence)),
            "effect_evidence_refs": sorted(set(mcp_evidence)),
            "owner_refs": ["MCP"],
            "mechanism_refs": ["CEREBRO-MCP-CONTROL-RESOLUTION-001"],
        },
        "work_position": work_position,
        "workform_adequacy": quality,
        "basis_and_prior_knowledge": {
            "basis_ref": source_commit or candidate_state.get("source_identity"),
            "authoritative_basis_current": source_current,
            "source_freshness_need_resolved": source_current or preflight_complete,
            "existing_mechanism_assessed": mechanism_assessed,
            "existing_owner_assessed": owner_assessed,
            "parallel_mechanism_risk_assessed": parallel_assessed,
            "prior_learning_applicable": bool(prior_refs),
            "prior_learning_applied": prior_learning_consumed,
            "prior_learning_refs": prior_refs,
            "failure_families_applicable": bool(failure_refs),
            "failure_families_applied": set(failure_refs).issubset(set(failure_applied_refs)) if failure_refs else True,
            "failure_family_refs": failure_refs,
            "evidence_refs": sorted(set(basis_evidence + prior_refs + prior_consumption_evidence + failure_applied_refs + architecture_assessment_refs)),
            "effect_evidence_refs": sorted(set(basis_effect + failure_applied_refs)),
            "owner_refs": ["MCP", "context"],
            "mechanism_refs": ["CEREBRO-MATERIAL-COMMITMENT-PREFLIGHT-001"] if preflight else [],
        },
        "next_gate_readiness": gate,
    }
    return {
        "control_event_ref": event_ref,
        "basis_fingerprint": basis_fingerprint,
        "coverage_mode": invocation.get("coverage_mode") or "ADAPTIVE",
        "primary_scope": invocation.get("primary_scope") or "ALL",
        "project_context_refs": project_context_refs,
        "resolved_objective_ref": resolved_objective,
        "governing_workform_ref": quality.get("required_depth") or candidate_profile.get("analysis_depth"),
        "next_gate_ref": gate.get("next_control_event_ref"),
        "gate_enforcement_required": gate.get("gate_profile") is not None,
        "invalidation_triggers": [
            "MATERIAL_BASIS_CHANGE", "PROJECT_CONTEXT_CHANGE", "RESOLVED_OBJECTIVE_CHANGE",
            "GOVERNING_WORKFORM_CHANGE", "NEXT_GATE_CHANGE",
        ],
        "control_snapshot": snapshot,
        "invocation_reasons": invocation.get("reasons", []),
        "canonical_context": {
            "promotion_basis_verified": promotion_basis.get("promotion_basis_verified") is True,
            "material_preflight_exercised": isinstance(preflight_result, dict),
            "delivery_profile_resolved": delivery_resolution is not None,
            "phase_transition_resolved": phase_transition is not None,
        },
    }


def apply_recommendation(candidate_outcome: str, assessment: dict[str, Any]) -> tuple[str, list[str]]:
    current = _upper(candidate_outcome)
    recommendation = _upper(_text((assessment.get("control_implications") or {}).get("recommended_mcp_outcome")))
    if current in {"BLOCK", "USER_DECISION_REQUIRED"}:
        return current, []
    if recommendation not in {"BLOCK", "REORIENT", "REMEDIATE"}:
        return current, []
    precedence = {"CONTINUE": 0, "RETRY": 0, "REMEDIATE": 1, "REORIENT": 2, "BLOCK": 3}
    if precedence.get(recommendation, 0) > precedence.get(current, 0):
        return recommendation, ["INTEGRITY:" + str(x) for x in (assessment.get("control_implications") or {}).get("reasons", [])]
    return current, []
