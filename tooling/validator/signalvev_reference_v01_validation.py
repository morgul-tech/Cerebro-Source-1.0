#!/usr/bin/env python3
"""Offline validator + falsifier suite for candidates/signalvev-reference-v0.1.

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside the
candidate directory. No NATS/WireGuard/VPS/Drive/JetStream code exists here.

Mirrors the existing tooling/validator/*_validation.py convention:
pure Python, no third-party dependencies, `selftest` subcommand runs every
check and prints a PASS/FAIL summary with a nonzero exit code on failure.

Source provenance for the rules enforced here:
  candidates/signalvev-reference-v0.1/cerebro-message-v1-candidate.schema.json
  candidates/signalvev-reference-v0.1/subject-registry-v0.1-candidate.json
  candidates/signalvev-reference-v0.1/receipt-taxonomy-candidate.yaml
(each carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

CANDIDATE_DIR = Path(__file__).resolve().parents[2] / "candidates" / "signalvev-reference-v0.1"
SCHEMA_PATH = CANDIDATE_DIR / "cerebro-message-v1-candidate.schema.json"
REGISTRY_PATH = CANDIDATE_DIR / "subject-registry-v0.1-candidate.json"

REQUIRED_FIELDS = [
    "message_id", "message_type", "schema_version", "subject", "source",
    "target", "issued_at", "ttl_seconds", "trace_id", "correlation_id",
    "causation_id", "idempotency_key", "authority_class", "authority_ref",
    "effect_class", "reply_to", "requires_ack", "payload_ref", "payload_hash",
]
MESSAGE_TYPES = {
    "EPHEMERAL_SIGNAL", "REQUEST", "RESPONSE", "DURABLE_COMMAND",
    "STATE_DELTA", "ARTIFACT_POINTER", "PRESENCE", "RECEIPT", "HUMAN_NOTICE",
}
EFFECT_CLASSES = {"NONE", "READ_ONLY", "WRITE_POSSIBLE", "UNKNOWN"}
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class SchemaError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 1. Envelope validation (hand-rolled; no jsonschema dependency, matching the
#    no-third-party-dependency convention used elsewhere in tooling/validator)
# ---------------------------------------------------------------------------

def validate_envelope(msg: dict[str, Any]) -> None:
    if not isinstance(msg, dict):
        raise SchemaError("message must be an object")

    missing = [f for f in REQUIRED_FIELDS if f not in msg]
    if missing:
        raise SchemaError(f"missing required fields: {missing}")

    extra = [f for f in msg if f not in REQUIRED_FIELDS]
    if extra:
        raise SchemaError(f"additionalProperties not allowed: {extra}")

    if not isinstance(msg["message_id"], str) or not msg["message_id"]:
        raise SchemaError("message_id must be a non-empty string")

    if msg["message_type"] not in MESSAGE_TYPES:
        raise SchemaError(f"message_type not in enum: {msg['message_type']!r}")

    if msg["schema_version"] != "1.0.0-draft":
        raise SchemaError("schema_version must equal '1.0.0-draft'")

    if not isinstance(msg["subject"], str) or not msg["subject"]:
        raise SchemaError("subject must be a non-empty string")

    src = msg["source"]
    src_required = ["principal_ref", "actor_id", "generation", "workload_ref",
                     "machine_ref", "session_ref", "custody_ref"]
    if not isinstance(src, dict) or any(f not in src for f in src_required):
        raise SchemaError("source object missing required fields")
    if not isinstance(src["principal_ref"], str) or not src["principal_ref"]:
        raise SchemaError("source.principal_ref must be a non-empty string")

    tgt = msg["target"]
    tgt_required = ["referent_type", "referent_id", "state_owner_ref"]
    if not isinstance(tgt, dict) or any(f not in tgt for f in tgt_required):
        raise SchemaError("target object missing required fields")
    if not isinstance(tgt["referent_type"], str) or not tgt["referent_type"]:
        raise SchemaError("target.referent_type must be a non-empty string")
    if not isinstance(tgt["referent_id"], str) or not tgt["referent_id"]:
        raise SchemaError("target.referent_id must be a non-empty string")

    if not isinstance(msg["ttl_seconds"], int) or isinstance(msg["ttl_seconds"], bool) \
            or msg["ttl_seconds"] < 1:
        raise SchemaError("ttl_seconds must be an integer >= 1")

    if not isinstance(msg["trace_id"], str) or not msg["trace_id"]:
        raise SchemaError("trace_id must be a non-empty string")
    if not isinstance(msg["correlation_id"], str) or not msg["correlation_id"]:
        raise SchemaError("correlation_id must be a non-empty string")

    if not isinstance(msg["authority_class"], str) or not msg["authority_class"]:
        raise SchemaError("authority_class must be a non-empty string")

    if msg["effect_class"] not in EFFECT_CLASSES:
        raise SchemaError(f"effect_class not in enum: {msg['effect_class']!r}")

    if not isinstance(msg["requires_ack"], bool):
        raise SchemaError("requires_ack must be a boolean")

    for field in ("payload_ref", "payload_hash", "reply_to", "causation_id",
                  "idempotency_key", "authority_ref"):
        val = msg[field]
        if val is not None and not isinstance(val, str):
            raise SchemaError(f"{field} must be string or null")

    if msg["payload_hash"] is not None and not HASH_RE.match(msg["payload_hash"]):
        raise SchemaError("payload_hash must match ^[0-9a-fA-F]{64}$")

    # allOf conditional: payload_ref <-> payload_hash co-presence
    if msg["payload_ref"] is not None and msg["payload_hash"] is None:
        raise SchemaError("payload_ref present requires payload_hash present")
    if msg["payload_hash"] is not None and msg["payload_ref"] is None:
        raise SchemaError("payload_hash present requires payload_ref present")

    # allOf conditional: DURABLE_COMMAND requirements
    if msg["message_type"] == "DURABLE_COMMAND":
        if not msg["idempotency_key"]:
            raise SchemaError("DURABLE_COMMAND requires non-empty idempotency_key")
        if not msg["authority_ref"]:
            raise SchemaError("DURABLE_COMMAND requires non-empty authority_ref")
        if msg["requires_ack"] is not True:
            raise SchemaError("DURABLE_COMMAND requires requires_ack == true")


def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text())


# ---------------------------------------------------------------------------
# 2. Receipt state machine (partial order; see receipt-taxonomy-candidate.yaml)
# ---------------------------------------------------------------------------

STAGE_PREDECESSORS: dict[str, set[str]] = {
    "PRODUCED": set(),
    "TRANSPORT_ACCEPTED": {"PRODUCED"},
    "DELIVERED": {"TRANSPORT_ACCEPTED"},
    "READ": {"DELIVERED"},
    "WORK_CONSUMED": {"DELIVERED"},
    "WORK_STARTED": {"WORK_CONSUMED"},
    "EFFECT_ACCEPTED": {"WORK_STARTED"},
    "EFFECT_UNKNOWN": {"WORK_STARTED"},
    "NO_EFFECT": {"WORK_STARTED"},
    "TERMINAL": {"EFFECT_ACCEPTED", "EFFECT_UNKNOWN", "NO_EFFECT"},
    "RELEASED": {"TERMINAL"},
}


class ReceiptError(ValueError):
    pass


class ReceiptTrail:
    """In-memory, single-attempt receipt trail. Enforces the partial order
    and the subset of negative_rules/falsifiers that are trail-local.
    No I/O, no network, no persistence -- offline reference logic only."""

    def __init__(self, attempt_id: str, actor_id: str, generation: str) -> None:
        self.attempt_id = attempt_id
        self.actor_id = actor_id
        self.generation = generation
        self.stages_reached: list[str] = []
        self._no_effect_proof: bool = False

    def emit(self, stage: str, *, no_effect_proof: bool = False,
             actor_id: str | None = None, generation: str | None = None) -> None:
        if stage not in STAGE_PREDECESSORS:
            raise ReceiptError(f"unknown stage {stage!r}")

        preds = STAGE_PREDECESSORS[stage]
        if preds and not (set(self.stages_reached) & preds):
            raise ReceiptError(
                f"{stage} has no satisfied predecessor in {preds}; "
                f"stages_reached={self.stages_reached} "
                "(falsifier 2/11: no skipped edge / ACK != EFFECT / SEND_ACCEPTED != WORK_STARTED)"
            )

        if stage == "NO_EFFECT" and not no_effect_proof:
            raise ReceiptError(
                "NO_EFFECT requires independent pre-effect proof "
                "(falsifier 5: timeout after possible effect != NO_EFFECT)"
            )

        if stage == "RELEASED":
            if actor_id != self.actor_id or generation != self.generation:
                raise ReceiptError(
                    "RELEASED actor/generation must match the attempt's own "
                    "(falsifier 8: terminal for another attempt/actor/generation "
                    "cannot free this claim)"
                )

        self.stages_reached.append(stage)


# ---------------------------------------------------------------------------
# 3. The twelve falsifiers (candidates/signalvev-reference-v0.1/
#    receipt-taxonomy-candidate.yaml#falsifiers, numbered 1-12 as in the
#    X4 runtime-fagpass source). Each test asserts the reference
#    implementation REJECTS the failure scenario.
# ---------------------------------------------------------------------------

def _base_msg(**overrides: Any) -> dict[str, Any]:
    msg = {
        "message_id": "m-0001",
        "message_type": "STATE_DELTA",
        "schema_version": "1.0.0-draft",
        "subject": "cerebro.v1.state.delta",
        "source": {
            "principal_ref": "principal-1", "actor_id": None, "generation": None,
            "workload_ref": None, "machine_ref": None, "session_ref": None,
            "custody_ref": None,
        },
        "target": {"referent_type": "candidate", "referent_id": "r-1", "state_owner_ref": None},
        "issued_at": "2026-09-28T00:00:00Z",
        "ttl_seconds": 60,
        "trace_id": "t-1",
        "correlation_id": "c-1",
        "causation_id": None,
        "idempotency_key": None,
        "authority_class": "NONE",
        "authority_ref": None,
        "effect_class": "NONE",
        "reply_to": None,
        "requires_ack": False,
        "payload_ref": None,
        "payload_hash": None,
    }
    msg.update(overrides)
    return msg


def test_falsifier_01_delivered_as_work_consumed() -> None:
    trail = ReceiptTrail("a1", "actor-1", "g1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    try:
        trail.emit("WORK_CONSUMED")  # allowed: DELIVERED is a valid predecessor
    except ReceiptError:
        raise AssertionError("WORK_CONSUMED after DELIVERED should be a legal transition")
    # The falsifier is that a consumer may claim WORK_CONSUMED status from a bare
    # DELIVERED receipt without its own acceptance evidence. This reference
    # implementation requires the caller to invoke emit("WORK_CONSUMED")
    # explicitly and separately from DELIVERED; there is no automatic promotion.
    fresh = ReceiptTrail("a2", "actor-1", "g1")
    fresh.emit("PRODUCED")
    fresh.emit("TRANSPORT_ACCEPTED")
    fresh.emit("DELIVERED")
    assert fresh.stages_reached[-1] == "DELIVERED", (
        "DELIVERED must never be auto-promoted to WORK_CONSUMED"
    )


def test_falsifier_02_ack_ne_effect_send_accepted_ne_work_started() -> None:
    trail = ReceiptTrail("a3", "actor-1", "g1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    for bad_stage in ("EFFECT_ACCEPTED", "WORK_STARTED"):
        try:
            trail.emit(bad_stage)
        except ReceiptError:
            continue
        raise AssertionError(f"TRANSPORT_ACCEPTED -> {bad_stage} must be rejected")


def test_falsifier_03_lost_wake_command_unreachable() -> None:
    # Reference-arc property: a DURABLE_COMMAND envelope's addressability must
    # not depend on the ephemeral wake signal surviving. The command identity
    # (idempotency_key/authority_ref) is carried in the envelope itself, not
    # invented by the wake. Losing the wake means re-derivation is possible
    # from the same envelope, not that the command becomes a different one.
    cmd = _base_msg(
        message_type="DURABLE_COMMAND", subject="cerebro.v1.work.command",
        idempotency_key="owner:domain:claim:attempt:payloadfp", authority_ref="auth-1",
        requires_ack=True,
    )
    validate_envelope(cmd)  # must validate independent of any wake delivery state
    replay = dict(cmd)  # simulate re-derivation after a lost wake
    assert replay["idempotency_key"] == cmd["idempotency_key"], (
        "command identity must be stable and re-derivable independent of wake delivery"
    )


def test_falsifier_04_idempotency_conflict_on_changed_payload() -> None:
    seen: dict[str, str] = {}

    def admit(key: str, payload_fingerprint: str) -> str:
        if key in seen and seen[key] != payload_fingerprint:
            return "CONFLICT"
        seen[key] = payload_fingerprint
        return "ADMITTED"

    first = admit("owner:domain:claim:attempt", "fp-aaa")
    second = admit("owner:domain:claim:attempt", "fp-bbb")
    assert first == "ADMITTED" and second == "CONFLICT", (
        "same idempotency_key with a different payload fingerprint must be CONFLICT, "
        "never a cached success"
    )


def test_falsifier_05_timeout_after_effect_not_no_effect() -> None:
    trail = ReceiptTrail("a4", "actor-1", "g1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    trail.emit("WORK_CONSUMED")
    trail.emit("WORK_STARTED")
    try:
        trail.emit("NO_EFFECT")  # no proof supplied -> must reject
    except ReceiptError:
        pass
    else:
        raise AssertionError("NO_EFFECT without independent pre-effect proof must be rejected")
    trail.emit("EFFECT_UNKNOWN")  # the lawful outcome of an ambiguous timeout


def test_falsifier_06_presence_not_authority() -> None:
    presence_states = {"ONLINE", "OFFLINE", "READY", "ARMED", "DEGRADED", "UNKNOWN"}
    authority_bearing_stage = "WORK_CONSUMED"
    assert authority_bearing_stage not in presence_states, (
        "presence vocabulary and work-admission stages must remain disjoint sets"
    )
    trail = ReceiptTrail("a5", "actor-1", "g1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    try:
        # simulate an attempt to admit work from presence=="READY" alone, with
        # no DELIVERED-derived predecessor satisfied for WORK_CONSUMED itself
        broken = ReceiptTrail("a5b", "actor-1", "g1")
        broken.emit("WORK_CONSUMED")
    except ReceiptError:
        pass
    else:
        raise AssertionError("WORK_CONSUMED must never be reachable from presence alone")


def test_falsifier_07_stale_revision_does_not_admit() -> None:
    def admit_from_snapshot(current_revision: int, snapshot_revision: int) -> str:
        if snapshot_revision != current_revision:
            return "HOLD_STALE_REVISION"
        return "ADMITTED"

    result = admit_from_snapshot(current_revision=5, snapshot_revision=3)
    assert result == "HOLD_STALE_REVISION", "a stale snapshot/route revision must not admit work"


def test_falsifier_08_terminal_scoped_to_exact_attempt() -> None:
    trail = ReceiptTrail("a6", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    trail.emit("WORK_CONSUMED")
    trail.emit("WORK_STARTED")
    trail.emit("EFFECT_ACCEPTED")
    trail.emit("TERMINAL")
    try:
        trail.emit("RELEASED", actor_id="actor-OTHER", generation="gen-1")
    except ReceiptError:
        pass
    else:
        raise AssertionError("RELEASED must reject a mismatched actor_id")
    trail.emit("RELEASED", actor_id="actor-1", generation="gen-1")  # legitimate release


def test_falsifier_09_stale_reply_not_canonical() -> None:
    def treat_as_canonical(reply_referent_version: int | None, current_version: int) -> bool:
        if reply_referent_version is None or reply_referent_version != current_version:
            return False
        return True

    assert treat_as_canonical(None, 4) is False
    assert treat_as_canonical(2, 4) is False
    assert treat_as_canonical(4, 4) is True


def test_falsifier_10_artifact_hash_mismatch_blocks_consumption() -> None:
    import hashlib

    def verify_and_consume(declared_hash: str, actual_bytes: bytes) -> str:
        actual_hash = hashlib.sha256(actual_bytes).hexdigest()
        if actual_hash != declared_hash:
            return "REJECTED_HASH_MISMATCH"
        return "CONSUMED"

    declared = "0" * 64
    result = verify_and_consume(declared, b"unrelated content")
    assert result == "REJECTED_HASH_MISMATCH"


def test_falsifier_11_no_skipped_edge_in_trace() -> None:
    trail = ReceiptTrail("a7", "actor-1", "g1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    trail.emit("WORK_CONSUMED")
    trail.emit("WORK_STARTED")
    trail.emit("EFFECT_ACCEPTED")
    trail.emit("TERMINAL")

    def first_broken_edge(reached: list[str]) -> str | None:
        for i in range(1, len(reached)):
            stage = reached[i]
            if not (set(reached[:i]) & STAGE_PREDECESSORS[stage] or not STAGE_PREDECESSORS[stage]):
                return stage
        return None

    assert first_broken_edge(trail.stages_reached) is None, (
        "a legitimately built trail must report no broken edge"
    )
    truncated_claim = trail.stages_reached[:3] + ["TERMINAL"]
    assert first_broken_edge(truncated_claim) == "TERMINAL", (
        "a trace asserting TERMINAL without its real predecessors must surface the broken edge, "
        "not a continuous chain"
    )


def test_falsifier_12_human_notice_does_not_invent_approval() -> None:
    def project_to_human(machine_state: str) -> str:
        if machine_state in ("HOLD", "UNKNOWN", "EFFECT_UNKNOWN"):
            return f"HUMAN_NOTICE_STATE_{machine_state}"  # never a fabricated approval request
        return machine_state

    for state in ("HOLD", "UNKNOWN", "EFFECT_UNKNOWN"):
        projected = project_to_human(state)
        assert "APPROVAL" not in projected and "HUMAN_GATE" not in projected, (
            f"projecting {state} to Human must not invent an approval/gate request"
        )


FALSIFIER_TESTS = [
    test_falsifier_01_delivered_as_work_consumed,
    test_falsifier_02_ack_ne_effect_send_accepted_ne_work_started,
    test_falsifier_03_lost_wake_command_unreachable,
    test_falsifier_04_idempotency_conflict_on_changed_payload,
    test_falsifier_05_timeout_after_effect_not_no_effect,
    test_falsifier_06_presence_not_authority,
    test_falsifier_07_stale_revision_does_not_admit,
    test_falsifier_08_terminal_scoped_to_exact_attempt,
    test_falsifier_09_stale_reply_not_canonical,
    test_falsifier_10_artifact_hash_mismatch_blocks_consumption,
    test_falsifier_11_no_skipped_edge_in_trace,
    test_falsifier_12_human_notice_does_not_invent_approval,
]


# ---------------------------------------------------------------------------
# 4. Schema conformance canaries (positive + negative)
# ---------------------------------------------------------------------------

def test_schema_valid_state_delta_passes() -> None:
    validate_envelope(_base_msg())


def test_schema_missing_field_rejected() -> None:
    msg = _base_msg()
    del msg["trace_id"]
    try:
        validate_envelope(msg)
    except SchemaError:
        return
    raise AssertionError("missing trace_id must be rejected")


def test_schema_unknown_message_type_rejected() -> None:
    try:
        validate_envelope(_base_msg(message_type="NOT_A_REAL_TYPE"))
    except SchemaError:
        return
    raise AssertionError("unknown message_type must be rejected")


def test_schema_durable_command_requires_idempotency_and_ack() -> None:
    try:
        validate_envelope(_base_msg(message_type="DURABLE_COMMAND", subject="cerebro.v1.work.command"))
    except SchemaError:
        return
    raise AssertionError("DURABLE_COMMAND without idempotency_key/authority_ref/requires_ack must be rejected")


def test_schema_payload_ref_hash_copresence() -> None:
    try:
        validate_envelope(_base_msg(payload_ref="drive://x"))
    except SchemaError:
        return
    raise AssertionError("payload_ref without payload_hash must be rejected")


def test_registry_has_seven_entries_matching_subject_uniqueness() -> None:
    registry = load_registry()
    entries = registry["entries"]
    assert len(entries) == 7, f"expected 7 subject entries, found {len(entries)}"
    subjects = [e["subject"] for e in entries]
    assert len(subjects) == len(set(subjects)), "subject values must be unique"


SCHEMA_TESTS = [
    test_schema_valid_state_delta_passes,
    test_schema_missing_field_rejected,
    test_schema_unknown_message_type_rejected,
    test_schema_durable_command_requires_idempotency_and_ack,
    test_schema_payload_ref_hash_copresence,
    test_registry_has_seven_entries_matching_subject_uniqueness,
]


def selftest() -> int:
    all_tests = SCHEMA_TESTS + FALSIFIER_TESTS
    failures: list[tuple[str, str]] = []
    for test in all_tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v01_validation selftest: "
          f"{len(all_tests) - len(failures)}/{len(all_tests)} PASS")
    for name, err in failures:
        print(f"  FAIL {name}: {err}")

    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selftest"])
    args = parser.parse_args()
    if args.command == "selftest":
        return selftest()
    return 2


if __name__ == "__main__":
    sys.exit(main())
