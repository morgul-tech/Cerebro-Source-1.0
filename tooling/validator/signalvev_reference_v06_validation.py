#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.6 (human notice projection: the L7
"shared rule for projecting uncertain machine state" that X4
runtime-fagpass's own crosswalk names as a material gap).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream/Tower/
HMI/Postkasse code exists here.

This module deliberately does NOT reimplement v0.1's envelope/receipt
logic -- it imports validate_envelope/ReceiptTrail/_base_msg from
signalvev_reference_v01_validation.py and tests how HUMAN_NOTICE
projection composes with the already-merged receipt trail (MESSAGE !=
AUTHORITY must hold for this message type too). It has no dependency on
v0.2, v0.3, or v0.5 -- see component.yaml explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.6/human-notice-projection-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))

from signalvev_reference_v01_validation import (  # noqa: E402
    validate_envelope,
    ReceiptTrail,
    _base_msg,
)


# ---------------------------------------------------------------------------
# 1. The projector (the "shared rule" X4's crosswalk names as missing)
# ---------------------------------------------------------------------------

class HumanNoticeError(ValueError):
    pass


NOTICE_KINDS = {"NOTICE", "GATE"}

# Machine states this projector knows how to classify. A state absent
# from this table is NOT an error -- it falls through to the fail-closed
# default (UNCERTAIN), which is exactly the point of HN4 below.
MACHINE_STATE_CLASS: dict[str, str] = {
    "HOLD": "UNCERTAIN",
    "UNKNOWN": "UNCERTAIN",
    "EFFECT_UNKNOWN": "UNCERTAIN",
    "NO_RESPONDER": "UNCERTAIN",
    "TIMEOUT_UNKNOWN": "UNCERTAIN",
    "EFFECT_ACCEPTED": "SUCCESS",
    "RELEASED": "SUCCESS",
    "NO_EFFECT": "SUCCESS",
}

# Substrings that must never appear in a projection of an UNCERTAIN
# state -- projecting uncertainty must never read as a fabricated
# success or approval.
FORBIDDEN_IN_UNCERTAIN_PROJECTION = ("APPROVED", "GRANTED", "SUCCESS", "COMPLETE", "OK")


class HumanNoticeProjector:
    """Reference implementation of the L7 projection rule X4's crosswalk
    names as a material gap: "No shared rule for projecting uncertain
    machine state without translating UNKNOWN into an apparent Human
    gate or success." Offline, in-process only -- no Tower/HMI/Postkasse
    wiring, no I/O, no network."""

    def project(self, machine_state: str, *, kind: str = "NOTICE") -> dict[str, Any]:
        if kind not in NOTICE_KINDS:
            raise HumanNoticeError(f"kind must be one of {sorted(NOTICE_KINDS)}, got {kind!r}")

        # Fail closed: an unrecognized state is treated as UNCERTAIN,
        # never defaulted to SUCCESS.
        state_class = MACHINE_STATE_CLASS.get(machine_state, "UNCERTAIN")
        label = f"{'CONFIRMED' if state_class == 'SUCCESS' else 'UNCERTAIN'}:{machine_state}"

        return {
            "kind": kind,
            "underlying_machine_state": machine_state,
            "state_class": state_class,
            "label": label,
            "authority": "NONE",  # MESSAGE != AUTHORITY holds for HUMAN_NOTICE too
        }


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers HN1-HN9
# ---------------------------------------------------------------------------

