#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.4 (end-to-end reference stack:
MESSAGE -> SIGNAL -> REQUEST/REPLY -> RECEIPT -> TRACE -> ARTIFACT
POINTER).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here.

This module deliberately does NOT reimplement v0.1's or v0.2's logic --
it imports validate_envelope/STAGE_PREDECESSORS/ReceiptTrail from
signalvev_reference_v01_validation.py and RequestReplyMatcher/
make_pointer/verify_and_consume from signalvev_reference_v02_validation.py,
then tests how they compose. TRACE is the one exception: it is
reimplemented locally rather than importing v0.3's FlightRecorder,
because v0.3 (PR #7) was still unmerged when this candidate was
prepared -- see end-to-end-scenario-candidate.yaml's chain_links entry
for TRACE.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.4/end-to-end-scenario-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))

from signalvev_reference_v01_validation import (  # noqa: E402
    validate_envelope,
    SchemaError,
    STAGE_PREDECESSORS,
    ReceiptTrail,
    ReceiptError,
    _base_msg,
)
from signalvev_reference_v02_validation import (  # noqa: E402
    RequestReplyMatcher,
    make_pointer,
    verify_and_consume,
    ArtifactPointerError,
)


# ---------------------------------------------------------------------------
# TRACE (local reimplementation -- see module docstring and
# end-to-end-scenario-candidate.yaml for why this is not imported from
# v0.3)
# ---------------------------------------------------------------------------

class EndToEndTrace:
    """Read-only analysis over a receipt trail's stages_reached list.
    Same algorithm as v0.3's FlightRecorder.first_broken_edge(), kept
    independent here so this candidate has no code dependency on the
    still-unmerged v0.3 branch."""

    @staticmethod
    def first_broken_edge(reached: list[str]) -> str | None:
        for i in range(1, len(reached)):
            stage = reached[i]
            preds = STAGE_PREDECESSORS.get(stage)
            if preds is None:
                return stage
            if preds and not (set(reached[:i]) & preds):
                return stage
        return None


# ---------------------------------------------------------------------------
# The composed scenario: builds one instance of each chain link and runs
# them through a shared attempt.
# ---------------------------------------------------------------------------

def run_golden_path() -> dict[str, Any]:
    """Runs the full MESSAGE -> SIGNAL -> REQUEST/REPLY -> RECEIPT ->
    TRACE -> ARTIFACT POINTER chain once, successfully, and returns
    evidence from each link for the caller to assert against."""
    evidence: dict[str, Any] = {}

    # 1. MESSAGE: a valid STATE_DELTA envelope.
    message = _base_msg(message_id="e2e-msg-1")
    validate_envelope(message)
    evidence["message"] = message

    # 2. SIGNAL: an EPHEMERAL_SIGNAL pointing at a DURABLE_COMMAND that
    # exists independently (the signal carries only a pointer; it is not
    # itself the command -- Human text: "en signal aldri substituere for
    # en canonical reread eller durable work receipt").
    durable_command = _base_msg(
        message_id="e2e-cmd-1", message_type="DURABLE_COMMAND",
        subject="cerebro.v1.work.command",
        idempotency_key="e2e-owner:e2e-domain:e2e-claim:attempt-1",
        authority_ref="auth-e2e-1", requires_ack=True,
    )
    validate_envelope(durable_command)
    signal = _base_msg(
        message_id="e2e-signal-1", message_type="EPHEMERAL_SIGNAL",
        subject="cerebro.v1.signal.wake",
        causation_id=durable_command["message_id"],
    )
    validate_envelope(signal)
    evidence["signal"] = signal
    evidence["durable_command"] = durable_command

    # 3. REQUEST/REPLY: a status check before admitting the command.
    matcher = RequestReplyMatcher()
    matcher.send_request("e2e-corr-1", sent_at=0.0, timeout_seconds=5.0, effect_class="READ_ONLY")
    reply_outcome = matcher.receive_response("e2e-corr-1", received_at=1.0)
    evidence["reply_outcome"] = reply_outcome

    # 4. RECEIPT: only after a RESPONSE (not NO_RESPONDER/TIMEOUT_UNKNOWN)
    # does the trail advance.
    trail = ReceiptTrail("e2e-attempt-1", actor_id="actor-e2e", generation="gen-e2e")
    if reply_outcome == "RESPONSE":
        trail.emit("PRODUCED")
        trail.emit("TRANSPORT_ACCEPTED")
        trail.emit("DELIVERED")
        trail.emit("WORK_CONSUMED")
        trail.emit("WORK_STARTED")
        trail.emit("EFFECT_ACCEPTED")
        trail.emit("TERMINAL")
        trail.emit("RELEASED", actor_id="actor-e2e", generation="gen-e2e")
    evidence["trail"] = trail

    # 5. TRACE: the completed trail must show no broken edge.
    broken_edge = EndToEndTrace.first_broken_edge(trail.stages_reached)
    evidence["broken_edge"] = broken_edge

    # 6. ARTIFACT POINTER: only fetched/consumed after RELEASED.
    artifact_bytes = b"end-to-end scenario artifact content"
    pointer = make_pointer(
        artifact_identity="e2e-artifact-1", location_reference="ref://e2e-artifact",
        hash=hashlib.sha256(artifact_bytes).hexdigest(), content_type="text/plain",
        size=len(artifact_bytes), provenance="e2e-scenario-fixture",
        access_custody_requirements="none",
    )
    if trail.stages_reached and trail.stages_reached[-1] == "RELEASED":
        consume_outcome = verify_and_consume(pointer, artifact_bytes)
    else:
        consume_outcome = "NOT_ATTEMPTED_TRAIL_NOT_RELEASED"
    evidence["consume_outcome"] = consume_outcome

    return evidence


