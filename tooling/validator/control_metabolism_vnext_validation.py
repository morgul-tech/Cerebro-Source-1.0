#!/usr/bin/env python3
"""Cold C0-C12 contract canaries for the non-authoritative vNext candidate."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("control_metabolism_vnext", ROOT / "mcp/control_metabolism_vnext.py")
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def intent() -> dict:
    return {
        "intent_id": "V-NEXT-CANARY", "human_goal": "one bounded ping",
        "requested_effect": "one ping", "success_criteria": "one exact reply",
        "scope": "synthetic fixture", "explicit_non_goals": ["VENDOR_ESCALATION"],
        "allowed_work_classes": ["READ_ONLY", "PING"],
        "hard_now_qualifications": ["EXACT_ATTEMPT"],
        "deferred_qualifications": [{"qualification": "STRONG_READER_PROVENANCE",
                                     "activation_trigger": "READER_AUTHORIZES_EFFECT",
                                     "owner": "READER_OWNER", "proof_duty": "same-call receipt"}],
        "resource_envelope": {"resource_posture": "SPAREBLUSS",
                              "pre_effect_cognition_budget_class": "SMALL",
                              "provider_refresh_budget_class": "BOUNDED",
                              "human_optional_acceleration": "ALLOWED",
                              "parallelism_cap_or_ELASTIC": "ELASTIC",
                              "stagnation_limit": 1, "reframe_owner": "X9"},
        "effect_distance_state": "OWNER_CLEAR",
        "scope_expansion_authority": "HUMAN", "material_effect_boundary": "PING_SEND",
        "human_gate_boundary": "EXPLICIT_SCOPE_EXPANSION", "project_ref_or_none": "PROJECT-1",
        "way_home": "PM8851",
    }


def lease() -> dict:
    return {"lease_ref": "LEASE-1", "intent_ref": "V-NEXT-CANARY",
            "scope": "synthetic fixture", "allowed_work_classes": ["READ_ONLY", "PING"],
            "material_effect_boundary": "PING_SEND", "way_home": "PM8851",
            "owner_set_or_resolution_rule": "X9 owner-clear", "resource_posture": "SPAREBLUSS",
            "PM_escalation_conditions": ["CROSS_SCOPE_CONFLICT"],
            "Human_gate_conditions": ["SCOPE_EXPANSION"],
            "expiry_or_revalidation": "before material effect"}


def facts() -> dict:
    return {"intent_ref": "V-NEXT-CANARY", "work_requested": True,
            "work_class": "PING", "lease_current": True,
            "proven_qualifications": ["EXACT_ATTEMPT"],
            "attempt_ref": "PING-ATTEMPT-1",
            "target_capability": "PING_WORKER", "target_available": True}


def run() -> dict:
    checks = {}

    def check(name, predicate):
        checks[name] = bool(predicate)

    i, l, f = intent(), lease(), facts()
    def route(**updates):
        x = copy.deepcopy(f)
        x.update(updates)
        return mod.compile_candidate(i, l, x)

    check("C0_HEALTHY_QUIET", route(work_requested=False)["route"] == "QUIET")
    c1 = route(work_class="READ_ONLY")
    check("C1_PROJECT_LEASE_LOCAL", c1["route"] == "PM_NOT_NEEDED_DISPATCH_GRANT")
    c2 = route()
    check("C2_SIMPLE_DISPATCH_CONTRACT", c2["dispatch_grant_candidate"]["send_once_required"]
          and c2["effect_authorized"] is False)
    independent = [route(target_capability=f"LENS_{n}") for n in range(3)]
    check("C3_PARALLEL_COGNITION_SHARED_FENCE", all(x["route"] == "PM_NOT_NEEDED_DISPATCH_GRANT"
          for x in independent) and route(shared_effect_collision=True)["route"] == "PM_NEEDED")
    check("C4_REAL_CONFLICT", route(active_claim_collision=True)["reason"] == "CROSS_SCOPE_CONFLICT")
    check("C5_TRUE_HUMAN_GATE", route(human_gate_required=True)["route"] == "HUMAN_GATE")
    check("C6_SCOPE_EXPANSION_TRAP", route(work_class="VENDOR_ESCALATION")["reason"]
          == "SCOPE_EXPANSION_REQUIRES_EXPLICIT_GATE")
    check("C7_RESOURCE_STAGNATION", route(material_resource_consumed=True,
          effect_distance_advanced=False, stagnation_count=1)["reason"] == "STOP_AND_REFRAME")
    check("C8_SIGNALVEV_BASELINE_CONTRACT", c2["route"] == "PM_NOT_NEEDED_DISPATCH_GRANT"
          and c2["reason"] == "OWNER_CLEAR_WITHIN_LEASE")
    grant = c2["dispatch_grant_candidate"]
    transport = mod.classify_transport_receipt_candidate(grant, {
        "intent_ref": grant["intent_ref"], "attempt_ref": grant["attempt_ref"],
        "target_thread_ref": "EXISTING-THREAD", "provider_submission": "ACCEPTED",
        "worker_consumption": "UNCONFIRMED"})
    check("C9_DIRIGENT_USE_CONTRACT", grant["transport_owner"] == "DIRIGENT"
          and grant["host_currentness_required"] and transport["work_consumed_proven"] is False
          and transport["provider_submission"] == "ACCEPTED")
    terminal = {"attempt_ref": "A", "claim_ref": "C", "packet_ref": "P",
                "generation_ref": "G", "terminal_state": "PASS",
                "delivery": "DELIVERED", "start": "STARTED", "admission": "NOT_REQUIRED", "effect": "NONE",
                "provider_visible": True, "provider_revision": "R1", "work_complete": True,
                "single_deterministic_transition": True}
    expected = {key: terminal[key] for key in ("attempt_ref", "claim_ref", "packet_ref", "generation_ref")}
    drain = mod.classify_terminal_candidate(terminal, expected)
    check("C10_TERMINALSLUSE_DRAIN_CONTRACT", drain["route"] == "TERMINALSLUSE_LOCAL_DRAIN_CANDIDATE"
          and drain["release_performed"] is False and drain["admission_performed"] is False)
    check("C10_MISSING_PROVIDER_PROOF_HOLDS", mod.classify_terminal_candidate(
          {**terminal, "provider_visible": False}, expected)["route"] == "HOLD_EXACT")
    offer = mod.optional_human_accelerator(i, action="one optional click",
                                           expected_saved_machine_work="three provider turns",
                                           continuation="continue machine route")
    check("C11_HUMAN_OPTIONAL_ACCELERATOR", offer["optional"] and not offer["human_heartbeat_required"])
    check("C12_DRIVE_NAV_DEFERRED_QUALIFICATION",
          route(work_class="READ_ONLY")["route"] == "PM_NOT_NEEDED_DISPATCH_GRANT"
          and route(work_class="READ_ONLY", active_qualification_triggers=["READER_AUTHORIZES_EFFECT"])
          ["reason"] == "HARD_NOW_UNPROVEN:STRONG_READER_PROVENANCE")
    bad = copy.deepcopy(terminal)
    bad["generation_ref"] = "WRONG"
    try:
        mod.classify_terminal_candidate(bad, expected)
    except mod.ContractError:
        mismatch_rejected = True
    else:
        mismatch_rejected = False
    check("EXACT_TERMINAL_IDENTITY_REJECTS_MISMATCH", mismatch_rejected)
    return {"result": "PASS" if all(checks.values()) else "FAIL",
            "passed": sum(checks.values()), "total": len(checks),
            "checks": checks, "authority": "NONE", "live_transport_proven": False,
            "live_terminal_release_proven": False}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["result"] == "PASS" else 1)
