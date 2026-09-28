#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.5 (durable command outbox: the
restart-safe fixture X4 runtime-fagpass named in "One sequencing
correction" and recommended moving into the first reference wave).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here. "Restart-safe" is proven via a serialize/deserialize
snapshot round-trip into a fresh instance -- no real disk I/O, no real
process restart.

This module deliberately does NOT reimplement v0.1's receipt logic --
it imports validate_envelope/STAGE_PREDECESSORS/ReceiptTrail/
ReceiptError/_base_msg from signalvev_reference_v01_validation.py and
tests how the outbox composes with that already-merged stage machine.
It has no dependency on v0.2 or v0.3 -- see component.yaml
explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.5/durable-command-outbox-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))

from signalvev_reference_v01_validation import (  # noqa: E402
    validate_envelope,
    STAGE_PREDECESSORS,
    ReceiptTrail,
    ReceiptError,
    _base_msg,
)


# ---------------------------------------------------------------------------
# 1. The outbox fixture (sequencing steps 1, 3, 5)
# ---------------------------------------------------------------------------

class OutboxError(ValueError):
    pass


class DurableCommandOutbox:
    """Local, offline, restart-safe command outbox reference fixture.

    Not a deployment recommendation and not JetStream/NATS -- a fixture
    proving the X4 "One sequencing correction" contract can be satisfied
    without activating durable transport infrastructure. Restart-safety
    is proven via to_snapshot()/from_snapshot(): a fresh instance rebuilt
    purely from serialized state, with no live in-memory reference to the
    committing instance. No I/O, no network -- offline reference logic
    only.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._consumed: dict[str, str] = {}

    def commit(self, command_identity: str, claim_ref: str, envelope: dict[str, Any]) -> None:
        # Sequencing step 1: state owner commits the exact command
        # identity and current claim reference.
        if not command_identity:
            raise OutboxError("commit requires a non-empty command_identity")
        if not claim_ref:
            raise OutboxError(
                "commit requires an explicit claim_ref (sequencing step 1: "
                "the state owner's own claim reference, not invented later)"
            )
        self._store[command_identity] = {"claim_ref": claim_ref, "envelope": envelope}

    def reread(self, command_identity: str) -> dict[str, Any] | None:
        return self._store.get(command_identity)

    def all_pending_identities(self) -> list[str]:
        return [cid for cid in self._store if cid not in self._consumed]

    def mark_consumed(self, command_identity: str, actor_id: str) -> bool:
        if command_identity not in self._store:
            raise OutboxError(f"cannot consume unknown command_identity {command_identity!r}")
        if command_identity in self._consumed:
            return False  # already consumed -- idempotent no-op, not an error
        self._consumed[command_identity] = actor_id
        return True

    def to_snapshot(self) -> str:
        return json.dumps({"store": self._store, "consumed": self._consumed}, sort_keys=True)

    @classmethod
    def from_snapshot(cls, snapshot: str) -> "DurableCommandOutbox":
        data = json.loads(snapshot)
        instance = cls()
        instance._store = data["store"]
        instance._consumed = data["consumed"]
        return instance


class WakeSignal:
    """L2 ephemeral signal: a pointer only. Structurally carries no
    authority, claim, or payload field -- matches X4's crosswalk L2 row:
    "a signal must cause a canonical reread, never supply work authority
    itself."""

    __slots__ = ("command_identity",)

    def __init__(self, command_identity: str) -> None:
        self.command_identity = command_identity


def consume_via_wake(
    outbox: DurableCommandOutbox,
    wake: WakeSignal | None,
    trail: ReceiptTrail,
    actor_id: str,
    *,
    poll_identity: str | None = None,
) -> str:
    """Sequencing step 3: consumer wakes (or, if the wake was lost, polls
    all_pending_identities() instead), rereads the authoritative outbox,
    and emits WORK_CONSUMED only after accepting the exact item. Never
    admits from the wake's own payload -- the outbox record is the only
    authority. Returns WORK_CONSUMED_NEW, WORK_CONSUMED_ALREADY
    (redelivery no-op, sequencing step 5), or NO_SUCH_COMMAND.
    """
    if wake is not None:
        command_identity = wake.command_identity
    elif poll_identity is not None:
        command_identity = poll_identity
    else:
        raise OutboxError("no wake and no poll_identity: nothing to reread")

    record = outbox.reread(command_identity)
    if record is None:
        return "NO_SUCH_COMMAND"  # never admit from wake payload alone

    if "DELIVERED" not in trail.stages_reached:
        trail.emit("DELIVERED")  # still enforces v0.1's own predecessor chain

    is_new = outbox.mark_consumed(command_identity, actor_id)
    if is_new:
        trail.emit("WORK_CONSUMED")
        return "WORK_CONSUMED_NEW"
    return "WORK_CONSUMED_ALREADY"


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers OB1-OB8
# ---------------------------------------------------------------------------

def _durable_command_msg(**overrides: Any) -> dict[str, Any]:
    fields = dict(
        message_type="DURABLE_COMMAND",
        subject="cerebro.v1.work.command",
        idempotency_key="owner:domain:claim:attempt:payloadfp",
        authority_ref="auth-1",
        requires_ack=True,
    )
    fields.update(overrides)
    return _base_msg(**fields)


def test_ob0_golden_path_full_sequencing_correction() -> None:
    outbox = DurableCommandOutbox()
    cmd_msg = _durable_command_msg(message_id="cmd-9-msg")
    validate_envelope(cmd_msg)
    outbox.commit("cmd-9", "claim-9", cmd_msg)          # step 1
    wake = WakeSignal("cmd-9")                            # step 2: pointer only
    trail = ReceiptTrail("attempt-9", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    outcome = consume_via_wake(outbox, wake, trail, "actor-1")  # step 3
    assert outcome == "WORK_CONSUMED_NEW"
    trail.emit("WORK_STARTED")                             # step 4: separate receipts
    trail.emit("EFFECT_ACCEPTED")
    trail.emit("TERMINAL")
    trail.emit("RELEASED", actor_id="actor-1", generation="gen-1")
    assert trail.stages_reached[-1] == "RELEASED"


def test_ob1_lost_wake_still_reachable_via_poll() -> None:
    outbox = DurableCommandOutbox()
    outbox.commit("cmd-1", "claim-1", _durable_command_msg(message_id="cmd-1-msg"))
    trail = ReceiptTrail("attempt-1", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    pending = outbox.all_pending_identities()
    assert "cmd-1" in pending, "lost wake must not make a committed command unreachable"
    outcome = consume_via_wake(outbox, None, trail, "actor-1", poll_identity="cmd-1")
    assert outcome == "WORK_CONSUMED_NEW"


def test_ob2_survives_simulated_restart() -> None:
    outbox = DurableCommandOutbox()
    outbox.commit("cmd-2", "claim-2", _durable_command_msg(message_id="cmd-2-msg"))
    snapshot = outbox.to_snapshot()
    del outbox  # no live reference carries over -- only the snapshot does
    reloaded = DurableCommandOutbox.from_snapshot(snapshot)
    trail = ReceiptTrail("attempt-2", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    outcome = consume_via_wake(reloaded, WakeSignal("cmd-2"), trail, "actor-1")
    assert outcome == "WORK_CONSUMED_NEW", "a committed command must survive a simulated restart"


def test_ob3_redelivery_is_idempotent_noop() -> None:
    outbox = DurableCommandOutbox()
    outbox.commit("cmd-3", "claim-3", _durable_command_msg(message_id="cmd-3-msg"))
    trail = ReceiptTrail("attempt-3", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    first = consume_via_wake(outbox, WakeSignal("cmd-3"), trail, "actor-1")
    second = consume_via_wake(outbox, WakeSignal("cmd-3"), trail, "actor-1")  # redelivered wake
    assert first == "WORK_CONSUMED_NEW"
    assert second == "WORK_CONSUMED_ALREADY"
    assert trail.stages_reached.count("WORK_CONSUMED") == 1, (
        "redelivery must not create a second admission/effect (sequencing step 5)"
    )


def test_ob4_wake_pointer_not_in_outbox_never_admits() -> None:
    outbox = DurableCommandOutbox()
    trail = ReceiptTrail("attempt-4", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    outcome = consume_via_wake(outbox, WakeSignal("cmd-does-not-exist"), trail, "actor-1")
    assert outcome == "NO_SUCH_COMMAND"
    assert "WORK_CONSUMED" not in trail.stages_reached, (
        "a wake's own payload must never itself admit work -- only a matching outbox record can"
    )


def test_ob5_commit_requires_explicit_claim_ref() -> None:
    outbox = DurableCommandOutbox()
    try:
        outbox.commit("cmd-5", "", _durable_command_msg(message_id="cmd-5-msg"))
    except OutboxError:
        return
    raise AssertionError("commit without an explicit claim_ref must be rejected")


def test_ob6_delivered_emit_still_enforces_v01_predecessor_chain() -> None:
    outbox = DurableCommandOutbox()
    outbox.commit("cmd-6", "claim-6", _durable_command_msg(message_id="cmd-6-msg"))
    trail = ReceiptTrail("attempt-6", "actor-1", "gen-1")  # deliberately no PRODUCED/TRANSPORT_ACCEPTED
    try:
        consume_via_wake(outbox, WakeSignal("cmd-6"), trail, "actor-1")
    except ReceiptError:
        return
    raise AssertionError(
        "this candidate must not bypass v0.1's STAGE_PREDECESSORS -- DELIVERED "
        "still requires TRANSPORT_ACCEPTED to have been reached first"
    )


def test_ob7_two_commands_do_not_cross_contaminate() -> None:
    outbox = DurableCommandOutbox()
    outbox.commit("cmd-7a", "claim-7a", _durable_command_msg(message_id="cmd-7a-msg"))
    outbox.commit("cmd-7b", "claim-7b", _durable_command_msg(message_id="cmd-7b-msg"))
    trail_a = ReceiptTrail("attempt-7a", "actor-1", "gen-1")
    trail_a.emit("PRODUCED")
    trail_a.emit("TRANSPORT_ACCEPTED")
    consume_via_wake(outbox, WakeSignal("cmd-7a"), trail_a, "actor-1")
    assert "cmd-7a" not in outbox.all_pending_identities()
    assert "cmd-7b" in outbox.all_pending_identities(), (
        "consuming one command must not consume or hide an unrelated one"
    )


def test_ob8_wake_signal_carries_no_authority_field() -> None:
    wake = WakeSignal("cmd-8")
    for forbidden in ("authority_ref", "authority_class", "payload", "claim_ref", "effect_class"):
        assert not hasattr(wake, forbidden), (
            f"WakeSignal must not carry {forbidden!r} -- L2 signal is a pointer "
            "only, never work authority (X4 crosswalk, L2 Signal row)"
        )
    assert wake.command_identity == "cmd-8"


DURABLE_COMMAND_OUTBOX_TESTS = [
    test_ob0_golden_path_full_sequencing_correction,
    test_ob1_lost_wake_still_reachable_via_poll,
    test_ob2_survives_simulated_restart,
    test_ob3_redelivery_is_idempotent_noop,
    test_ob4_wake_pointer_not_in_outbox_never_admits,
    test_ob5_commit_requires_explicit_claim_ref,
    test_ob6_delivered_emit_still_enforces_v01_predecessor_chain,
    test_ob7_two_commands_do_not_cross_contaminate,
    test_ob8_wake_signal_carries_no_authority_field,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in DURABLE_COMMAND_OUTBOX_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v05_validation selftest: "
          f"{len(DURABLE_COMMAND_OUTBOX_TESTS) - len(failures)}/{len(DURABLE_COMMAND_OUTBOX_TESTS)} PASS")
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