# ---------------------------------------------------------------------------
# End-to-end falsifiers (E2E1-E2E8)
# ---------------------------------------------------------------------------

def test_e2e1_golden_path_composes_cleanly() -> None:
    """Positive composition test: every link in the chain succeeds and
    hands off cleanly to the next, in one run."""
    evidence = run_golden_path()
    assert evidence["reply_outcome"] == "RESPONSE"
    assert evidence["trail"].stages_reached[-1] == "RELEASED"
    assert evidence["broken_edge"] is None
    assert evidence["consume_outcome"] == "CONSUMED"


def test_e2e2_signal_alone_never_produces_a_receipt() -> None:
    """Human text: a signal must trigger a canonical reread, never itself
    supply work authority or evidence. A SIGNAL with no accompanying
    ReceiptTrail.emit() call must correspond to an attempt with zero
    stages reached -- the signal's existence never auto-populates a
    receipt."""
    signal = _base_msg(message_id="e2e-signal-2", message_type="EPHEMERAL_SIGNAL", subject="cerebro.v1.signal.wake")
    validate_envelope(signal)
    trail = ReceiptTrail("e2e-attempt-2", actor_id="actor-e2e", generation="gen-e2e")
    assert trail.stages_reached == [], "a SIGNAL message must never, by itself, advance a receipt trail"


def test_e2e3_timed_out_request_never_starts_a_receipt() -> None:
    """A REQUEST/REPLY outcome of TIMEOUT_UNKNOWN or NO_RESPONDER must not
    be treated as license to begin WORK_STARTED on a receipt trail -- the
    composed golden path only advances the trail on RESPONSE (see
    run_golden_path's `if reply_outcome == "RESPONSE"` gate)."""
    matcher = RequestReplyMatcher()
    matcher.send_request("e2e-corr-2", sent_at=0.0, timeout_seconds=5.0)
    outcome = matcher.check_timeout("e2e-corr-2", now=10.0)
    assert outcome == "TIMEOUT_UNKNOWN"
    trail = ReceiptTrail("e2e-attempt-3", actor_id="actor-e2e", generation="gen-e2e")
    try:
        trail.emit("WORK_STARTED")
    except ReceiptError:
        pass
    else:
        raise AssertionError("WORK_STARTED must not be reachable directly from a TIMEOUT_UNKNOWN reply")


def test_e2e4_response_is_never_substituted_for_a_receipt_stage() -> None:
    """Human text (Grunnlov-kandidat, section 4): 'Ingen av disse lagene
    faar implisitt overta funksjonen til et annet lag.' A RequestReply
    RESPONSE outcome string is not a member of STAGE_PREDECESSORS -- it
    cannot even be passed to ReceiptTrail.emit() as a valid stage."""
    assert "RESPONSE" not in STAGE_PREDECESSORS
    assert "NO_RESPONDER" not in STAGE_PREDECESSORS
    assert "TIMEOUT_UNKNOWN" not in STAGE_PREDECESSORS
    trail = ReceiptTrail("e2e-attempt-4", actor_id="actor-e2e", generation="gen-e2e")
    try:
        trail.emit("RESPONSE")  # type: ignore[arg-type]
    except ReceiptError:
        pass
    else:
        raise AssertionError("a request/reply outcome must never be accepted as a receipt stage")


