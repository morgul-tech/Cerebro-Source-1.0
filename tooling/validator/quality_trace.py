#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


STAGES = [
    "UNDERSTAND_FRAME",
    "EXPLORE_RESEARCH",
    "REFINE",
    "CRITIQUE",
    "COMPARE_CONVERGE",
    "DECIDE",
    "EXECUTE_GENERATE",
    "VERIFY",
    "LEARN",
]
REQ = {
    "LIGHT": ["UNDERSTAND_FRAME", "EXECUTE_GENERATE", "VERIFY"],
    "STANDARD": [
        "UNDERSTAND_FRAME",
        "EXPLORE_RESEARCH",
        "REFINE",
        "CRITIQUE",
        "COMPARE_CONVERGE",
        "EXECUTE_GENERATE",
        "VERIFY",
    ],
    "DEEP": STAGES,
}

BLIND_CLAIM_BLOCKER = (
    "required_observation_closure_evidence_absent_or_nonpass_for_"
    "blindness_dependent_claim"
)
OBSERVATION_CLOSURE_PROBE = "OBSERVATION_CLOSURE"
METHOD_FLOOR = (
    "PREEXPOSURE_CONFIG_SEAL",
    "INDEPENDENT_NEGATIVE_CANARIES",
    "CONTENT_EXPOSURE_GATE",
    "POSTRUN_CONFORMANCE_READBACK",
)


