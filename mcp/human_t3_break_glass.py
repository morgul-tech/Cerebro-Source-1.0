"""MCP-owned HG04 two-event Human T3 break-glass semantics.

All Human-event/currentness facts and the pre-Human effect capability are
constructor-bound. Custody is not authorization; ARM cannot emit or grant tool,
file or API authority. A consumed override is never replayed after a crash.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

SCHEMA = "cerebro-human-t3-break-glass/v1"
CANDIDATE_FIELDS = (
    "candidate_id", "candidate_digest", "generation_id", "target_binding",
    "material_fingerprint", "supersession_ref", "project_revision",
    "session_revision", "frontier_revision", "normal_admission", "override_dimensions",
)
IDENTITY_FIELDS = ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref")
OVERRIDEABLE = {"AUTHORITY", "FENCE", "CURRENTNESS"}
EFFECT_TRUTHS = {"EFFECT_SUCCESS", "EFFECT_NO_EFFECT", "EFFECT_UNKNOWN"}


class HumanT3BreakGlassError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HumanT3BreakGlassError(message)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def seal_record(record: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(record)
    result.pop("fingerprint", None)
    result["fingerprint"] = fingerprint(result)
    return result


def validate_candidate(candidate: dict[str, Any]) -> None:
    require(isinstance(candidate, dict) and set(candidate) == set(CANDIDATE_FIELDS), "exact-T3-candidate-fields-required")
    for field in CANDIDATE_FIELDS[:-1]:
        require(isinstance(candidate[field], str) and bool(candidate[field].strip()), "candidate-field-required:" + field)
    for field in ("candidate_digest", "material_fingerprint"):
        require(len(candidate[field]) == 64 and all(c in "0123456789abcdef" for c in candidate[field]), "candidate-digest-invalid:" + field)
    require(candidate["normal_admission"] in {"BLOCK", "UNKNOWN"}, "break-glass-requires-normal-block-or-unknown")
    dims = candidate["override_dimensions"]
    require(isinstance(dims, list) and bool(dims) and all(isinstance(d, str) for d in dims) and len(dims) == len(set(dims)) and set(dims) <= OVERRIDEABLE,
            "nonoverrideable-dimension-rejected")


def resolve_arm(candidate: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Pure MCP admission over trusted host facts; never a runtime executor."""
    validate_candidate(candidate)
    require(fresh.get("event_kind") == "HUMAN_ARM", "exact-Human-ARM-event-required")
    require(fresh.get("constitutional_floor_pass") is True, "MCP-constitutional-floor-not-proven")
    require(fresh.get("candidate") == candidate, "ARM-candidate-currentness-mismatch")
    for field in ("human_ref", "event_id", "turn_id"):
        require(isinstance(fresh.get(field), str) and bool(fresh[field]), "trusted-Human-event-field-required:" + field)
    return {"decision": "ARM_ADMITTED", "current": False, "authorizing": False}