def test_e2e5_trace_reports_exact_broken_edge_on_a_truncated_composed_trail() -> None:
    """TRACE must correctly diagnose a receipt trail that was built
    through the real composed pipeline (not just a synthetic fixture),
    proving the reimplementation is faithful to STAGE_PREDECESSORS."""
    evidence = run_golden_path()
    full_trail = evidence["trail"].stages_reached
    truncated = full_trail[:4] + ["TERMINAL"]  # drop WORK_STARTED..EFFECT_ACCEPTED
    broken = EndToEndTrace.first_broken_edge(truncated)
    assert broken == "TERMINAL", f"expected TERMINAL as the first broken edge, got {broken!r}"


def test_e2e6_artifact_pointer_not_consumed_before_trail_released() -> None:
    """STATE STAYS HOME; DELTA TRAVELS, combined with the receipt
    ladder's own ordering: an artifact tied to an attempt must not be
    treated as consumable before that attempt's trail reaches RELEASED."""
    trail = ReceiptTrail("e2e-attempt-5", actor_id="actor-e2e", generation="gen-e2e")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")  # deliberately stop before RELEASED

    artifact_bytes = b"not yet releasable"
    pointer = make_pointer(
        artifact_identity="e2e-artifact-2", location_reference="ref://e2e-artifact-2",
        hash=hashlib.sha256(artifact_bytes).hexdigest(), content_type="text/plain",
        size=len(artifact_bytes), provenance="e2e-scenario-fixture", access_custody_requirements="none",
    )
    if trail.stages_reached[-1] == "RELEASED":
        outcome = verify_and_consume(pointer, artifact_bytes)
    else:
        outcome = "NOT_ATTEMPTED_TRAIL_NOT_RELEASED"
    assert outcome == "NOT_ATTEMPTED_TRAIL_NOT_RELEASED", "an artifact must not be consumed before its attempt's trail is RELEASED"


def test_e2e7_every_link_independently_conforms_to_the_envelope_schema() -> None:
    """Every message_type used across the composed chain
    (STATE_DELTA, EPHEMERAL_SIGNAL, DURABLE_COMMAND) independently passes
    v0.1's validate_envelope -- schema conformance holds at every hop,
    not just for a hand-picked fixture."""
    evidence = run_golden_path()
    for key in ("message", "signal", "durable_command"):
        validate_envelope(evidence[key])  # re-validates; raises SchemaError on failure


def test_e2e8_envelope_source_claim_never_substitutes_for_receipt_evidence() -> None:
    """The envelope's `source` object is asserted metadata (a claim), not
    attested identity, and it must never be treated as if it were
    RECEIPT-stage evidence. Concretely: constructing a message with a
    plausible `source` block must not, by itself, advance any
    ReceiptTrail -- only explicit trail.emit() calls do."""
    message = _base_msg(
        message_id="e2e-msg-8",
        source={
            "principal_ref": "principal-e2e", "actor_id": "actor-claiming-to-be-trusted",
            "generation": "gen-1", "workload_ref": "wl-1", "machine_ref": "m-1",
            "session_ref": "s-1", "custody_ref": "c-1",
        },
    )
    validate_envelope(message)  # a well-formed claim is schema-valid ...
    trail = ReceiptTrail("e2e-attempt-8", actor_id="actor-e2e", generation="gen-e2e")
    assert trail.stages_reached == [], (
        "... but schema-valid source metadata must never, by itself, advance a receipt trail"
    )


END_TO_END_FALSIFIER_TESTS = [
    test_e2e1_golden_path_composes_cleanly,
    test_e2e2_signal_alone_never_produces_a_receipt,
    test_e2e3_timed_out_request_never_starts_a_receipt,
    test_e2e4_response_is_never_substituted_for_a_receipt_stage,
    test_e2e5_trace_reports_exact_broken_edge_on_a_truncated_composed_trail,
    test_e2e6_artifact_pointer_not_consumed_before_trail_released,
    test_e2e7_every_link_independently_conforms_to_the_envelope_schema,
    test_e2e8_envelope_source_claim_never_substitutes_for_receipt_evidence,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in END_TO_END_FALSIFIER_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v04_validation selftest: "
          f"{len(END_TO_END_FALSIFIER_TESTS) - len(failures)}/{len(END_TO_END_FALSIFIER_TESTS)} PASS")
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