def fp(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def new(work_item: str, depth: str, basis: str) -> dict[str, Any]:
    return {
        "schema": "cerebro-quality-trace/v0.2",
        "work_item_ref": work_item,
        "required_depth": depth,
        "basis_fingerprint": basis,
        "stages": {
            stage: {
                "state": "PENDING",
                "basis_fingerprint": basis,
                "evidence_refs": [],
            }
            for stage in STAGES
        },
        "overall_assurance": "IN_PROGRESS",
    }


def pass_stage(
    trace: dict[str, Any], stage: str, basis: str, evidence: Sequence[str]
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError("UNKNOWN_STAGE")
    if basis != trace["basis_fingerprint"]:
        raise ValueError("STALE_BASIS")
    if not evidence:
        raise ValueError("PASS_REQUIRES_EVIDENCE")
    trace["stages"][stage] = {
        "state": "PASS",
        "basis_fingerprint": basis,
        "evidence_refs": sorted(set(evidence)),
    }
    required = REQ[trace["required_depth"]]
    trace["overall_assurance"] = (
        "PASS"
        if all(trace["stages"][item]["state"] == "PASS" for item in required)
        else "IN_PROGRESS"
    )
    return trace


def rebase(trace: dict[str, Any], new_basis: str) -> dict[str, Any]:
    if new_basis == trace["basis_fingerprint"]:
        return trace
    trace["basis_fingerprint"] = new_basis
    for stage in STAGES:
        if trace["stages"][stage]["state"] == "PASS":
            trace["stages"][stage]["state"] = "STALE"
        trace["stages"][stage]["basis_fingerprint"] = new_basis
    trace["overall_assurance"] = "STALE"
    return trace


def _as_refs(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {str(item) for item in value if str(item)}
    return {str(value)}


def _blindness_dependent(context: Mapping[str, Any]) -> bool:
    if context.get("blindness_dependent") is True:
        return True
    if context.get("strong_blind_claim") is True:
        return True
    claim_type = str(context.get("claim_type", "")).upper()
    if claim_type in {"BLIND", "STRONG_BLIND", "BLINDNESS_DEPENDENT"}:
        return True
    dependencies = {
        item.upper().replace("-", "_")
        for item in _as_refs(context.get("causal_dependencies"))
    }
    return bool(dependencies & {"BLINDNESS", "ISOLATION", "BLIND_ISOLATION"})


def _assessment(
    context: Mapping[str, Any],
    admissibility: str,
    reason_code: str,
    evidence: Mapping[str, Any] | None = None,
    *,
    blocker: str | None = BLIND_CLAIM_BLOCKER,
    fresh_generation_required: bool = False,
) -> dict[str, Any]:
    decision_state = (
        "PASS"
        if admissibility == "VALID_FOR_STRONG_BLIND_CLAIM"
        else "NOT_APPLICABLE"
        if admissibility == "NOT_APPLICABLE"
        else "HOLD"
    )
    strong_claim_validity = (
        "VALID_FOR_STRONG_BLIND_CLAIM"
        if admissibility == "VALID_FOR_STRONG_BLIND_CLAIM"
        else "NOT_APPLICABLE"
        if admissibility == "NOT_APPLICABLE"
        else "INVALID_FOR_STRONG_BLIND_CLAIM"
    )
    return {
        "schema": "cerebro-blind-claim-admissibility/v1",
        "applicability": (
            "NOT_APPLICABLE"
            if admissibility == "NOT_APPLICABLE"
            else "APPLICABLE"
        ),
        "admissibility": admissibility,
        "decision_state": decision_state,
        "strong_blind_claim_validity": strong_claim_validity,
        "blocker": blocker,
        "evidence_ref": None if evidence is None else evidence.get("evidence_id"),
        "basis_fingerprint": context.get("basis_fingerprint"),
        "reason_code": reason_code,
        "preserve_historical_observations": True,
        "narrower_nonblind_use_permitted": True,
        "fresh_arm_or_generation_required": fresh_generation_required,
    }


def _first_evidence(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping):
                return item
    return None


def assess_blind_claim_admissibility(
    claim_context: Mapping[str, Any], observation_closure_evidence: Any
) -> dict[str, Any]:
    """Return a deterministic evidence view; never runtime activation proof."""
    if not isinstance(claim_context, Mapping):
        raise TypeError("claim_context must be a mapping")

    if not _blindness_dependent(claim_context):
        return _assessment(
            claim_context,
            "NOT_APPLICABLE",
            "CLAIM_DOES_NOT_CAUSALLY_DEPEND_ON_BLINDNESS_OR_ISOLATION",
            blocker=None,
        )

    evidence = _first_evidence(observation_closure_evidence)
    contaminated = (
        claim_context.get("preseal_material_exposure") is True
        or str(claim_context.get("contamination_state", "")).upper()
        == "CONTAMINATED"
    )
    if contaminated:
        return _assessment(
            claim_context,
            "CONTAMINATED",
            "PRESEAL_MATERIAL_EXPOSURE",
            evidence,
            fresh_generation_required=True,
        )

    if claim_context.get("airlock_infrastructure_available") is not True:
        return _assessment(
            claim_context,
            "HOLD",
            "AIRLOCK_INFRASTRUCTURE_UNAVAILABLE_POLICY_REMAINS_APPLICABLE",
            evidence,
        )

    if evidence is None:
        return _assessment(
            claim_context,
            "HOLD",
            "OBSERVATION_CLOSURE_EVIDENCE_MISSING",
        )

    if evidence.get("evidence_kind") != "VERIFICATION_RESULT":
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_EVIDENCE_KIND_INVALID",
            evidence,
        )
    if evidence.get("probe_ref") != OBSERVATION_CLOSURE_PROBE:
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_PROBE_REF_INVALID",
            evidence,
        )

    subject = evidence.get("subject_ref")
    if not isinstance(subject, Mapping) or any(
        subject.get(field) != claim_context.get(field)
        or claim_context.get(field) in (None, "")
        for field in ("claim_ref", "arm_ref", "generation_ref")
    ):
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_SUBJECT_MISMATCH",
            evidence,
        )

    if str(evidence.get("status", "")).upper() != "COMPLETE" or str(
        evidence.get("result", "")
    ).upper() != "PASS":
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_NONPASS",
            evidence,
        )

    freshness = evidence.get("freshness")
    if not isinstance(freshness, Mapping):
        return _assessment(
            claim_context,
            "HOLD",
            "OBSERVATION_CLOSURE_FRESHNESS_MISSING",
            evidence,
        )
    if (
        freshness.get("freshness_kind") != "STATE_BOUND"
        or freshness.get("current") is False
        or str(freshness.get("state", "CURRENT")).upper() == "STALE"
    ):
        return _assessment(
            claim_context,
            "HOLD",
            "OBSERVATION_CLOSURE_STALE",
            evidence,
        )
    if (
        not claim_context.get("basis_fingerprint")
        or freshness.get("basis_fingerprint") != claim_context.get("basis_fingerprint")
    ):
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_BASIS_MISMATCH",
            evidence,
        )

    value = evidence.get("value")
    if not isinstance(value, Mapping) or any(
        value.get(method) != "PASS" for method in METHOD_FLOOR
    ):
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_METHOD_FLOOR_NONPASS",
            evidence,
        )

    producer = str(evidence.get("producer_ref", ""))
    disallowed_producers = set()
    for field in (
        "evaluated_actor_ref",
        "evaluated_actor_refs",
        "builder_ref",
        "builder_refs",
        "self_certifier_ref",
        "self_certifier_refs",
        "disallowed_producer_refs",
    ):
        disallowed_producers.update(_as_refs(claim_context.get(field)))
    if (
        not producer
        or producer in disallowed_producers
        or evidence.get("self_attested") is True
        or evidence.get("producer_independence_verified") is not True
    ):
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_PRODUCER_NOT_INDEPENDENT",
            evidence,
        )

    basis_refs = "\n".join(sorted(_as_refs(evidence.get("basis_refs"))))
    if any(method not in basis_refs for method in METHOD_FLOOR):
        return _assessment(
            claim_context,
            "INVALID_FOR_STRONG_BLIND_CLAIM",
            "OBSERVATION_CLOSURE_BASIS_REFS_INCOMPLETE",
            evidence,
        )

    return _assessment(
        claim_context,
        "VALID_FOR_STRONG_BLIND_CLAIM",
        "CURRENT_EXACT_INDEPENDENT_OBSERVATION_CLOSURE_PASS",
        evidence,
        blocker=None,
    )


