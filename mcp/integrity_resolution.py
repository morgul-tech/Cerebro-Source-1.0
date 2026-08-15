#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "cerebro-mcp-integrity-assessment/v1"
CONTROL_ID = "CEREBRO-MCP-INTEGRITY-CONTROL-001"
AUTHORITY = "DERIVED_CONTROL_EVIDENCE"

DIMENSIONS = (
    "OBJECTIVE_ALIGNMENT",
    "MCP_LOOP_INTEGRITY",
    "WORK_POSITION",
    "WORKFORM_ADEQUACY",
    "BASIS_AND_PRIOR_KNOWLEDGE",
    "NEXT_GATE_READINESS",
)
RESULTS = {"PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"}
STATUSES = {"COMPLETE", "PARTIAL", "UNAVAILABLE", "ERROR"}
SUFFICIENCY = {"COMPLETE", "PARTIAL", "INSUFFICIENT", "FAILED"}
COVERAGE_MODES = {"ADAPTIVE", "FULL"}
CURRENT_FRESHNESS = {"CURRENT", "FRESH", "VALID", "STATE_BOUND_CURRENT", "IMMUTABLE_CURRENT"}
DEPTH_RANK = {"LIGHT": 1, "STANDARD": 2, "DEEP": 3}
GATE_PROFILES = {
    "DECISION_READINESS",
    "IMPLEMENTATION_READINESS",
    "PATCH_READINESS",
    "OPERATIONAL_INTEGRATION",
    "DELIVERY_CONFORMITY",
}
OUTCOME_PRECEDENCE = {None: 0, "REMEDIATE": 1, "REORIENT": 2, "BLOCK": 3}


class IntegrityResolutionError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any, default: str = "") -> str:
    text = _text(value)
    return text.upper() if text else default


def _sorted_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({_text(item) for item in values if _text(item)})


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _freshness(basis_fingerprint: str, state: str = "CURRENT") -> dict[str, str]:
    return {"state": state, "basis_fingerprint": basis_fingerprint}


def _freshness_is_current(value: Any, basis_fingerprint: str) -> bool:
    if not isinstance(value, dict):
        return False
    state = _upper(value.get("state"))
    if state not in CURRENT_FRESHNESS:
        return False
    bound_basis = _text(value.get("basis_fingerprint"))
    return bool(bound_basis and bound_basis == basis_fingerprint)


