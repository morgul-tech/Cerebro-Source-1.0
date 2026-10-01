"""Pure, non-authoritative Control vNext compiler candidate.

This module classifies bounded intent and trusted-fact *shapes*. It never sends,
admits, mutates Source, or grants runtime authority. A host must bind facts and
perform currentness/effect checks before acting on a proposed route.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

SCHEMA = "cerebro-control-metabolism-vnext-candidate/v0.1"
DISTANCE = (
    "ORIENTED", "OWNER_CLEAR", "READY_TO_ACT", "EFFECT_ATTEMPTED",
    "READBACK", "CLOSED",
)
PM_REASONS = {
    "PRIORITY", "NOVELTY", "CROSS_SCOPE_CONFLICT", "SHARED_MATERIAL_EFFECT",
    "POLICY_AUTHORITY_EDGE", "AMBIGUOUS_TERMINAL",
}


class ContractError(ValueError):
    pass


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ContractError(reason)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strset(value: Any, name: str) -> set[str]:
    _require(isinstance(value, list) and all(_nonempty(x) for x in value), name)
    _require(len(value) == len(set(value)), f"{name}-duplicate")
    return set(value)


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_intent(intent: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    for key in ("intent_id", "human_goal", "requested_effect", "success_criteria",
                "scope", "scope_expansion_authority", "material_effect_boundary",
                "human_gate_boundary", "project_ref_or_none", "way_home"):
        _require(_nonempty(intent.get(key)), f"intent-{key}-required")
    allowed = _strset(intent.get("allowed_work_classes"), "intent-allowed-work-classes")
    _require(bool(allowed), "intent-allowed-work-classes-empty")
    non_goals = _strset(intent.get("explicit_non_goals"), "intent-explicit-non-goals")
    _strset(intent.get("hard_now_qualifications"), "intent-hard-now-qualifications")
    deferred = intent.get("deferred_qualifications")
    _require(isinstance(deferred, list), "intent-deferred-qualifications-required")
    for item in deferred:
        _require(isinstance(item, Mapping), "deferred-qualification-object-required")
        for key in ("qualification", "activation_trigger", "owner", "proof_duty"):
            _require(_nonempty(item.get(key)), f"deferred-{key}-required")
    _require(intent.get("effect_distance_state") in DISTANCE, "effect-distance-invalid")
    resource = intent.get("resource_envelope")
    _require(isinstance(resource, Mapping), "resource-envelope-required")
    _require(resource.get("resource_posture") in {"SPAREBLUSS", "NORMALDRIFT", "FULLT_KJOR_LOCAL"},
             "resource-posture-invalid")
    _require(resource.get("pre_effect_cognition_budget_class") in {"TINY", "SMALL", "MEDIUM", "EXPLICIT_LARGE"},
             "resource-cognition-budget-invalid")
    _require(resource.get("provider_refresh_budget_class") in {"NONE", "BOUNDED", "EXPLICIT_LARGE"},
             "resource-provider-budget-invalid")
    _require(resource.get("human_optional_acceleration") in {"ALLOWED", "DISALLOWED", "NOT_APPLICABLE"},
             "resource-human-acceleration-invalid")
    cap = resource.get("parallelism_cap_or_ELASTIC")
    _require(cap == "ELASTIC" or (isinstance(cap, int) and not isinstance(cap, bool) and cap > 0),
             "resource-parallelism-cap-invalid")
    _require(isinstance(resource.get("stagnation_limit"), int) and resource["stagnation_limit"] >= 0,
             "resource-stagnation-limit-invalid")
    _require(_nonempty(resource.get("reframe_owner")), "resource-reframe-owner-required")
    return allowed, non_goals


def _validate_lease(lease: Mapping[str, Any], intent: Mapping[str, Any], allowed: set[str]) -> set[str]:
    _require(lease.get("intent_ref") == intent["intent_id"], "lease-intent-mismatch")
    for key in ("scope", "material_effect_boundary", "way_home"):
        _require(lease.get(key) == intent[key], f"lease-{key}-mismatch")
    lease_allowed = _strset(lease.get("allowed_work_classes"), "lease-allowed-work-classes")
    _require(bool(lease_allowed) and lease_allowed <= allowed, "lease-expands-allowed-work")
    _require(_nonempty(lease.get("lease_ref")), "lease-ref-required")
    _require(_nonempty(lease.get("owner_set_or_resolution_rule")), "lease-owner-rule-required")
    _require(lease.get("resource_posture") == intent["resource_envelope"]["resource_posture"],
             "lease-resource-posture-mismatch")
    _strset(lease.get("PM_escalation_conditions"), "lease-pm-escalation-conditions")
    _strset(lease.get("Human_gate_conditions"), "lease-human-gate-conditions")
    _require(_nonempty(lease.get("expiry_or_revalidation")), "lease-expiry-or-revalidation-required")
    return lease_allowed


def _result(route: str, intent: Mapping[str, Any], *, reason: str,
            target_capability: str | None = None,
            attempt_ref: str | None = None) -> dict[str, Any]:
    result = {
        "schema": SCHEMA, "authority": "NONE", "effect_authorized": False,
        "route": route, "reason": reason, "intent_ref": intent["intent_id"],
        "way_home": intent["way_home"],
    }
    if route == "PM_NOT_NEEDED_DISPATCH_GRANT":
        result["dispatch_grant_candidate"] = {
            "intent_ref": intent["intent_id"], "attempt_ref": attempt_ref,
            "target_capability": target_capability,
            "transport_owner": "DIRIGENT", "host_currentness_required": True,
            "send_once_required": True, "authority": "NONE",
        }
    result["fingerprint"] = _fingerprint(result)
    return result


def compile_candidate(intent: Mapping[str, Any], lease: Mapping[str, Any] | None,
                      facts: Mapping[str, Any]) -> dict[str, Any]:
    """Propose a route from bounded intent, lease and externally bound facts.

    Facts are deliberately not self-attesting. Callers cannot use this return as
    an effect permit, dispatch receipt, PM decision, or terminal admission.
    """
    _require(isinstance(intent, Mapping), "intent-object-required")
    _require(isinstance(facts, Mapping), "facts-object-required")
    allowed, non_goals = _validate_intent(intent)
    _require(facts.get("intent_ref") == intent["intent_id"], "facts-intent-mismatch")
    if facts.get("work_requested") is not True:
        return _result("QUIET", intent, reason="NO_WORK_REQUESTED")
    work_class = facts.get("work_class")
    _require(_nonempty(work_class), "facts-work-class-required")
    if work_class in non_goals or work_class not in allowed:
        return _result("HOLD_EXACT", intent, reason="SCOPE_EXPANSION_REQUIRES_EXPLICIT_GATE")
    if lease is None:
        return _result("HOLD_EXACT", intent, reason="PROJECT_LEASE_REQUIRED")
    _require(isinstance(lease, Mapping), "lease-object-required")
    lease_allowed = _validate_lease(lease, intent, allowed)
    if work_class not in lease_allowed:
        return _result("HOLD_EXACT", intent, reason="WORK_CLASS_OUTSIDE_LEASE")
    if facts.get("lease_current") is not True:
        return _result("HOLD_EXACT", intent, reason="LEASE_CURRENTNESS_UNPROVEN")
    if facts.get("human_gate_required") is True:
        return _result("HUMAN_GATE", intent, reason="TRUE_HUMAN_GATE")
    pm_reasons = _strset(facts.get("pm_reasons", []), "facts-pm-reasons")
    _require(pm_reasons <= PM_REASONS, "facts-pm-reason-invalid")
    if pm_reasons:
        return _result("PM_NEEDED", intent, reason=sorted(pm_reasons)[0])
    resource = intent["resource_envelope"]
    count = facts.get("stagnation_count", 0)
    _require(isinstance(count, int) and count >= 0, "facts-stagnation-count-invalid")
    if (facts.get("material_resource_consumed") is True
            and facts.get("effect_distance_advanced") is not True
            and facts.get("hard_now_uncertainty_retired") is not True
            and count >= resource["stagnation_limit"]):
        return _result("HOLD_EXACT", intent, reason="STOP_AND_REFRAME")
    hard_now = set(intent["hard_now_qualifications"])
    proven = _strset(facts.get("proven_qualifications", []), "facts-proven-qualifications")
    active_triggers = _strset(facts.get("active_qualification_triggers", []),
                              "facts-active-qualification-triggers")
    for item in intent["deferred_qualifications"]:
        if item["activation_trigger"] in active_triggers:
            hard_now.add(item["qualification"])
    missing = hard_now - proven
    if missing:
        return _result("HOLD_EXACT", intent, reason="HARD_NOW_UNPROVEN:" + sorted(missing)[0])
    if facts.get("active_claim_collision") is True:
        return _result("PM_NEEDED", intent, reason="CROSS_SCOPE_CONFLICT")
    if facts.get("shared_effect_collision") is True:
        return _result("PM_NEEDED", intent, reason="SHARED_MATERIAL_EFFECT")
    target = facts.get("target_capability")
    if not _nonempty(target) or facts.get("target_available") is not True:
        return _result("HOLD_EXACT", intent, reason="TARGET_CAPABILITY_UNAVAILABLE")
    _require(_nonempty(facts.get("attempt_ref")), "dispatch-attempt-ref-required")
    return _result("PM_NOT_NEEDED_DISPATCH_GRANT", intent,
                   reason="OWNER_CLEAR_WITHIN_LEASE", target_capability=target,
                   attempt_ref=facts["attempt_ref"])


def classify_transport_receipt_candidate(grant: Mapping[str, Any],
                                         receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Keep provider submission and worker consumption as separate evidence axes."""
    _require(isinstance(grant, Mapping) and isinstance(receipt, Mapping),
             "transport-grant-and-receipt-objects-required")
    _require(grant.get("authority") == "NONE", "transport-grant-candidate-required")
    for key in ("intent_ref", "attempt_ref"):
        _require(_nonempty(grant.get(key)) and receipt.get(key) == grant[key],
                 f"transport-{key}-mismatch")
    _require(_nonempty(receipt.get("target_thread_ref")), "transport-target-thread-required")
    submission = receipt.get("provider_submission")
    consumption = receipt.get("worker_consumption")
    _require(submission in {"ACCEPTED", "REJECTED", "UNKNOWN"},
             "transport-submission-invalid")
    _require(consumption in {"CONFIRMED", "UNCONFIRMED", "UNKNOWN"},
             "transport-consumption-invalid")
    _require(consumption != "CONFIRMED" or
             (submission == "ACCEPTED" and _nonempty(receipt.get("consumption_proof_ref"))),
             "transport-consumption-proof-required")
    return {
        "schema": SCHEMA, "authority": "NONE", "effect_authorized": False,
        "intent_ref": grant["intent_ref"], "attempt_ref": grant["attempt_ref"],
        "provider_submission": submission, "worker_consumption": consumption,
        "work_consumed_proven": consumption == "CONFIRMED",
        "live_transport_qualification": "UNPROVEN_BY_THIS_CLASSIFIER",
    }


