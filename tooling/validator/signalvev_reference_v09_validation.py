#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.9 (first-broken-edge flight recorder:
the read-only "what happened to message X" reconstruction the Human
text names and X4 runtime-fagpass's own crosswalk marks as a material
L6 gap).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here, and this candidate generates no trace/correlation/
causation IDs of its own -- it only reconstructs from receipts a caller
already holds.

This module deliberately does NOT reimplement v0.1's receipt logic --
it imports ReceiptTrail/ReceiptError from
signalvev_reference_v01_validation.py. It has no dependency on v0.2,
v0.3, v0.5, v0.6, v0.7, or v0.8 -- see component.yaml
explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.9/first-broken-edge-flight-recorder-candidate.yaml
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
# 1. The first-broken-edge recorder
# ---------------------------------------------------------------------------

RECEIPT_LADDER = (
    "PRODUCED",
    "TRANSPORT_ACCEPTED",
    "DELIVERED",
    "READ",
    "WORK_CONSUMED",
    "WORK_STARTED",
    "EFFECT",  # branch slot: satisfied by any of EFFECT_BRANCH_STAGES
    "TERMINAL",
    "RELEASED",
)

EFFECT_BRANCH_STAGES = frozenset({"EFFECT_ACCEPTED", "EFFECT_UNKNOWN", "NO_EFFECT"})


class FlightRecorderError(ValueError):
    pass


class FirstBrokenEdgeRecorder:
    """Read-only reconstruction of one attempt's receipt ladder. Never
    infers, assumes, or fabricates a receipt that was not actually
    observed, and never reports a continuous chain past a gap that has
    later evidence."""

    def reconstruct(self, observed_stages) -> dict:
        observed = frozenset(observed_stages)  # never mutate caller's set
        unknown = observed - set(RECEIPT_LADDER) - EFFECT_BRANCH_STAGES
        if unknown:
            raise FlightRecorderError(f"unrecognized stage(s): {sorted(unknown)}")

        effect_observed = bool(observed & EFFECT_BRANCH_STAGES)
        effect_label = next(iter(observed & EFFECT_BRANCH_STAGES), None)

        present = []
        for stage in RECEIPT_LADDER:
            if stage == "EFFECT":
                present.append(effect_observed)
            else:
                present.append(stage in observed)

        first_broken_index = None
        for i, is_present in enumerate(present):
            if not is_present and any(present[i + 1:]):
                first_broken_index = i
                break

        reached_stages = tuple(
            (effect_label if RECEIPT_LADDER[i] == "EFFECT" else RECEIPT_LADDER[i])
            for i, is_present in enumerate(present)
            if is_present
        )

        if first_broken_index is not None:
            return {
                "status": "BROKEN",
                "first_broken_edge": RECEIPT_LADDER[first_broken_index],
                "reached_stages": reached_stages,
            }

        if all(present):
            return {
                "status": "COMPLETE",
                "first_broken_edge": None,
                "reached_stages": reached_stages,
            }

        return {
            "status": "IN_PROGRESS_NO_BREAK",
            "first_broken_edge": None,
            "reached_stages": reached_stages,
        }


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers FR1-FR8
# ---------------------------------------------------------------------------

def test_fr0_golden_path_full_ladder_reports_complete() -> None:
    recorder = FirstBrokenEdgeRecorder()
    observed = {
        "PRODUCED", "TRANSPORT_ACCEPTED", "DELIVERED", "READ",
        "WORK_CONSUMED", "WORK_STARTED", "EFFECT_ACCEPTED",
        "TERMINAL", "RELEASED",
    }
    report = recorder.reconstruct(observed)
    assert report["status"] == "COMPLETE"
    assert report["first_broken_edge"] is None
    assert len(report["reached_stages"]) == len(RECEIPT_LADDER)


def test_fr1_gap_in_middle_reports_first_broken_edge_not_fabricated() -> None:
    recorder = FirstBrokenEdgeRecorder()
    # DELIVERED is missing, but READ exists -- proves the chain moved past
    # the gap without leaving evidence there.
    observed = {"PRODUCED", "TRANSPORT_ACCEPTED", "READ"}
    report = recorder.reconstruct(observed)
    assert report["status"] == "BROKEN"
    assert report["first_broken_edge"] == "DELIVERED"
    assert "DELIVERED" not in report["reached_stages"], (
        "the recorder must never fabricate the missing receipt it reports"
    )


def test_fr2_naturally_stopped_sequence_is_in_progress_not_broken() -> None:
    # Nothing later than WORK_CONSUMED exists -- this is simply not yet
    # reached, not a broken edge. A false BROKEN claim here would be its
    # own falsifier violation.
    recorder = FirstBrokenEdgeRecorder()
    observed = {"PRODUCED", "TRANSPORT_ACCEPTED", "DELIVERED", "READ", "WORK_CONSUMED"}
    report = recorder.reconstruct(observed)
    assert report["status"] == "IN_PROGRESS_NO_BREAK"
    assert report["first_broken_edge"] is None