def _evidence(raw: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
    return (
        _sorted_strings(raw.get("evidence_refs")),
        _sorted_strings(raw.get("effect_evidence_refs")),
        _sorted_strings(raw.get("owner_refs")),
        _sorted_strings(raw.get("mechanism_refs")),
    )


def _derived_row(
    dimension: str,
    basis_fingerprint: str,
    *,
    result: str,
    status: str = "COMPLETE",
    sufficiency: str = "COMPLETE",
    evidence_refs: list[str] | None = None,
    effect_evidence_refs: list[str] | None = None,
    owner_refs: list[str] | None = None,
    mechanism_refs: list[str] | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "status": status,
        "result": result,
        "sufficiency": sufficiency,
        "freshness": _freshness(basis_fingerprint),
        "evidence_refs": sorted(set(evidence_refs or [])),
        "effect_evidence_refs": sorted(set(effect_evidence_refs or [])),
        "owner_refs": sorted(set(owner_refs or [])),
        "mechanism_refs": sorted(set(mechanism_refs or [])),
        "reason": reason,
        "details": details or {},
    }


def _unknown_dimension(dimension: str, basis_fingerprint: str, reason: str) -> dict[str, Any]:
    return _derived_row(
        dimension,
        basis_fingerprint,
        result="UNKNOWN",
        status="PARTIAL",
        sufficiency="INSUFFICIENT",
        reason=reason,
    )


def normalize_dimension(dimension: str, raw: Any, basis_fingerprint: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _unknown_dimension(dimension, basis_fingerprint, "DIMENSION_ASSESSMENT_NOT_SUPPLIED")

    status = _upper(raw.get("status"), "PARTIAL")
    result = _upper(raw.get("result"), "UNKNOWN")
    sufficiency = _upper(raw.get("sufficiency"), "INSUFFICIENT")
    if status not in STATUSES:
        raise IntegrityResolutionError(f"noncanonical-status:{dimension}:{status}")
    if result not in RESULTS:
        raise IntegrityResolutionError(f"noncanonical-result:{dimension}:{result}")
    if sufficiency not in SUFFICIENCY:
        raise IntegrityResolutionError(f"noncanonical-sufficiency:{dimension}:{sufficiency}")

    evidence_refs, effect_evidence_refs, owner_refs, mechanism_refs = _evidence(raw)
    freshness = raw.get("freshness") if isinstance(raw.get("freshness"), dict) else {
        "state": "UNKNOWN",
        "basis_fingerprint": basis_fingerprint,
    }
    false_green_rejected = False
    reason = _text(raw.get("reason"))
    fresh = _freshness_is_current(freshness, basis_fingerprint)

    if result == "NOT_APPLICABLE":
        effect_evidence_refs = []
        if fresh:
            status = "COMPLETE" if status != "ERROR" else status
            sufficiency = "COMPLETE" if sufficiency != "FAILED" else sufficiency
            reason = reason or "NOT_APPLICABLE_TO_CURRENT_CONTROL_BASIS"
        else:
            result = "UNKNOWN"
            status = "PARTIAL" if status != "ERROR" else status
            sufficiency = "INSUFFICIENT" if sufficiency != "FAILED" else sufficiency
            reason = reason or "STALE_APPLICABILITY_CANNOT_RESOLVE_CURRENT_QUESTION"

    if result in {"PASS", "FAIL"}:
        effect_supported = (
            bool(evidence_refs)
            and bool(effect_evidence_refs)
            and set(effect_evidence_refs).issubset(set(evidence_refs))
            and fresh
        )
        if not effect_supported:
            if result == "PASS":
                false_green_rejected = True
                reason = reason or "PASS_REJECTED_WITHOUT_CURRENT_EFFECT_EVIDENCE"
            else:
                reason = reason or "FAIL_REJECTED_WITHOUT_CURRENT_EFFECT_EVIDENCE"
            result = "UNKNOWN"
            if status == "COMPLETE":
                status = "PARTIAL"
            if sufficiency == "COMPLETE":
                sufficiency = "INSUFFICIENT"

    if status == "UNAVAILABLE" and result == "FAIL":
        result = "UNKNOWN"
        if sufficiency == "COMPLETE":
            sufficiency = "INSUFFICIENT"
        reason = reason or "UNAVAILABLE_EVIDENCE_IS_NOT_SUBJECT_FAILURE"

    if status == "ERROR" and result == "FAIL":
        result = "UNKNOWN"
        sufficiency = "FAILED"
        reason = reason or "ACQUISITION_ERROR_IS_NOT_SUBJECT_FAILURE"

    return {
        "dimension": dimension,
        "status": status,
        "result": result,
        "sufficiency": sufficiency,
        "freshness": freshness,
        "evidence_refs": evidence_refs,
        "effect_evidence_refs": effect_evidence_refs,
        "owner_refs": owner_refs,
        "mechanism_refs": mechanism_refs,
        "false_green_rejected": false_green_rejected,
        "reason": reason or None,
        "details": _dict(raw.get("details")),
    }


def _resolve_objective(snapshot: dict[str, Any], basis: str) -> dict[str, Any]:
    raw = _dict(snapshot.get("objective_alignment"))
    evidence, effect, owners, mechanisms = _evidence(raw)
    resolved_obj = _text(raw.get("resolved_objective_ref"))
    current_obj = _text(raw.get("current_objective_ref"))
    resolved_scope = _text(raw.get("resolved_scope_ref"))
    current_scope = _text(raw.get("current_scope_ref"))
    if not resolved_obj or not current_obj:
        return _unknown_dimension("OBJECTIVE_ALIGNMENT", basis, "RESOLVED_AND_CURRENT_OBJECTIVE_REQUIRED")
    mismatch = resolved_obj != current_obj or (resolved_scope and current_scope and resolved_scope != current_scope)
    silent_reframe = raw.get("silent_material_reframe_known") is True
    assumptions_exposed = raw.get("material_assumptions_exposed")
    if mismatch or silent_reframe:
        result, reason = "FAIL", "OBJECTIVE_OR_SCOPE_DRIFT_DETECTED"
    elif assumptions_exposed is False:
        result, reason = "FAIL", "MATERIAL_ASSUMPTIONS_NOT_EXPOSED"
    elif assumptions_exposed is True:
        result, reason = "PASS", "RESOLVED_OBJECTIVE_AND_CURRENT_WORK_ALIGN"
    else:
        result, reason = "UNKNOWN", "MATERIAL_ASSUMPTION_EXPOSURE_UNRESOLVED"
    return _derived_row(
        "OBJECTIVE_ALIGNMENT", basis, result=result,
        status="COMPLETE" if result != "UNKNOWN" else "PARTIAL",
        sufficiency="COMPLETE" if result != "UNKNOWN" else "INSUFFICIENT",
        evidence_refs=evidence, effect_evidence_refs=effect, owner_refs=owners or ["interaction"],
        mechanism_refs=mechanisms, reason=reason,
        details={"resolved_objective_ref": resolved_obj, "current_objective_ref": current_obj,
                 "resolved_scope_ref": resolved_scope or None, "current_scope_ref": current_scope or None},
    )


def _resolve_mcp_loop(snapshot: dict[str, Any], basis: str) -> dict[str, Any]:
    raw = _dict(snapshot.get("mcp_loop_integrity"))
    evidence, effect, owners, mechanisms = _evidence(raw)
    required_refs = {
        "previous_state_ref": _text(raw.get("previous_state_ref")),
        "current_state_ref": _text(raw.get("current_state_ref")),
        "next_control_event_ref": _text(raw.get("next_control_event_ref")),
    }
    known_false = []
    for key in ("mcp_governs_current_work", "governing_contracts_preserved", "assurance_preserved"):
        if raw.get(key) is False:
            known_false.append(key)
    if raw.get("known_control_bypass") is True:
        known_false.append("known_control_bypass")
    if known_false:
        result, reason = "FAIL", "MCP_CONTROL_CONTINUITY_BROKEN"
    elif not all(required_refs.values()):
        result, reason = "UNKNOWN", "PREVIOUS_CURRENT_NEXT_NOT_FULLY_RESOLVED"
    elif any(raw.get(key) is not True for key in ("mcp_governs_current_work", "governing_contracts_preserved", "assurance_preserved")):
        result, reason = "UNKNOWN", "MCP_GOVERNANCE_OR_ASSURANCE_UNRESOLVED"
    elif raw.get("known_control_bypass") not in {False, None}:
        result, reason = "UNKNOWN", "CONTROL_BYPASS_STATE_UNRESOLVED"
    else:
        result, reason = "PASS", "PREVIOUS_CURRENT_NEXT_AND_GOVERNANCE_COHERENT"
    return _derived_row(
        "MCP_LOOP_INTEGRITY", basis, result=result,
        status="COMPLETE" if result != "UNKNOWN" else "PARTIAL",
        sufficiency="COMPLETE" if result != "UNKNOWN" else "INSUFFICIENT",
        evidence_refs=evidence, effect_evidence_refs=effect, owner_refs=owners or ["MCP"],
        mechanism_refs=mechanisms, reason=reason, details=required_refs,
    )


def _resolve_work_position(snapshot: dict[str, Any], basis: str) -> dict[str, Any]:
    raw = _dict(snapshot.get("work_position"))
    evidence, effect, owners, mechanisms = _evidence(raw)
    if raw.get("applicable") is False:
        return _derived_row(
            "WORK_POSITION", basis, result="NOT_APPLICABLE", evidence_refs=evidence,
            owner_refs=owners or ["project", "context"], mechanism_refs=mechanisms,
            reason="NO_PROJECT_OR_CONTEXT_POSITION_APPLICABLE",
        )
    if raw.get("applicable") is not True:
        return _unknown_dimension("WORK_POSITION", basis, "WORK_POSITION_APPLICABILITY_UNRESOLVED")
    if raw.get("binding_validated") is False or raw.get("position_coherent") is False or raw.get("side_branch_drift_known") is True:
        result, reason = "FAIL", "PROJECT_CONTEXT_WORK_POSITION_INCOHERENT"
    elif raw.get("binding_validated") is True and raw.get("position_coherent") is True and raw.get("side_branch_drift_known") is False:
        result, reason = "PASS", "PROJECT_CONTEXT_WORK_POSITION_COHERENT"
    else:
        result, reason = "UNKNOWN", "WORK_POSITION_EVIDENCE_INCOMPLETE"
    return _derived_row(
        "WORK_POSITION", basis, result=result,
        status="COMPLETE" if result != "UNKNOWN" else "PARTIAL",
        sufficiency="COMPLETE" if result != "UNKNOWN" else "INSUFFICIENT",
        evidence_refs=evidence, effect_evidence_refs=effect, owner_refs=owners or ["project", "context"],
        mechanism_refs=mechanisms, reason=reason,
        details={k: raw.get(k) for k in ("project_ref", "session_ref", "active_context_ref", "main_track_ref", "active_focus_ref")},
    )


def _resolve_workform(snapshot: dict[str, Any], basis: str) -> dict[str, Any]:
    raw = _dict(snapshot.get("workform_adequacy"))
    evidence, effect, owners, mechanisms = _evidence(raw)
    required = _upper(raw.get("required_depth"))
    selected = _upper(raw.get("selected_depth"))
    required_ops = set(_sorted_strings(raw.get("required_operations")))
    completed_ops = set(_sorted_strings(raw.get("completed_operations")))
    missing_ops = sorted(required_ops - completed_ops)
    if required not in DEPTH_RANK or selected not in DEPTH_RANK:
        return _unknown_dimension("WORKFORM_ADEQUACY", basis, "REQUIRED_AND_SELECTED_WORKFORM_DEPTH_REQUIRED")
    if raw.get("silent_downgrade_known") is True or DEPTH_RANK[selected] < DEPTH_RANK[required]:
        result, reason = "FAIL", "GOVERNING_WORKFORM_DEPTH_NOT_PRESERVED"
    elif required == "DEEP" and missing_ops:
        result, reason = "FAIL", "RELEVANT_DEEP_OPERATIONS_NOT_COMPLETED"
    elif raw.get("quality_trace_current") is False:
        result, reason = "FAIL", "WORKFORM_EVIDENCE_STALE_OR_NOT_CURRENT"
    elif raw.get("quality_trace_current") is True:
        result, reason = "PASS", "GOVERNING_WORKFORM_AND_RELEVANT_OPERATIONS_SATISFIED"
    else:
        result, reason = "UNKNOWN", "WORKFORM_EFFECT_EVIDENCE_UNRESOLVED"
    return _derived_row(
        "WORKFORM_ADEQUACY", basis, result=result,
        status="COMPLETE" if result != "UNKNOWN" else "PARTIAL",
        sufficiency="COMPLETE" if result != "UNKNOWN" else "INSUFFICIENT",
        evidence_refs=evidence, effect_evidence_refs=effect, owner_refs=owners or ["quality", "MCP"],
        mechanism_refs=mechanisms, reason=reason,
        details={"required_depth": required, "selected_depth": selected, "missing_required_operations": missing_ops},
    )


def _resolve_basis(snapshot: dict[str, Any], basis: str) -> dict[str, Any]:
    raw = _dict(snapshot.get("basis_and_prior_knowledge"))
    evidence, effect, owners, mechanisms = _evidence(raw)
    required_true = (
        "authoritative_basis_current",
        "source_freshness_need_resolved",
        "existing_mechanism_assessed",
        "existing_owner_assessed",
        "parallel_mechanism_risk_assessed",
    )
    failed = [key for key in required_true if raw.get(key) is False]
    prior_applicable = raw.get("prior_learning_applicable") is True
    failure_applicable = raw.get("failure_families_applicable") is True
    if prior_applicable and raw.get("prior_learning_applied") is False:
        failed.append("prior_learning_applied")
    if failure_applicable and raw.get("failure_families_applied") is False:
        failed.append("failure_families_applied")
    if failed:
        result, reason = "FAIL", "CURRENT_BASIS_OR_RELEVANT_PRIOR_KNOWLEDGE_NOT_SATISFIED"
    elif any(raw.get(key) is not True for key in required_true):
        result, reason = "UNKNOWN", "CURRENT_BASIS_RESOLUTION_INCOMPLETE"
    elif prior_applicable and raw.get("prior_learning_applied") is not True:
        result, reason = "UNKNOWN", "RELEVANT_PRIOR_LEARNING_APPLICATION_UNRESOLVED"
    elif failure_applicable and raw.get("failure_families_applied") is not True:
        result, reason = "UNKNOWN", "RELEVANT_FAILURE_FAMILY_APPLICATION_UNRESOLVED"
    else:
        result, reason = "PASS", "CURRENT_BASIS_AND_RELEVANT_PRIOR_KNOWLEDGE_SATISFIED"
    return _derived_row(
        "BASIS_AND_PRIOR_KNOWLEDGE", basis, result=result,
        status="COMPLETE" if result != "UNKNOWN" else "PARTIAL",
        sufficiency="COMPLETE" if result != "UNKNOWN" else "INSUFFICIENT",
        evidence_refs=evidence, effect_evidence_refs=effect, owner_refs=owners or ["MCP", "context"],
        mechanism_refs=mechanisms, reason=reason,
        details={
            "basis_ref": raw.get("basis_ref"),
            "prior_learning_refs": _sorted_strings(raw.get("prior_learning_refs")),
            "failure_family_refs": _sorted_strings(raw.get("failure_family_refs")),
            "failed_requirements": failed,
        },
    )


def select_gate_profile(snapshot: dict[str, Any]) -> str | None:
    raw = _dict(snapshot.get("next_gate_readiness"))
    explicit = _upper(raw.get("gate_profile"))
    if explicit:
        if explicit not in GATE_PROFILES:
            raise IntegrityResolutionError(f"unknown-gate-profile:{explicit}")
        return explicit
    stage = _upper(raw.get("stage"))
    if stage in {"DECIDE", "LOCK"}:
        return "DECISION_READINESS"
    if raw.get("patch_handoff_requested") is True:
        return "PATCH_READINESS"
    if raw.get("operational_claim_requested") is True:
        return "OPERATIONAL_INTEGRATION"
    if raw.get("delivery_requested") is True:
        return "DELIVERY_CONFORMITY"
    if raw.get("implementation_requested") is True:
        return "IMPLEMENTATION_READINESS"
    return None


def _resolve_next_gate(snapshot: dict[str, Any], basis: str) -> dict[str, Any]:
    raw = _dict(snapshot.get("next_gate_readiness"))
    evidence, effect, owners, mechanisms = _evidence(raw)
    profile = select_gate_profile(snapshot)
    next_event = _text(raw.get("next_control_event_ref"))
    if profile is None:
        return _derived_row(
            "NEXT_GATE_READINESS", basis, result="NOT_APPLICABLE", evidence_refs=evidence,
            owner_refs=owners or ["MCP"], mechanism_refs=mechanisms,
            reason="NO_MATERIAL_NEXT_GATE_APPLICABLE", details={"next_control_event_ref": next_event or None},
        )
    required_checks = set(_sorted_strings(raw.get("required_checks")))
    passed_checks = set(_sorted_strings(raw.get("passed_checks")))
    failed_checks = set(_sorted_strings(raw.get("failed_checks")))
    unavailable_checks = set(_sorted_strings(raw.get("unavailable_checks")))
    missing = sorted(required_checks - passed_checks - failed_checks - unavailable_checks)
    if failed_checks:
        result, reason = "FAIL", "NEXT_GATE_REQUIRED_CHECK_FAILED"
    elif unavailable_checks or missing or not next_event:
        result, reason = "UNKNOWN", "NEXT_GATE_READINESS_INSUFFICIENT"
    elif required_checks.issubset(passed_checks):
        result, reason = "PASS", "NEXT_GATE_READY"
    else:
        result, reason = "UNKNOWN", "NEXT_GATE_READINESS_UNRESOLVED"
    return _derived_row(
        "NEXT_GATE_READINESS", basis, result=result,
        status="COMPLETE" if result != "UNKNOWN" else ("UNAVAILABLE" if unavailable_checks and not passed_checks else "PARTIAL"),
        sufficiency="COMPLETE" if result != "UNKNOWN" else "INSUFFICIENT",
        evidence_refs=evidence, effect_evidence_refs=effect, owner_refs=owners or ["MCP"],
        mechanism_refs=mechanisms, reason=reason,
        details={"gate_profile": profile, "next_control_event_ref": next_event or None,
                 "required_checks": sorted(required_checks), "missing_checks": missing,
                 "failed_checks": sorted(failed_checks), "unavailable_checks": sorted(unavailable_checks)},
    )


def derive_dimension_assessments(snapshot: dict[str, Any], basis_fingerprint: str) -> dict[str, dict[str, Any]]:
    return {
        "OBJECTIVE_ALIGNMENT": _resolve_objective(snapshot, basis_fingerprint),
        "MCP_LOOP_INTEGRITY": _resolve_mcp_loop(snapshot, basis_fingerprint),
        "WORK_POSITION": _resolve_work_position(snapshot, basis_fingerprint),
        "WORKFORM_ADEQUACY": _resolve_workform(snapshot, basis_fingerprint),
        "BASIS_AND_PRIOR_KNOWLEDGE": _resolve_basis(snapshot, basis_fingerprint),
        "NEXT_GATE_READINESS": _resolve_next_gate(snapshot, basis_fingerprint),
    }


def aggregate(rows: list[dict[str, Any]], coverage_mode: str) -> dict[str, Any]:
    applicable = [row for row in rows if row["result"] != "NOT_APPLICABLE"]
    if not applicable:
        result = "NOT_APPLICABLE"
    elif any(row["result"] == "FAIL" for row in applicable):
        result = "FAIL"
    elif any(row["result"] == "UNKNOWN" for row in applicable):
        result = "UNKNOWN"
    elif all(row["result"] == "PASS" for row in applicable):
        result = "PASS"
    else:
        result = "UNKNOWN"

    if any(row["status"] == "ERROR" for row in applicable):
        status = "ERROR"
    elif applicable and all(row["status"] == "UNAVAILABLE" for row in applicable):
        status = "UNAVAILABLE"
    elif any(row["status"] in {"PARTIAL", "UNAVAILABLE"} for row in applicable):
        status = "PARTIAL"
    else:
        status = "COMPLETE"

    if any(row["sufficiency"] == "FAILED" for row in applicable):
        sufficiency = "FAILED"
    elif any(row["sufficiency"] == "INSUFFICIENT" for row in applicable):
        sufficiency = "INSUFFICIENT"
    elif any(row["sufficiency"] == "PARTIAL" for row in applicable):
        sufficiency = "PARTIAL"
    else:
        sufficiency = "COMPLETE"

    full_coverage_complete = all(
        row["result"] == "NOT_APPLICABLE"
        or (row["status"] == "COMPLETE" and row["sufficiency"] == "COMPLETE" and row["result"] in {"PASS", "FAIL"})
        for row in rows
    )
    coverage_complete = full_coverage_complete if coverage_mode == "FULL" else sufficiency == "COMPLETE"
    return {
        "status": status,
        "result": result,
        "sufficiency": sufficiency,
        "coverage_mode": coverage_mode,
        "coverage_complete": coverage_complete,
        "applicable_dimension_count": len(applicable),
        "false_green_rejection_count": sum(1 for row in rows if row["false_green_rejected"]),
    }


def _control_implications(rows: list[dict[str, Any]], gate_enforcement_required: bool) -> dict[str, Any]:
    recommendation: str | None = None
    reasons: list[str] = []
    for row in rows:
        if row["result"] != "FAIL":
            continue
        dim = row["dimension"]
        proposed = (
            "REORIENT" if dim in {"OBJECTIVE_ALIGNMENT", "WORK_POSITION"}
            else "REMEDIATE" if dim == "WORKFORM_ADEQUACY"
            else "BLOCK"
        )
        if OUTCOME_PRECEDENCE[proposed] > OUTCOME_PRECEDENCE[recommendation]:
            recommendation = proposed
        reasons.append(f"{dim}:{row.get('reason') or 'FAIL'}")
    if recommendation is None and gate_enforcement_required:
        unresolved = [row["dimension"] for row in rows if row["result"] == "UNKNOWN"]
        if unresolved:
            recommendation = "BLOCK"
            reasons.append("MATERIAL_GATE_INTEGRITY_EVIDENCE_INSUFFICIENT:" + ",".join(unresolved))
    return {
        "authority": "DERIVED_CONTROL_RECOMMENDATION",
        "final_control_decision_owner": "MCP",
        "recommended_mcp_outcome": recommendation,
        "reasons": reasons,
        "may_override_existing_stronger_block": False,
    }


def resolve_invocation(request: dict[str, Any], intent: dict[str, Any] | None = None) -> dict[str, Any]:
    intent = intent if isinstance(intent, dict) else {}
    reasons: list[str] = []
    coverage_mode = "ADAPTIVE"
    primary_scope = "ALL"
    if intent.get("recognized") is True:
        reasons.append("MANUAL_ENTRYPOINT:" + str(intent.get("canonical_command") or "Integrity"))
        coverage_mode = _upper(intent.get("coverage_mode"), "ADAPTIVE")
        primary_scope = _upper(intent.get("primary_scope"), "ALL")
    if request.get("integrity_required") is True:
        reasons.append("EXPLICIT_INTEGRITY_REQUIRED")
    stage = _upper(request.get("stage"))
    if stage in {"DECIDE", "LOCK"}:
        reasons.append("DECISION_READINESS_BOUNDARY")
    if request.get("implementation_requested") is True:
        reasons.append("IMPLEMENTATION_READINESS_BOUNDARY")
    if request.get("patch_handoff_requested") is True:
        reasons.append("PATCH_READINESS_BOUNDARY")
    if request.get("operational_claim_requested") is True:
        reasons.append("OPERATIONAL_INTEGRATION_BOUNDARY")
    if request.get("delivery_requested") is True:
        reasons.append("DELIVERY_CONFORMITY_BOUNDARY")
    return {
        "required": bool(reasons),
        "coverage_mode": coverage_mode,
        "primary_scope": primary_scope,
        "reasons": sorted(set(reasons)),
        "same_path_for_manual_and_automatic": True,
    }


def resolve(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise IntegrityResolutionError("request-must-be-object")
    basis_fingerprint = _text(request.get("basis_fingerprint"))
    if not basis_fingerprint:
        raise IntegrityResolutionError("basis-fingerprint-required")
    control_event_ref = _text(request.get("control_event_ref"))
    if not control_event_ref:
        raise IntegrityResolutionError("control-event-ref-required")
    coverage_mode = _upper(request.get("coverage_mode"), "ADAPTIVE")
    if coverage_mode not in COVERAGE_MODES:
        raise IntegrityResolutionError(f"invalid-coverage-mode:{coverage_mode}")

    raw_dimensions = request.get("dimension_assessments")
    if not isinstance(raw_dimensions, dict):
        snapshot = _dict(request.get("control_snapshot"))
        raw_dimensions = derive_dimension_assessments(snapshot, basis_fingerprint)
    rows = [normalize_dimension(name, raw_dimensions.get(name), basis_fingerprint) for name in DIMENSIONS]
    summary = aggregate(rows, coverage_mode)
    evidence_refs = sorted({ref for row in rows for ref in row["evidence_refs"]})
    applicable_dimensions = [row["dimension"] for row in rows if row["result"] != "NOT_APPLICABLE"]
    gate_row = next(row for row in rows if row["dimension"] == "NEXT_GATE_READINESS")
    gate_profile = gate_row.get("details", {}).get("gate_profile")
    gate_enforcement_required = bool(request.get("gate_enforcement_required")) or gate_profile is not None
    implications = _control_implications(rows, gate_enforcement_required)
    primary_scope = _upper(request.get("primary_scope"), "ALL")

    identity_material = {
        "control_id": CONTROL_ID,
        "control_event_ref": control_event_ref,
        "basis_fingerprint": basis_fingerprint,
        "project_context_refs": _sorted_strings(request.get("project_context_refs")),
        "resolved_objective_ref": _text(request.get("resolved_objective_ref")),
        "governing_workform_ref": _text(request.get("governing_workform_ref")),
        "next_gate_ref": _text(request.get("next_gate_ref")) or _text(gate_row.get("details", {}).get("next_control_event_ref")),
        "coverage_mode": coverage_mode,
        "primary_scope": primary_scope,
        "invalidation_triggers": _sorted_strings(request.get("invalidation_triggers")),
        "dimensions": rows,
    }
    assessment_id = "INTG-" + fingerprint(identity_material)[:20].upper()
    return {
        "schema": SCHEMA,
        "assessment_id": assessment_id,
        "control_ref": CONTROL_ID,
        "authority": AUTHORITY,
        "direct_live_authority": False,
        "final_control_decision_owner": "MCP",
        "control_event_ref": control_event_ref,
        "basis_fingerprint": basis_fingerprint,
        "project_context_refs": identity_material["project_context_refs"],
        "resolved_objective_ref": identity_material["resolved_objective_ref"] or None,
        "governing_workform_ref": identity_material["governing_workform_ref"] or None,
        "applicable_dimensions": applicable_dimensions,
        "primary_scope": primary_scope,
        "next_gate_ref": identity_material["next_gate_ref"] or None,
        "gate_profile": gate_profile,
        "gate_enforcement_required": gate_enforcement_required,
        "evidence_refs": evidence_refs,
        "invalidation_triggers": identity_material["invalidation_triggers"],
        "status": summary["status"],
        "result": summary["result"],
        "sufficiency": summary["sufficiency"],
        "coverage_mode": coverage_mode,
        "coverage_complete": summary["coverage_complete"],
        "false_green_rejection_count": summary["false_green_rejection_count"],
        "dimensions": rows,
        "control_implications": implications,
        "full_is_coverage_deep_is_workform_depth": True,
        "mechanism_existence_is_not_effect_evidence": True,
        "resolved_at": utc_now(),
    }


def selftest() -> dict[str, Any]:
    basis = "b" * 64
    def obs(name: str, **kwargs: Any) -> dict[str, Any]:
        value = {
            "evidence_refs": [f"EVIDENCE:{name}"],
            "effect_evidence_refs": [f"EVIDENCE:{name}"],
        }
        value.update(kwargs)
        return value

    snapshot = {
        "objective_alignment": obs("OBJECTIVE", resolved_objective_ref="OBJ", current_objective_ref="OBJ", material_assumptions_exposed=True),
        "mcp_loop_integrity": obs("MCP", previous_state_ref="PREV", current_state_ref="CUR", next_control_event_ref="NEXT", mcp_governs_current_work=True, governing_contracts_preserved=True, assurance_preserved=True, known_control_bypass=False),
        "work_position": obs("POSITION", applicable=True, binding_validated=True, position_coherent=True, side_branch_drift_known=False, project_ref="P", session_ref="S", active_context_ref="C"),
        "workform_adequacy": obs("WORKFORM", required_depth="DEEP", selected_depth="DEEP", required_operations=["REFINE", "CRITIQUE", "XREF", "CONSOLIDATE", "COMPARE", "CONVERGE", "VERIFY", "LEARN"], completed_operations=["REFINE", "CRITIQUE", "XREF", "CONSOLIDATE", "COMPARE", "CONVERGE", "VERIFY", "LEARN"], quality_trace_current=True, silent_downgrade_known=False),
        "basis_and_prior_knowledge": obs("BASIS", authoritative_basis_current=True, source_freshness_need_resolved=True, existing_mechanism_assessed=True, existing_owner_assessed=True, parallel_mechanism_risk_assessed=True, prior_learning_applicable=True, prior_learning_applied=True, failure_families_applicable=True, failure_families_applied=True),
        "next_gate_readiness": obs("GATE", gate_profile="IMPLEMENTATION_READINESS", next_control_event_ref="IMPLEMENT", required_checks=["ARCHITECTURE_CONVERGED", "CURRENT_BASIS", "PRIOR_LEARNING", "OWNERSHIP_RESOLVED", "IMPLEMENTATION_PATH"], passed_checks=["ARCHITECTURE_CONVERGED", "CURRENT_BASIS", "PRIOR_LEARNING", "OWNERSHIP_RESOLVED", "IMPLEMENTATION_PATH"]),
    }
    positive = resolve({
        "control_event_ref": "EVENT-POSITIVE", "basis_fingerprint": basis, "coverage_mode": "FULL",
        "resolved_objective_ref": "OBJ", "governing_workform_ref": "DEEP", "control_snapshot": snapshot,
    })

    mechanism_only = json.loads(json.dumps(snapshot))
    mechanism_only["mcp_loop_integrity"]["effect_evidence_refs"] = []
    mechanism_only_result = resolve({"control_event_ref": "EVENT-FG", "basis_fingerprint": basis, "control_snapshot": mechanism_only})

    stale_rows = derive_dimension_assessments(snapshot, basis)
    stale_rows["WORK_POSITION"]["freshness"] = _freshness("c" * 64)
    stale = resolve({"control_event_ref": "EVENT-STALE", "basis_fingerprint": basis, "dimension_assessments": stale_rows})

    unavailable_rows = derive_dimension_assessments(snapshot, basis)
    unavailable_rows["OBJECTIVE_ALIGNMENT"].update({"status": "UNAVAILABLE", "result": "FAIL", "sufficiency": "INSUFFICIENT", "effect_evidence_refs": []})
    unavailable = resolve({"control_event_ref": "EVENT-UNAVAILABLE", "basis_fingerprint": basis, "dimension_assessments": unavailable_rows})

    deep_missing = json.loads(json.dumps(snapshot))
    deep_missing["workform_adequacy"]["completed_operations"].remove("LEARN")
    deep_missing_result = resolve({"control_event_ref": "EVENT-DEEP", "basis_fingerprint": basis, "control_snapshot": deep_missing})

    gate_unknown = json.loads(json.dumps(snapshot))
    gate_unknown["next_gate_readiness"]["passed_checks"].remove("OWNERSHIP_RESOLVED")
    gate_unknown_result = resolve({"control_event_ref": "EVENT-GATE", "basis_fingerprint": basis, "control_snapshot": gate_unknown})

    checks = {
        "all_six_dimensions_present": len(positive["dimensions"]) == 6,
        "full_all_effect_proven_pass": positive["result"] == "PASS" and positive["coverage_complete"] is True,
        "direct_live_authority_false": positive["direct_live_authority"] is False,
        "authority_is_derived_control_evidence": positive["authority"] == AUTHORITY,
        "false_green_mechanism_only_rejected": mechanism_only_result["result"] == "UNKNOWN" and mechanism_only_result["false_green_rejection_count"] >= 1,
        "unavailable_is_not_subject_failure": unavailable["result"] == "UNKNOWN",
        "stale_pass_is_rejected": stale["result"] == "UNKNOWN" and stale["false_green_rejection_count"] >= 1,
        "full_and_deep_axes_separate": positive["coverage_mode"] == "FULL" and positive["governing_workform_ref"] == "DEEP",
        "deep_selected_not_enough": deep_missing_result["result"] == "FAIL" and deep_missing_result["control_implications"]["recommended_mcp_outcome"] == "REMEDIATE",
        "next_gate_incomplete_blocks_material_gate": gate_unknown_result["result"] == "UNKNOWN" and gate_unknown_result["control_implications"]["recommended_mcp_outcome"] == "BLOCK",
        "gate_profile_is_existing_profile": positive["gate_profile"] == "IMPLEMENTATION_READINESS",
    }
    return {"schema": "cerebro-mcp-integrity-selftest/v2", "result": "PASS" if all(checks.values()) else "FAIL", "checks": checks}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise IntegrityResolutionError("json-object-required")
    return value


def _emit(value: dict[str, Any], output: str | None) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=True) + "\n"
    if output:
        Path(output).write_text(text, encoding="ascii")
    else:
        print(text, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro MCP Integrity subresolver")
    sub = parser.add_subparsers(dest="command", required=True)
    p_resolve = sub.add_parser("resolve")
    p_resolve.add_argument("--request", required=True)
    p_resolve.add_argument("--output")
    p_selftest = sub.add_parser("selftest")
    p_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        result = resolve(_read_json(Path(args.request))) if args.command == "resolve" else selftest()
        _emit(result, getattr(args, "output", None))
        if args.command == "selftest":
            return 0 if result.get("result") == "PASS" else 1
        # Integrity FAIL is a valid subject assessment, not process failure.
        return 0
    except Exception as exc:
        failure = {"result": "ERROR", "error": str(exc), "error_class": type(exc).__name__}
        _emit(failure, getattr(args, "output", None))
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