def _blind_claim_canaries() -> list[dict[str, str]]:
    tests: list[dict[str, str]] = []

    def canary(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    basis = fp({"claim": "C1", "arm": "A1", "generation": "G1"})
    context: dict[str, Any] = {
        "claim_ref": "C1",
        "arm_ref": "A1",
        "generation_ref": "G1",
        "basis_fingerprint": basis,
        "blindness_dependent": True,
        "evaluated_actor_ref": "ACTOR-1",
        "builder_ref": "BUILDER-1",
        "airlock_infrastructure_available": True,
    }
    evidence: dict[str, Any] = {
        "evidence_id": "E-CLOSURE-1",
        "evidence_kind": "VERIFICATION_RESULT",
        "subject_ref": {
            "claim_ref": "C1",
            "arm_ref": "A1",
            "generation_ref": "G1",
        },
        "probe_ref": OBSERVATION_CLOSURE_PROBE,
        "status": "COMPLETE",
        "result": "PASS",
        "freshness": {
            "freshness_kind": "STATE_BOUND",
            "basis_fingerprint": basis,
            "current": True,
        },
        "value": {method: "PASS" for method in METHOD_FLOOR},
        "producer_ref": "INDEPENDENT-VERIFIER-1",
        "producer_independence_verified": True,
        "basis_refs": [f"{method}:REF" for method in METHOD_FLOOR],
    }

    result = assess_blind_claim_admissibility(
        {"claim_ref": "C-NONBLIND", "causal_dependencies": ["ACCURACY"]}, None
    )
    canary(
        "nonblind-claim-without-observation-closure-is-not-applicable",
        result["admissibility"] == "NOT_APPLICABLE" and result["blocker"] is None,
    )

    result = assess_blind_claim_admissibility(context, evidence)
    canary(
        "current-exact-independent-observation-closure-admits-strong-blind-claim",
        result["admissibility"] == "VALID_FOR_STRONG_BLIND_CLAIM"
        and result["blocker"] is None,
    )

    result = assess_blind_claim_admissibility(context, None)
    canary(
        "missing-observation-closure-holds-strong-blind-claim",
        result["admissibility"] == "HOLD"
        and result["blocker"] == BLIND_CLAIM_BLOCKER,
    )

    stale = copy.deepcopy(evidence)
    stale["freshness"]["current"] = False
    result = assess_blind_claim_admissibility(context, stale)
    canary(
        "stale-observation-closure-holds-strong-blind-claim",
        result["admissibility"] == "HOLD",
    )

    nonpass_cases = []
    for status in ("PARTIAL", "UNAVAILABLE", "ERROR"):
        candidate = copy.deepcopy(evidence)
        candidate["status"] = status
        nonpass_cases.append(candidate)
    for nonpass_result in ("UNKNOWN", "FAIL"):
        candidate = copy.deepcopy(evidence)
        candidate["result"] = nonpass_result
        nonpass_cases.append(candidate)
    canary(
        "partial-unavailable-error-unknown-or-fail-observation-closure-denies-admissibility",
        all(
            assess_blind_claim_admissibility(context, item)["admissibility"]
            == "INVALID_FOR_STRONG_BLIND_CLAIM"
            for item in nonpass_cases
        ),
    )

    wrong_generation = copy.deepcopy(evidence)
    wrong_generation["subject_ref"]["generation_ref"] = "G2"
    result = assess_blind_claim_admissibility(context, wrong_generation)
    canary(
        "wrong-arm-or-generation-denies-admissibility",
        result["admissibility"] == "INVALID_FOR_STRONG_BLIND_CLAIM",
    )

    drifted = copy.deepcopy(evidence)
    drifted["freshness"]["basis_fingerprint"] = fp({"different": True})
    result = assess_blind_claim_admissibility(context, drifted)
    canary(
        "observation-closure-basis-drift-denies-admissibility",
        result["admissibility"] == "INVALID_FOR_STRONG_BLIND_CLAIM",
    )

    self_attested = copy.deepcopy(evidence)
    self_attested["producer_ref"] = "ACTOR-1"
    self_attested["self_attested"] = True
    result = assess_blind_claim_admissibility(context, self_attested)
    canary(
        "self-attested-observation-closure-denies-admissibility",
        result["admissibility"] == "INVALID_FOR_STRONG_BLIND_CLAIM",
    )

    exposed_context = {**context, "preseal_material_exposure": True}
    result = assess_blind_claim_admissibility(exposed_context, evidence)
    canary(
        "preseal-material-exposure-contaminates-current-arm-or-generation",
        result["admissibility"] == "CONTAMINATED"
        and result["fresh_arm_or_generation_required"] is True,
    )

    contaminated_context = {**context, "contamination_state": "CONTAMINATED"}
    repaired_evidence = copy.deepcopy(evidence)
    repaired_evidence["evidence_id"] = "E-CLOSURE-REPAIR"
    result = assess_blind_claim_admissibility(contaminated_context, repaired_evidence)
    canary(
        "contamination-cannot-be-repaired-in-place",
        result["admissibility"] == "CONTAMINATED"
        and result["fresh_arm_or_generation_required"] is True,
    )

    result = assess_blind_claim_admissibility({**context, "legacy_claim": True}, None)
    canary(
        "legacy-strong-blind-claim-without-closure-downgrades-while-preserving-observations",
        result["admissibility"] == "HOLD"
        and result["preserve_historical_observations"] is True
        and result["narrower_nonblind_use_permitted"] is True,
    )

    repeated_self_observation = copy.deepcopy(evidence)
    repeated_self_observation["producer_ref"] = "ACTOR-1"
    repeated_self_observation["producer_independence_verified"] = False
    repeated = [repeated_self_observation, copy.deepcopy(repeated_self_observation)]
    result = assess_blind_claim_admissibility(context, repeated)
    canary(
        "repeated-identical-observation-is-not-independent-confirmation",
        result["admissibility"] == "INVALID_FOR_STRONG_BLIND_CLAIM",
    )

    result = assess_blind_claim_admissibility(
        {**context, "airlock_infrastructure_available": False}, evidence
    )
    canary(
        "missing-airlock-infrastructure-blocks-pass-not-policy",
        result["applicability"] == "APPLICABLE"
        and result["admissibility"] == "HOLD",
    )

    result = assess_blind_claim_admissibility(
        {**context, "quality_gate_pass": True}, None
    )
    canary(
        "quality-gate-pass-cannot-override-observation-closure-blocker",
        result["admissibility"] == "HOLD"
        and result["blocker"] == BLIND_CLAIM_BLOCKER,
    )

    result = assess_blind_claim_admissibility(
        {
            "claim_ref": "LEGACY-NONBLIND",
            "legacy_claim": True,
            "quality_gate_pass": True,
            "causal_dependencies": ["ACCURACY"],
        },
        None,
    )
    canary(
        "legacy-nonblind-claim-remains-unaffected",
        result["admissibility"] == "NOT_APPLICABLE",
    )

    return tests


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def canary(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    basis = fp({"x": 1})
    trace = new("X", "DEEP", basis)
    try:
        pass_stage(trace, "REFINE", basis, [])
        no_evidence_rejected = False
    except ValueError:
        no_evidence_rejected = True
    canary("pass-without-evidence-rejected", no_evidence_rejected)
    pass_stage(trace, "REFINE", basis, ["E1"])
    canary(
        "evidence-bound-pass-accepted", trace["stages"]["REFINE"]["state"] == "PASS"
    )
    rebase(trace, fp({"x": 2}))
    canary(
        "basis-change-invalidates-pass",
        trace["stages"]["REFINE"]["state"] == "STALE"
        and trace["overall_assurance"] == "STALE",
    )

    blind_tests = _blind_claim_canaries()
    tests.extend(blind_tests)
    return {
        "schema": "cerebro-quality-trace-selftest/v0.2",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "quality_trace_canary_count": 3,
        "blind_claim_canary_count": len(blind_tests),
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["selftest"])
    args = parser.parse_args()
    if args.command == "selftest":
        output = selftest()
        print(json.dumps(output, indent=2))
        return 0 if output["result"] == "PASS" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
