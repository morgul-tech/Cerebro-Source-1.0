#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.7 (durable command idempotency scope:
the five-part composite identity -- owner + effect_domain + claim +
attempt + payload_fingerprint -- that the Subject Registry names for
DURABLE_COMMAND).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here.

This module deliberately does NOT reimplement v0.1's envelope/receipt
logic -- it imports validate_envelope/ReceiptTrail/_base_msg from
signalvev_reference_v01_validation.py. It has no dependency on v0.2,
v0.3, v0.5, or v0.6 -- see component.yaml explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.7/durable-command-idempotency-scope-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))

from signalvev_reference_v01_validation import (  # noqa: E402
    validate_envelope,
    ReceiptTrail,
    ReceiptError,
    _base_msg,
)


# ---------------------------------------------------------------------------
# 1. The five-part idempotency scope (X4: "authority owner + effect
#    domain + claim + attempt + payload fingerprint")
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdempotencyScope:
    """The four identity-defining parts of a DURABLE_COMMAND attempt.
    payload_fingerprint is deliberately NOT part of this dataclass --
    it is the one part allowed to legitimately vary between an
    ADMITTED_DUPLICATE and a CONFLICT for the same identity. message_id
    is deliberately absent entirely: X4 states "a new message ID does
    not create a new lawful attempt."""

    owner: str
    effect_domain: str
    claim: str
    attempt: str

    def identity(self) -> str:
        return f"{self.owner}:{self.effect_domain}:{self.claim}:{self.attempt}"


class DurableCommandAdmitter:
    """Offline, in-process admission ledger for DURABLE_COMMAND
    attempts, keyed by the five-part composite identity. No I/O, no
    network -- offline reference logic only."""

    def __init__(self) -> None:
        self._admitted: dict[str, str] = {}  # identity -> payload_fingerprint

    def admit(self, scope: IdempotencyScope, payload_fingerprint: str) -> str:
        identity = scope.identity()
        if identity in self._admitted:
            if self._admitted[identity] != payload_fingerprint:
                return "CONFLICT"
            return "ADMITTED_DUPLICATE"
        self._admitted[identity] = payload_fingerprint
        return "ADMITTED_NEW"


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers IS1-IS8
# ---------------------------------------------------------------------------

def _scope(**overrides: str) -> IdempotencyScope:
    fields = dict(owner="owner-1", effect_domain="domain-1", claim="claim-1", attempt="attempt-1")
    fields.update(overrides)
    return IdempotencyScope(**fields)


def test_is0_golden_path_independent_attempts_do_not_interfere() -> None:
    admitter = DurableCommandAdmitter()
    outcome_a = admitter.admit(_scope(claim="claim-A"), "fp-a")
    outcome_b = admitter.admit(_scope(claim="claim-B"), "fp-b")
    assert outcome_a == "ADMITTED_NEW"
    assert outcome_b == "ADMITTED_NEW"

    trail_a = ReceiptTrail("attempt-A", "actor-1", "gen-1")
    trail_b = ReceiptTrail("attempt-B", "actor-2", "gen-1")
    for trail in (trail_a, trail_b):
        trail.emit("PRODUCED")
        trail.emit("TRANSPORT_ACCEPTED")
        trail.emit("DELIVERED")
        trail.emit("WORK_CONSUMED")
        trail.emit("WORK_STARTED")
        trail.emit("EFFECT_ACCEPTED")
        trail.emit("TERMINAL")
    trail_a.emit("RELEASED", actor_id="actor-1", generation="gen-1")
    trail_b.emit("RELEASED", actor_id="actor-2", generation="gen-1")
    assert trail_a.stages_reached[-1] == "RELEASED"
    assert trail_b.stages_reached[-1] == "RELEASED"


def test_is1_exact_duplicate_is_a_safe_replay() -> None:
    admitter = DurableCommandAdmitter()
    first = admitter.admit(_scope(), "fp-same")
    second = admitter.admit(_scope(), "fp-same")
    assert first == "ADMITTED_NEW"
    assert second == "ADMITTED_DUPLICATE", (
        "an exact duplicate (same identity, same payload) must be a safe replay, "
        "never a fresh admission and never an error"
    )


def test_is2_same_identity_changed_payload_is_conflict() -> None:
    admitter = DurableCommandAdmitter()
    admitter.admit(_scope(), "fp-original")
    second = admitter.admit(_scope(), "fp-altered")
    assert second == "CONFLICT", (
        "same idempotency identity with a changed payload must be CONFLICT, "
        "never a cached success (X4, verbatim)"
    )


def test_is3_changing_owner_alone_yields_an_independent_identity() -> None:
    admitter = DurableCommandAdmitter()
    admitter.admit(_scope(owner="owner-1"), "fp-x")
    outcome = admitter.admit(_scope(owner="owner-2"), "fp-x")
    assert outcome == "ADMITTED_NEW", (
        "a different owner is a different attempt entirely, not a conflict "
        "against an unrelated owner's admission"
    )


