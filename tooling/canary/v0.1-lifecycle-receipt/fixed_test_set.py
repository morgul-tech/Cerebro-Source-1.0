#!/usr/bin/env python3
"""Fixed, pre-declared synthetic test set for the v0.1 bounded canary.

STATUS: canary preparation artifact. authority: NONE. Not yet executed.

Per PRINCIPAL DECISION (2026-09-29): "steng etter fast testsett" -- the
exact set of messages a canary run will send must be declared BEFORE the
run, not generated live. This module is that declaration. Both
producer.py and consumer.py import THIS SAME list; neither may invent a
message that is not in it, and the consumer independently verifies it
received exactly this set, in a sequence that satisfies v0.1's own
ReceiptTrail partial order -- nothing more, nothing less.

Scope (verbatim from PRINCIPAL DECISION, restated in RUNBOOK.md):
  - Isolated, non-production NATS context.
  - One subject: cerebro.v1.lifecycle.receipt.
  - One producer, one consumer, synthetic messages only.
  - No JetStream, no durable storage, no production consumers, no
    authority/state effect.
  - Max 60 minutes; close after this fixed test set completes.

This is a single golden-path receipt trail for one synthetic attempt --
the minimal trace that exercises every stage transition v0.1's
STAGE_PREDECESSORS defines on the "happy path" (PRODUCED through
RELEASED), so that real NATS delivery can be checked against the exact
same partial-order logic already verified offline in
signalvev_reference_v01_validation.py. It deliberately does not attempt
every falsifier branch (e.g. NO_EFFECT, EFFECT_UNKNOWN, a second
attempt/actor for RELEASED-mismatch) -- those remain offline-only
falsifiers; this canary's only new claim is "the same nine legal
transitions survive one real trip over NATS, unmutated, in order,
undropped, unduplicated."

Stage is NOT a schema field (CEREBRO_MESSAGE_V1 draft has no such field --
see RUNBOOK.md open deviation #1). It is encoded, by canary-only
convention, as a suffix on message_id: "<attempt_id>::STAGE::<stage>".
This does not extend or modify the schema; validate_envelope() from v0.1
is run unmodified against every message below and must accept it purely
because message_id is a non-empty string -- the suffix carries no
schema-level meaning.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tooling" / "validator"))

import signalvev_reference_v01_validation as v01  # noqa: E402

SUBJECT = "cerebro.v1.lifecycle.receipt"
ATTEMPT_ID = "canary-v0.1-attempt-0001"
TRACE_ID = "canary-v0.1-trace-0001"
CORRELATION_ID = "canary-v0.1-correlation-0001"
SYNTHETIC_ACTOR = "CANARY-SYNTHETIC-ACTOR"
SYNTHETIC_GENERATION = "CANARY-SYNTHETIC-GEN-1"

# The single golden path this canary proves, in required order.
# (See v01.STAGE_PREDECESSORS for the full partial order this is one
# legal linearisation of.)
STAGE_SEQUENCE: list[str] = [
    "PRODUCED",
    "TRANSPORT_ACCEPTED",
    "DELIVERED",
    "READ",
    "WORK_CONSUMED",
    "WORK_STARTED",
    "EFFECT_ACCEPTED",
    "TERMINAL",
    "RELEASED",
]


def _message_id(stage: str) -> str:
    return f"{ATTEMPT_ID}::STAGE::{stage}"


def build_fixed_test_set() -> list[dict[str, Any]]:
    """Build the exact, ordered list of envelopes this canary will send.

    Each envelope is schema-valid per v0.1's validate_envelope(), built
    from v0.1's own _base_msg-equivalent fields (reconstructed here
    explicitly rather than importing the private _base_msg helper, so
    this module's message shape is self-evident without reading v0.1's
    internals). causation_id chains each message to the previous one,
    exercising L6 trace/causation continuity over the real transport.
    """
    messages: list[dict[str, Any]] = []
    prev_message_id: str | None = None

    for stage in STAGE_SEQUENCE:
        msg: dict[str, Any] = {
            "message_id": _message_id(stage),
            "message_type": "RECEIPT",
            "schema_version": "1.0.0-draft",
            "subject": SUBJECT,
            "source": {
                "principal_ref": "canary-principal",
                "actor_id": SYNTHETIC_ACTOR,
                "generation": SYNTHETIC_GENERATION,
                "workload_ref": "canary-v0.1-lifecycle-receipt",
                "machine_ref": None,
                "session_ref": None,
                "custody_ref": None,
            },
            "target": {
                "referent_type": "canary_attempt",
                "referent_id": ATTEMPT_ID,
                "state_owner_ref": None,
            },
            "issued_at": "2026-09-29T00:00:00Z",
            "ttl_seconds": 3600,
            "trace_id": TRACE_ID,
            "correlation_id": CORRELATION_ID,
            "causation_id": prev_message_id,
            "idempotency_key": None,
            "authority_class": "NONE",
            "authority_ref": None,
            "effect_class": "NONE",
            "reply_to": None,
            "requires_ack": False,
            "payload_ref": None,
            "payload_hash": None,
        }
        v01.validate_envelope(msg)  # fail fast if the fixed set is itself invalid
        messages.append(msg)
        prev_message_id = msg["message_id"]

    return messages


def stage_from_message_id(message_id: str) -> str | None:
    prefix = f"{ATTEMPT_ID}::STAGE::"
    if not message_id.startswith(prefix):
        return None
    stage = message_id[len(prefix):]
    return stage if stage in v01.STAGE_PREDECESSORS else None


if __name__ == "__main__":
    fixed = build_fixed_test_set()
    print(f"{len(fixed)} fixed synthetic messages declared, all schema-valid:")
    for m in fixed:
        print(f"  {m['message_id']}")