def classify_terminal_candidate(terminal: Mapping[str, Any],
                                expected: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one exact terminal without admitting effect or releasing a lease."""
    _require(isinstance(terminal, Mapping) and isinstance(expected, Mapping),
             "terminal-and-expected-objects-required")
    for key in ("attempt_ref", "claim_ref", "packet_ref", "generation_ref"):
        _require(_nonempty(expected.get(key)), f"expected-{key}-required")
        _require(terminal.get(key) == expected[key], f"terminal-{key}-mismatch")
    _require(terminal.get("terminal_state") in {"PASS", "REFINE", "HOLD", "UNKNOWN"},
             "terminal-state-invalid")
    for key in ("delivery", "start", "admission", "effect"):
        _require(key in terminal, f"terminal-{key}-axis-required")
    uncertain = (terminal.get("provider_visible") is not True
                 or terminal.get("work_complete") is not True
                 or not _nonempty(terminal.get("provider_revision"))
                 or terminal.get("currentness_conflict") is True)
    ambiguous = (terminal["terminal_state"] == "UNKNOWN"
                 or terminal.get("semantic_novelty") is True
                 or terminal.get("human_gate_required") is True
                 or terminal.get("writer_collision") is True
                 or terminal.get("ambiguous_next") is True)
    deterministic = terminal.get("single_deterministic_transition") is True
    if uncertain:
        route, reason = "HOLD_EXACT", "TERMINAL_PROVIDER_PROOF_INCOMPLETE"
    elif ambiguous:
        route, reason = "PM_NEEDED", "AMBIGUOUS_TERMINAL"
    elif not deterministic:
        route, reason = "HOLD_EXACT", "DETERMINISTIC_TRANSITION_UNPROVEN"
    else:
        route, reason = "TERMINALSLUSE_LOCAL_DRAIN_CANDIDATE", "KNOWN_TERMINAL"
    result = {
        "schema": SCHEMA, "authority": "NONE", "effect_authorized": False,
        "terminal_state": terminal["terminal_state"],
        "route": route, "reason": reason,
        "release_performed": False, "admission_performed": False,
        "attempt_ref": terminal["attempt_ref"], "packet_ref": terminal["packet_ref"],
        "axes": {key: terminal[key] for key in ("delivery", "start", "admission", "effect")},
    }
    result["fingerprint"] = _fingerprint(result)
    return result


def optional_human_accelerator(intent: Mapping[str, Any], *, action: str,
                               expected_saved_machine_work: str,
                               continuation: str) -> dict[str, Any]:
    """Offer help without making Human a clock, courier, or required dependency."""
    _validate_intent(intent)
    _require(intent["resource_envelope"]["human_optional_acceleration"] == "ALLOWED",
             "human-accelerator-outside-envelope")
    for value in (action, expected_saved_machine_work, continuation):
        _require(_nonempty(value), "human-accelerator-field-required")
    return {
        "schema": SCHEMA, "authority": "NONE", "effect_authorized": False,
        "optional": True, "action": action,
        "expected_saved_machine_work": expected_saved_machine_work,
        "continuation_if_ignored": continuation,
        "human_heartbeat_required": False,
    }