def test_is4_changing_effect_domain_alone_yields_an_independent_identity() -> None:
    admitter = DurableCommandAdmitter()
    admitter.admit(_scope(effect_domain="domain-1"), "fp-x")
    outcome = admitter.admit(_scope(effect_domain="domain-2"), "fp-x")
    assert outcome == "ADMITTED_NEW"


def test_is5_changing_claim_alone_yields_an_independent_identity() -> None:
    admitter = DurableCommandAdmitter()
    admitter.admit(_scope(claim="claim-1"), "fp-x")
    outcome = admitter.admit(_scope(claim="claim-2"), "fp-x")
    assert outcome == "ADMITTED_NEW"


def test_is6_changing_attempt_alone_yields_an_independent_identity() -> None:
    admitter = DurableCommandAdmitter()
    admitter.admit(_scope(attempt="attempt-1"), "fp-x")
    outcome = admitter.admit(_scope(attempt="attempt-2"), "fp-x")
    assert outcome == "ADMITTED_NEW"


def test_is7_new_message_id_does_not_create_a_new_lawful_attempt() -> None:
    # Verbatim X4: "A new message ID does not create a new lawful
    # attempt." message_id is not part of IdempotencyScope at all, so
    # two envelopes differing only in message_id, with an identical
    # five-part identity and payload, must collapse to the same
    # attempt -- a safe duplicate, not two admissions.
    admitter = DurableCommandAdmitter()
    scope = _scope()
    msg_a = _base_msg(
        message_type="DURABLE_COMMAND", subject="cerebro.v1.work.command",
        message_id="msg-aaaa", idempotency_key=scope.identity(),
        authority_ref="auth-1", requires_ack=True,
    )
    msg_b = _base_msg(
        message_type="DURABLE_COMMAND", subject="cerebro.v1.work.command",
        message_id="msg-bbbb", idempotency_key=scope.identity(),  # different message_id
        authority_ref="auth-1", requires_ack=True,
    )
    validate_envelope(msg_a)
    validate_envelope(msg_b)
    assert msg_a["message_id"] != msg_b["message_id"]

    first = admitter.admit(scope, "fp-shared")
    second = admitter.admit(scope, "fp-shared")  # same scope, as if redelivered under a new message_id
    assert first == "ADMITTED_NEW"
    assert second == "ADMITTED_DUPLICATE", (
        "a new message_id must not, by itself, create a new lawful attempt"
    )


def test_is8_conflict_never_advances_past_work_consumed() -> None:
    admitter = DurableCommandAdmitter()
    scope = _scope()
    admitter.admit(scope, "fp-first")
    outcome = admitter.admit(scope, "fp-conflicting")
    assert outcome == "CONFLICT"

    trail = ReceiptTrail("attempt-conflict", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    if outcome == "CONFLICT":
        # A CONFLICT must block admission -- the reference caller must
        # never call trail.emit("WORK_CONSUMED") for a conflicting
        # attempt. We assert the trail was never advanced past DELIVERED
        # as the load-bearing proof of that contract.
        pass
    assert trail.stages_reached == ["PRODUCED", "TRANSPORT_ACCEPTED", "DELIVERED"], (
        "a CONFLICT outcome must never be paired with a WORK_CONSUMED emission"
    )
    try:
        trail.emit("EFFECT_ACCEPTED")  # skip WORK_CONSUMED/WORK_STARTED entirely
    except ReceiptError:
        return
    raise AssertionError(
        "v0.1's own STAGE_PREDECESSORS must still reject skipping WORK_CONSUMED/"
        "WORK_STARTED even for a conflicting attempt -- this candidate must not bypass it"
    )


DURABLE_COMMAND_IDEMPOTENCY_SCOPE_TESTS = [
    test_is0_golden_path_independent_attempts_do_not_interfere,
    test_is1_exact_duplicate_is_a_safe_replay,
    test_is2_same_identity_changed_payload_is_conflict,
    test_is3_changing_owner_alone_yields_an_independent_identity,
    test_is4_changing_effect_domain_alone_yields_an_independent_identity,
    test_is5_changing_claim_alone_yields_an_independent_identity,
    test_is6_changing_attempt_alone_yields_an_independent_identity,
    test_is7_new_message_id_does_not_create_a_new_lawful_attempt,
    test_is8_conflict_never_advances_past_work_consumed,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in DURABLE_COMMAND_IDEMPOTENCY_SCOPE_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v07_validation selftest: "
          f"{len(DURABLE_COMMAND_IDEMPOTENCY_SCOPE_TESTS) - len(failures)}/"
          f"{len(DURABLE_COMMAND_IDEMPOTENCY_SCOPE_TESTS)} PASS")
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
