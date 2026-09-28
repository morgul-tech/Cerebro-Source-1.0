#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.8 (stale revision admission guard:
the "atomic compare-and-bind/admit" primitive X4 runtime-fagpass's own
crosswalk names as a material L4 gap).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code,
and no live provider integration, exists here.

This module deliberately does NOT reimplement v0.1's receipt logic --
it imports ReceiptTrail/ReceiptError from
signalvev_reference_v01_validation.py. It has no dependency on v0.2,
v0.3, v0.5, v0.6, or v0.7 -- see component.yaml
explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.8/stale-revision-admission-guard-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))

from signalvev_reference_v01_validation import (  # noqa: E402
    ReceiptTrail,
    ReceiptError,
)


# ---------------------------------------------------------------------------
# 1. The revision-guarded admitter (X4's "atomic compare-and-bind/admit")
# ---------------------------------------------------------------------------

class RevisionGuardError(ValueError):
    pass


class RevisionLedger:
    """Tracks a monotonically increasing current_revision per referent
    (state or route). Offline, in-process only -- no I/O, no network,
    not a live provider."""

    def __init__(self) -> None:
        self._current: dict[str, int] = {}

    def initialize(self, referent_id: str, *, starting_revision: int = 1) -> int:
        if referent_id in self._current:
            raise RevisionGuardError(f"referent {referent_id!r} already initialized")
        self._current[referent_id] = starting_revision
        return starting_revision

    def bump(self, referent_id: str) -> int:
        if referent_id not in self._current:
            raise RevisionGuardError(f"cannot bump unknown referent {referent_id!r}")
        self._current[referent_id] += 1
        return self._current[referent_id]

    def current(self, referent_id: str) -> int:
        if referent_id not in self._current:
            raise RevisionGuardError(f"unknown referent {referent_id!r}")
        return self._current[referent_id]


class RevisionGuardedAdmitter:
    """The compare-and-bind admission guard itself. Currentness is
    evaluated live against the ledger at the moment of admission, not
    cached from whenever the caller took its snapshot."""

    def __init__(self, ledger: RevisionLedger) -> None:
        self._ledger = ledger

    def admit(self, referent_id: str, snapshot_revision: int) -> str:
        current = self._ledger.current(referent_id)  # raises if unknown -- never silently admits
        if snapshot_revision != current:
            return "HOLD_STALE_REVISION"  # covers both behind-current and ahead-of-current
        return "ADMITTED"


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers RG1-RG8
# ---------------------------------------------------------------------------

def test_rg0_golden_path_stale_second_caller_is_held() -> None:
    ledger = RevisionLedger()
    ledger.initialize("state-1")  # revision 1
    guard = RevisionGuardedAdmitter(ledger)

    caller_a_outcome = guard.admit("state-1", snapshot_revision=1)
    assert caller_a_outcome == "ADMITTED"

    ledger.bump("state-1")  # some other actor changed state underneath -- now revision 2

    caller_b_outcome = guard.admit("state-1", snapshot_revision=1)  # stale snapshot
    assert caller_b_outcome == "HOLD_STALE_REVISION", (
        "a second caller holding a now-stale snapshot must be held, never admitted"
    )


def test_rg1_exact_revision_match_is_admitted() -> None:
    ledger = RevisionLedger()
    ledger.initialize("state-2", starting_revision=7)
    guard = RevisionGuardedAdmitter(ledger)
    assert guard.admit("state-2", snapshot_revision=7) == "ADMITTED"


def test_rg2_snapshot_behind_current_is_held() -> None:
    ledger = RevisionLedger()
    ledger.initialize("state-3")
    ledger.bump("state-3")
    ledger.bump("state-3")  # current_revision is now 3
    guard = RevisionGuardedAdmitter(ledger)
    assert guard.admit("state-3", snapshot_revision=1) == "HOLD_STALE_REVISION"


def test_rg3_snapshot_ahead_of_current_is_also_held() -> None:
    # A caller claiming a revision that does not yet exist is just as
    # invalid as a stale one -- the contract is exact match, not "not older".
    ledger = RevisionLedger()
    ledger.initialize("state-4")  # current_revision is 1
    guard = RevisionGuardedAdmitter(ledger)
    assert guard.admit("state-4", snapshot_revision=99) == "HOLD_STALE_REVISION"