def test_fr3_effect_branch_any_of_three_satisfies_ladder_position() -> None:
    recorder = FirstBrokenEdgeRecorder()
    base = {"PRODUCED", "TRANSPORT_ACCEPTED", "DELIVERED", "READ", "WORK_CONSUMED", "WORK_STARTED"}
    for branch in ("EFFECT_ACCEPTED", "EFFECT_UNKNOWN", "NO_EFFECT"):
        observed = base | {branch, "TERMINAL", "RELEASED"}
        report = recorder.reconstruct(observed)
        assert report["status"] == "COMPLETE", f"branch {branch} must satisfy the EFFECT ladder slot"
        assert branch in report["reached_stages"]


def test_fr4_never_fabricates_missing_receipt_evidence() -> None:
    recorder = FirstBrokenEdgeRecorder()
    observed = {"PRODUCED", "WORK_STARTED"}  # a very large gap
    report = recorder.reconstruct(observed)
    assert report["status"] == "BROKEN"
    assert report["first_broken_edge"] == "TRANSPORT_ACCEPTED"
    for missing in ("TRANSPORT_ACCEPTED", "DELIVERED", "READ", "WORK_CONSUMED"):
        assert missing not in report["reached_stages"]


def test_fr5_empty_observed_set_is_in_progress_not_broken_or_complete() -> None:
    recorder = FirstBrokenEdgeRecorder()
    report = recorder.reconstruct(set())
    assert report["status"] == "IN_PROGRESS_NO_BREAK", (
        "no evidence at all must never be reported as COMPLETE or as an invented BROKEN edge"
    )
    assert report["reached_stages"] == ()


def test_fr6_multiple_gaps_reports_only_the_earliest_one() -> None:
    # Both TRANSPORT_ACCEPTED and WORK_CONSUMED are missing; only the
    # first (earliest) broken edge is reported, per the Human text's
    # own "forste brutte edge" (first broken edge) wording.
    recorder = FirstBrokenEdgeRecorder()
    observed = {"PRODUCED", "DELIVERED", "READ", "WORK_STARTED"}
    report = recorder.reconstruct(observed)
    assert report["status"] == "BROKEN"
    assert report["first_broken_edge"] == "TRANSPORT_ACCEPTED"


def test_fr7_released_without_terminal_is_broken_not_complete() -> None:
    # RELEASED present but TERMINAL absent is exactly the falsifier
    # scenario: a later stage exists while an earlier required edge does
    # not -- must be BROKEN, never silently treated as a clean chain.
    recorder = FirstBrokenEdgeRecorder()
    observed = {
        "PRODUCED", "TRANSPORT_ACCEPTED", "DELIVERED", "READ",
        "WORK_CONSUMED", "WORK_STARTED", "EFFECT_ACCEPTED", "RELEASED",
    }
    report = recorder.reconstruct(observed)
    assert report["status"] == "BROKEN"
    assert report["first_broken_edge"] == "TERMINAL"


def test_fr8_read_only_never_mutates_caller_input_and_rejects_unknown_stage() -> None:
    recorder = FirstBrokenEdgeRecorder()
    caller_set = {"PRODUCED", "DELIVERED"}
    frozen_copy = frozenset(caller_set)
    recorder.reconstruct(caller_set)
    assert caller_set == frozen_copy, "reconstruct() must never mutate the caller's observed-stage set"

    try:
        recorder.reconstruct({"PRODUCED", "NOT_A_REAL_STAGE"})
    except FlightRecorderError:
        pass
    else:
        raise AssertionError("an unrecognized stage name must raise, never be silently ignored")

    # Confirm this candidate does not bypass v0.1's own receipt-trail
    # ordering -- it is a separate, read-only reconstruction, not a
    # replacement for ReceiptTrail's own stage enforcement.
    trail = ReceiptTrail("attempt-fr8", "actor-1", "gen-1")
    trail.emit("PRODUCED")
    try:
        trail.emit("WORK_STARTED")  # skip straight past required predecessors
    except ReceiptError:
        return
    raise AssertionError("v0.1's own STAGE_PREDECESSORS must still reject skipped stages")


FIRST_BROKEN_EDGE_RECORDER_TESTS = [
    test_fr0_golden_path_full_ladder_reports_complete,
    test_fr1_gap_in_middle_reports_first_broken_edge_not_fabricated,
    test_fr2_naturally_stopped_sequence_is_in_progress_not_broken,
    test_fr3_effect_branch_any_of_three_satisfies_ladder_position,
    test_fr4_never_fabricates_missing_receipt_evidence,
    test_fr5_empty_observed_set_is_in_progress_not_broken_or_complete,
    test_fr6_multiple_gaps_reports_only_the_earliest_one,
    test_fr7_released_without_terminal_is_broken_not_complete,
    test_fr8_read_only_never_mutates_caller_input_and_rejects_unknown_stage,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in FIRST_BROKEN_EDGE_RECORDER_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v09_validation selftest: "
          f"{len(FIRST_BROKEN_EDGE_RECORDER_TESTS) - len(failures)}/"
          f"{len(FIRST_BROKEN_EDGE_RECORDER_TESTS)} PASS")
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