def resolve_confirm(record: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    require(fresh.get("event_kind") == "HUMAN_CONFIRM", "exact-Human-CONFIRM-event-required")
    require(fresh.get("human_ref") == record["human_ref"], "different-Human-confirm-rejected")
    require(isinstance(fresh.get("event_id"), str) and bool(fresh["event_id"]), "trusted-confirm-event-required")
    require(isinstance(fresh.get("turn_id"), str) and bool(fresh["turn_id"]), "trusted-confirm-turn-required")
    require(fresh["event_id"] != record["arm_event_id"] and fresh["turn_id"] != record["arm_turn_id"], "separate-Human-event-and-turn-required")
    if fresh.get("constitutional_floor_pass") is not True:
        return {"decision": "INVALIDATE", "reason": "MCP_CONSTITUTIONAL_FLOOR_NOT_PROVEN"}
    if fresh.get("candidate") != record["candidate"]:
        return {"decision": "INVALIDATE", "reason": "CANDIDATE_OR_MATERIAL_OR_SUPERSESSION_CHANGED"}
    return {"decision": "CONFIRM_ADMITTED"}


class HumanT3BreakGlassHost:
    """Trusted host binding to MCP admission, transactional custody and one T3 attempt.

    ``read_current`` must derive authenticated Human event/turn, fresh material
    truth and ``constitutional_request.constitutional_breach_candidates``
    independently of arguments. MCP's canonical constitutional consumer evaluates
    those facts in addition to the explicit nonoverrideable floor proof.
    ``effect_capability`` must be bound by the
    pre-Human T3 carrier; a tool-call argument can never supply one. Its attempt
    method must enforce the exact candidate against its provider-side material
    fence, emit no general tool/file/API effect, and report actual effect truth.
    """

    def __init__(self, *, state_port: Any, current_reader: Any, effect_capability: Any | None = None):
        for method in ("read_human_t3_arm", "commit_human_t3_arm"):
            require(callable(getattr(state_port, method, None)), "T3-state-port-method-required:" + method)
        require(callable(getattr(current_reader, "read_current", None)), "trusted-T3-current-reader-required")
        if effect_capability is not None:
            require(getattr(effect_capability, "effect_kind", None) == "HUMAN_FACING_T3", "non-T3-effect-capability-rejected")
            for method in ("is_available", "attempt"):
                require(callable(getattr(effect_capability, method, None)), "T3-effect-capability-method-required:" + method)
        self._state = state_port
        self._reader = current_reader
        self._effect = effect_capability

    @staticmethod
    def _identity(identity: dict[str, Any], scopes: set[str]) -> dict[str, str]:
        require(set(identity) == set(IDENTITY_FIELDS), "exact-host-identity-required")
        require(all(isinstance(v, str) and bool(v.strip()) for v in identity.values()), "host-identity-invalid")
        require("project_state:transition" in scopes, "required-scope-missing:project_state:transition")
        return dict(identity)

    def _fresh(self, identity: dict[str, Any], operation: str, override_id: str) -> dict[str, Any]:
        fresh = self._reader.read_current(identity=copy.deepcopy(identity), operation=operation, override_id=override_id)
        require(isinstance(fresh, dict), "trusted-T3-currentness-unavailable")
        return copy.deepcopy(fresh)

    def _read(self, identity: dict[str, Any], override_id: str, scopes: set[str]) -> dict[str, Any] | None:
        return self._state.read_human_t3_arm(**identity, override_id=override_id, scopes=scopes)

    def _persist(self, record: dict[str, Any], expected: int, scopes: set[str]) -> dict[str, Any]:
        record = seal_record(record)
        receipt = self._state.commit_human_t3_arm(record=record, expected_revision=expected, scopes=scopes)
        observed = self._read(record["identity"], record["override_id"], scopes)
        require(observed == record, "T3-custody-independent-readback-mismatch")
        require(receipt.get("record_fingerprint") == record["fingerprint"] and receipt.get("revision") == record["revision"], "T3-custody-receipt-binding-mismatch")
        return {"record": record, "receipt": receipt, "readback_verified": True}

    def arm(self, *, identity: dict[str, Any], override_id: str, candidate: dict[str, Any], scopes: set[str]) -> dict[str, Any]:
        identity = self._identity(identity, scopes)
        require(isinstance(override_id, str) and bool(override_id.strip()), "override-id-required")
        fresh = self._fresh(identity, "ARM", override_id)
        from control_resolution import resolve_human_t3_arm
        resolve_human_t3_arm(candidate, fresh)
        prior = self._read(identity, override_id, scopes)
        if prior is not None:
            require(prior["candidate"] == candidate and prior["human_ref"] == fresh["human_ref"] and prior["arm_event_id"] == fresh["event_id"] and prior["arm_turn_id"] == fresh["turn_id"], "override-id-reuse-conflict")
            return {"result": "ARM_ALREADY_RECORDED", "record": prior, "effect_attempted": False}
        record = {
            "schema": SCHEMA, "identity": identity, "override_id": override_id,
            "candidate": copy.deepcopy(candidate), "human_ref": fresh["human_ref"],
            "arm_event_id": fresh["event_id"], "arm_turn_id": fresh["turn_id"],
            "revision": 1, "state": "ARMED_PENDING_CONFIRM", "authority": "NONAUTHORIZING_CUSTODY",
            "current": False, "override_consumed": False, "confirm_event_id": None,
            "effect_attempt_id": None, "effect_truth": "NOT_ATTEMPTED",
            "context_override_record": None, "invalidation_reason": None,
        }
        return {"result": "ARMED_PENDING_CONFIRM", **self._persist(record, 0, scopes), "effect_attempted": False}

    def confirm(self, *, identity: dict[str, Any], override_id: str, expected_revision: int, scopes: set[str]) -> dict[str, Any]:
        identity = self._identity(identity, scopes)
        require(type(expected_revision) is int and expected_revision >= 1, "exact-positive-expected-revision-required")
        prior = self._read(identity, override_id, scopes)
        require(prior is not None, "ARM-not-found")
        require(prior["revision"] == expected_revision, "ARM-revision-currentness-mismatch")
        if prior["state"] != "ARMED_PENDING_CONFIRM":
            return {"result": "HOLD_TERMINAL_OR_CONSUMED_NO_RETRY", "record": prior, "effect_attempted": False}
        fresh = self._fresh(identity, "CONFIRM", override_id)
        from control_resolution import resolve_human_t3_confirm
        decision = resolve_human_t3_confirm(prior, fresh)
        record = copy.deepcopy(prior)
        record["revision"] += 1
        if decision["decision"] == "INVALIDATE":
            record.update(state="INVALIDATED", invalidation_reason=decision["reason"])
            return {"result": "INVALIDATED", **self._persist(record, expected_revision, scopes), "effect_attempted": False}
        if self._effect is None or self._effect.is_available(candidate=copy.deepcopy(prior["candidate"]), fresh_state=copy.deepcopy(fresh)) is not True:
            return {"result": "HOLD_PREHUMAN_T3_CAPABILITY_NOT_PROVEN", "record": prior, "effect_attempted": False}
        attempt_id = "T3-" + fingerprint([identity, override_id, prior["fingerprint"], fresh["event_id"]])[:32]
        record.update(state="OVERRIDE_CONSUMED", override_consumed=True,
                      confirm_event_id=fresh["event_id"], effect_attempt_id=attempt_id, effect_truth="EFFECT_UNKNOWN")
        consumed = self._persist(record, expected_revision, scopes)
        record = copy.deepcopy(consumed["record"])
        record["revision"] += 1
        # Downstream Context evidence records MCP admission; it never authorizes.
        record.update(state="CONTEXT_OVERRIDE_COMMITTED", context_override_record={
            "record_type": "OVERRIDE", "authority": "EVIDENCE_ONLY", "mcp_decision": "CONFIRM_ADMITTED",
            "override_id": override_id, "effect_attempt_id": attempt_id,
            "candidate_fingerprint": fingerprint(prior["candidate"]),
            "state_service_receipt": copy.deepcopy(consumed["receipt"]), "readback_verified": True,
        })
        committed = self._persist(record, record["revision"] - 1, scopes)
        try:
            preattempt = self._fresh(identity, "CONFIRM", override_id)
            require(preattempt == fresh and resolve_human_t3_confirm(prior, preattempt)["decision"] == "CONFIRM_ADMITTED",
                    "T3-preattempt-currentness-changed")
            require(self._effect.is_available(candidate=copy.deepcopy(prior["candidate"]), fresh_state=copy.deepcopy(preattempt)) is True,
                    "T3-preattempt-capability-unavailable")
        except Exception:
            return {"result": "HOLD_CONSUMED_PREATTEMPT_REQUAL_FAILED", **committed,
                    "effect_attempted": False, "retry_allowed": False}
        # There is exactly one attempt site, after consumption + downstream readback.
        try:
            outcome = self._effect.attempt(candidate=copy.deepcopy(prior["candidate"]), effect_attempt_id=attempt_id)
            truth = outcome.get("effect_truth") if isinstance(outcome, dict) else None
            truth = truth if truth in EFFECT_TRUTHS else "EFFECT_UNKNOWN"
        except Exception:
            truth = "EFFECT_UNKNOWN"
        record = copy.deepcopy(committed["record"])
        record["revision"] += 1
        record.update(state=truth, effect_truth=truth)
        try:
            final = self._persist(record, record["revision"] - 1, scopes)
        except Exception:
            # A failed/unknown post-attempt commit never licenses another attempt.
            return {"result": "EFFECT_UNKNOWN_RECONCILE_REQUIRED", "effect_attempt_id": attempt_id,
                    "effect_truth": "EFFECT_UNKNOWN", "effect_attempted": True, "retry_allowed": False}
        return {"result": truth, **final, "effect_attempted": True, "retry_allowed": False}