def test_rg4_unknown_referent_never_silently_admits() -> None:
    ledger = RevisionLedger()
    guard = RevisionGuardedAdmitter(ledger)
    try:
        guard.admit("never-initialized", snapshot_revision=1)
    except RevisionGuardError:
        return
    raise AssertionError("an unknown referent must raise, never silently admit")


def test_rg5_previously_valid_snapshot_can_go_stale_before_admission() -> None:
    # Currentness is evaluated live at admission time, not cached from
    # whenever the snapshot was read -- this is the operationally
    # important case v0.1's original test never covered.
    ledger = RevisionLedger()
    ledger.initialize("state-5")
    guard = RevisionGuardedAdmitter(ledger)
    snapshot_taken_at = ledger.current("state-5")  # caller reads revision 1, holds onto it
    assert snapshot_taken_at == 1

    ledger.bump("state-5")  # world moves on before the caller gets to admit

    outcome = guard.admit("state-5", snapshot_revision=snapshot_taken_at)
    assert outcome == "HOLD_STALE_REVISION", (
        "a snapshot that was valid when read can still go stale before admission -- "
        "currentness must be checked live, not assumed from read time"
    )


def test_rg6_independent_referents_do_not_interfere() -> None:
    ledger = RevisionLedger()
    ledger.initialize("referent-A")
    ledger.initialize("referent-B")
    ledger.bump("referent-A")  # referent-A is now at revision 2
    guard = RevisionGuardedAdmitter(ledger)
    assert guard.admit("referent-A", snapshot_revision=1) == "HOLD_STALE_REVISION"
    assert guard.admit("referent-B", snapshot_revision=1) == "ADMITTED", (
        "bumping one referent's revision must never affect an unrelated referent's admission"
    )


def test_rg7_stale_hold_never_paired_with_work_consumed() -> None:
    ledger = RevisionLedger()
    ledger.initialize("state-7")
    ledger.bump("state-7")
    guard = RevisionGuardedAdmitter(ledger)
    outcome = guard.admit("state-7", snapshot_revision=1)  # stale
    assert outcome == "HOLD_STALE_REVISION"

    trail = ReceiptTrail("attempt-rg7", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    assert trail.stages_reached == ["PRODUCED", "TRANSPORT_ACCEPTED", "DELIVERED"], (
        "a HOLD_STALE_REVISION outcome must never be paired with a WORK_CONSUMED emission"
    )
    try:
        trail.emit("WORK_STARTED")  # skip WORK_CONSUMED entirely, as a held attempt must
    except ReceiptError:
        return
    raise AssertionError(
        "v0.1's own STAGE_PREDECESSORS must still reject skipping WORK_CONSUMED "
        "even for a held attempt -- this candidate must not bypass it"
    )


def test_rg8_route_revision_is_covered_not_just_state_revision() -> None:
    # Falsifier 7's own wording is "a stale snapshot OR route revision" --
    # this guard is not scoped to only one referent flavor.
    ledger = RevisionLedger()
    ledger.initialize("route:factory-floor-3")
    ledger.bump("route:factory-floor-3")
    guard = RevisionGuardedAdmitter(ledger)
    assert guard.admit("route:factory-floor-3", snapshot_revision=1) == "HOLD_STALE_REVISION"
    assert guard.admit("route:factory-floor-3", snapshot_revision=2) == "ADMITTED"


STALE_REVISION_ADMISSION_GUARD_TESTS = [
    test_rg0_golden_path_stale_second_caller_is_held,
    test_rg1_exact_revision_match_is_admitted,
    test_rg2_snapshot_behind_current_is_held,
    test_rg3_snapshot_ahead_of_current_is_also_held,
    test_rg4_unknown_referent_never_silently_admits,
    test_rg5_previously_valid_snapshot_can_go_stale_before_admission,
    test_rg6_independent_referents_do_not_interfere,
    test_rg7_stale_hold_never_paired_with_work_consumed,
    test_rg8_route_revision_is_covered_not_just_state_revision,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in STALE_REVISION_ADMISSION_GUARD_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v08_validation selftest: "
          f"{len(STALE_REVISION_ADMISSION_GUARD_TESTS) - len(failures)}/"
          f"{len(STALE_REVISION_ADMISSION_GUARD_TESTS)} PASS")
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