def test_hn0_golden_path_quarantine_projects_as_uncertain_notice() -> None:
    trail = ReceiptTrail("hn-golden-1", actor_id="actor-1", generation="gen-1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    trail.emit("WORK_CONSUMED")
    trail.emit("WORK_STARTED")
    trail.emit("EFFECT_UNKNOWN")  # quarantine: effect boundary may have been crossed, unconfirmed

    projector = HumanNoticeProjector()
    projection = projector.project(trail.stages_reached[-1])
    assert projection["state_class"] == "UNCERTAIN"
    assert projection["kind"] == "NOTICE"
    for forbidden in FORBIDDEN_IN_UNCERTAIN_PROJECTION:
        assert forbidden not in projection["label"]
    assert projection["authority"] == "NONE"


def test_hn1_uncertain_state_never_reads_as_success() -> None:
    projector = HumanNoticeProjector()
    for state in ("HOLD", "UNKNOWN", "EFFECT_UNKNOWN", "NO_RESPONDER", "TIMEOUT_UNKNOWN"):
        projection = projector.project(state)
        for forbidden in FORBIDDEN_IN_UNCERTAIN_PROJECTION:
            assert forbidden not in projection["label"], (
                f"projecting {state!r} must never read as {forbidden!r}"
            )
        assert projection["state_class"] == "UNCERTAIN"


def test_hn2_uncertainty_does_not_silently_become_a_gate() -> None:
    projector = HumanNoticeProjector()
    projection = projector.project("UNKNOWN")  # kind not specified -- defaults to NOTICE
    assert projection["kind"] == "NOTICE", (
        "uncertainty must never automatically escalate into a Human Gate; "
        "GATE requires an explicit, separate request (X4 material gap, second half)"
    )


def test_hn3_effect_unknown_distinct_from_effect_accepted() -> None:
    projector = HumanNoticeProjector()
    unknown = projector.project("EFFECT_UNKNOWN")
    accepted = projector.project("EFFECT_ACCEPTED")
    assert unknown["label"] != accepted["label"], (
        "a genuinely uncertain outcome must never be silently merged with a confirmed one"
    )
    assert unknown["state_class"] == "UNCERTAIN"
    assert accepted["state_class"] == "SUCCESS"


def test_hn4_unrecognized_state_defaults_uncertain_not_success() -> None:
    projector = HumanNoticeProjector()
    projection = projector.project("SOME_NEW_STATE_NOT_YET_KNOWN")
    assert projection["state_class"] == "UNCERTAIN", (
        "an unrecognized machine state must fail closed to UNCERTAIN, never default to SUCCESS"
    )


def test_hn5_human_notice_message_never_advances_a_receipt_trail() -> None:
    msg = _base_msg(message_type="HUMAN_NOTICE", subject="cerebro.v1.human.notice")
    validate_envelope(msg)  # schema-valid ...
    trail = ReceiptTrail("hn-attempt-1", actor_id="actor-1", generation="gen-1")
    assert trail.stages_reached == [], (
        "... but constructing/validating a HUMAN_NOTICE must never, by itself, "
        "advance a receipt trail (MESSAGE != AUTHORITY, specific to this message type)"
    )


def test_hn6_projection_never_carries_authority_even_if_envelope_does() -> None:
    # The envelope MAY carry an authority_ref pointing elsewhere in the
    # system -- but the human-facing projection itself must never claim
    # authority derived from that.
    msg = _base_msg(
        message_type="HUMAN_NOTICE", subject="cerebro.v1.human.notice",
        authority_class="some-authority-class", authority_ref="auth-ref-123",
    )
    validate_envelope(msg)
    projector = HumanNoticeProjector()
    projection = projector.project("HOLD")
    assert projection["authority"] == "NONE"


def test_hn7_confirmed_success_states_are_allowed_to_read_as_confirmed() -> None:
    # This is not a rule that nothing may ever read as success -- a
    # genuinely confirmed state must still be reportable as such.
    projector = HumanNoticeProjector()
    for state in ("EFFECT_ACCEPTED", "RELEASED", "NO_EFFECT"):
        projection = projector.project(state)
        assert projection["state_class"] == "SUCCESS"
        assert projection["label"].startswith("CONFIRMED:")


def test_hn8_explicit_gate_request_is_honored_and_still_non_authority() -> None:
    projector = HumanNoticeProjector()
    projection = projector.project("UNKNOWN", kind="GATE")
    assert projection["kind"] == "GATE", (
        "an explicit Human Gate request must be honored, not silently downgraded to NOTICE"
    )
    assert projection["authority"] == "NONE", (
        "even an explicit Human Gate request is a request for a Human decision, "
        "never authority granted by the projection itself"
    )


def test_hn9_invalid_kind_rejected() -> None:
    projector = HumanNoticeProjector()
    try:
        projector.project("UNKNOWN", kind="INVENTED_KIND")
    except HumanNoticeError:
        return
    raise AssertionError("an invalid notice kind must be rejected, not silently accepted")


HUMAN_NOTICE_PROJECTION_TESTS = [
    test_hn0_golden_path_quarantine_projects_as_uncertain_notice,
    test_hn1_uncertain_state_never_reads_as_success,
    test_hn2_uncertainty_does_not_silently_become_a_gate,
    test_hn3_effect_unknown_distinct_from_effect_accepted,
    test_hn4_unrecognized_state_defaults_uncertain_not_success,
    test_hn5_human_notice_message_never_advances_a_receipt_trail,
    test_hn6_projection_never_carries_authority_even_if_envelope_does,
    test_hn7_confirmed_success_states_are_allowed_to_read_as_confirmed,
    test_hn8_explicit_gate_request_is_honored_and_still_non_authority,
    test_hn9_invalid_kind_rejected,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in HUMAN_NOTICE_PROJECTION_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v06_validation selftest: "
          f"{len(HUMAN_NOTICE_PROJECTION_TESTS) - len(failures)}/{len(HUMAN_NOTICE_PROJECTION_TESTS)} PASS")
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
