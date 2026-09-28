#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.17 (readiness non-conflation guard:
two more of the ten Section 5 negative laws no prior candidate has
tested -- "ONLINE != READY" and "TRANSPORT_AVAILABLE != WORK_ALLOWED").

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here.

This module has **no dependency on any other signalvev-reference
candidate's code** -- it is a pure, stateless pair of classifiers. It
is distinct from v0.12's PresenceEnvelopeGuard: v0.12 proves presence
states never carry authority/occupancy/effect *claims*; this candidate
proves a *caller* must not conflate a weaker presence/connectivity
signal with the stronger "candidate for work" signal in the first
place -- a different failure mode (state confusion, not claim
injection), and composes with v0.12 rather than duplicating it.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.17/readiness-non-conflation-guard-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))


# ---------------------------------------------------------------------------
# 1. The readiness non-conflation guard
# ---------------------------------------------------------------------------

class ReadinessConflationError(ValueError):
    pass


# The six presence states, verbatim from the Human text's own PRESENCE
# / SERVICE DIRECTORY section. Only READY and ARMED are ever candidates
# for work consideration -- the same pair falsifier 6 names together
# ("Presence READY or ARMED supplies command authority or occupancy").
PRESENCE_STATES = frozenset({"ONLINE", "OFFLINE", "READY", "ARMED", "DEGRADED", "UNKNOWN"})
WORK_CANDIDATE_STATES = frozenset({"READY", "ARMED"})

_PRESENCE_KEYS = frozenset({"work_candidate", "reason"})
_TRANSPORT_KEYS = frozenset({"work_allowed", "reason"})


def presence_indicates_work_candidacy(presence_state: str) -> dict:
    """Section 5: "ONLINE != READY". Being merely reachable (ONLINE) --
    or OFFLINE, DEGRADED, or UNKNOWN -- must never be conflated with
    being a candidate for work consideration. Only READY and ARMED
    qualify, and even then this is a pre-filter fact only, never an
    authority grant (that boundary stays v0.12's own, separate job)."""
    if presence_state not in PRESENCE_STATES:
        raise ReadinessConflationError(f"unrecognized presence_state: {presence_state!r}")

    if presence_state in WORK_CANDIDATE_STATES:
        return {"work_candidate": True, "reason": None}
    return {"work_candidate": False, "reason": f"{presence_state}_IS_NOT_A_WORK_CANDIDATE_STATE"}


def transport_availability_allows_work(transport_available: bool) -> dict:
    """Section 5: "TRANSPORT_AVAILABLE != WORK_ALLOWED". Whether bytes
    can currently move between endpoints (L0) is a fact about the pipe,
    never a fact about whether work is admitted (L4). This function
    always reports work_allowed=False for any transport_available value
    -- there is no transport-layer signal, including "available", that
    this module treats as sufficient on its own."""
    if not isinstance(transport_available, bool):
        raise ReadinessConflationError("transport_available must be a bool")

    reason = "TRANSPORT_AVAILABILITY_NEVER_GRANTS_WORK_ADMISSION"
    return {"work_allowed": False, "reason": reason}


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers RC1-RC8
# ---------------------------------------------------------------------------

def test_rc0_golden_path_ready_is_a_work_candidate_state() -> None:
    report = presence_indicates_work_candidacy("READY")
    assert report["work_candidate"] is True
    assert report["reason"] is None


def test_rc1_online_is_not_a_work_candidate_state() -> None:
    # Direct proof of Section 5's own "ONLINE != READY": merely being
    # reachable must never be conflated with being ready for work.
    report = presence_indicates_work_candidacy("ONLINE")
    assert report["work_candidate"] is False
    assert report["reason"] == "ONLINE_IS_NOT_A_WORK_CANDIDATE_STATE"


def test_rc2_armed_is_also_a_work_candidate_state() -> None:
    # Falsifier 6 names READY and ARMED together -- both qualify as
    # candidates, not just READY.
    report = presence_indicates_work_candidacy("ARMED")
    assert report["work_candidate"] is True


def test_rc3_offline_degraded_unknown_are_all_not_work_candidates() -> None:
    for state in ("OFFLINE", "DEGRADED", "UNKNOWN"):
        report = presence_indicates_work_candidacy(state)
        assert report["work_candidate"] is False, (
            f"{state} must never be treated as a work-candidate presence state"
        )


def test_rc4_unrecognized_presence_state_fails_closed() -> None:
    try:
        presence_indicates_work_candidacy("SUPER_READY")
        raise AssertionError("expected ReadinessConflationError for unrecognized state")
    except ReadinessConflationError:
        pass


def test_rc5_transport_available_true_never_allows_work() -> None:
    # Direct proof of Section 5's own "TRANSPORT_AVAILABLE !=
    # WORK_ALLOWED" -- even a positive, connected transport signal
    # never establishes work admission.
    report = transport_availability_allows_work(True)
    assert report["work_allowed"] is False
    assert report["reason"] == "TRANSPORT_AVAILABILITY_NEVER_GRANTS_WORK_ADMISSION"


def test_rc6_transport_available_false_also_never_allows_work() -> None:
    # No special-casing: neither value of transport_available ever
    # flips this to True.
    report = transport_availability_allows_work(False)
    assert report["work_allowed"] is False


def test_rc7_non_bool_transport_value_fails_closed() -> None:
    try:
        transport_availability_allows_work("yes")  # type: ignore[arg-type]
        raise AssertionError("expected ReadinessConflationError for non-bool input")
    except ReadinessConflationError:
        pass


def test_rc8_positive_looking_inputs_never_combine_into_a_derived_authority_field() -> None:
    # Even when presence looks like a strong candidate (READY) and
    # transport looks available (True), this module exposes no combined
    # "ready_to_work" or authority-flavored field anywhere -- the two
    # facts stay separate, and neither report's shape ever grows an
    # extra key under favorable-looking inputs.
    presence_report = presence_indicates_work_candidacy("READY")
    transport_report = transport_availability_allows_work(True)

    assert set(presence_report.keys()) == _PRESENCE_KEYS
    assert set(transport_report.keys()) == _TRANSPORT_KEYS
    assert "authority" not in presence_report and "authority" not in transport_report
    assert "work_allowed" not in presence_report
    assert "work_candidate" not in transport_report


READINESS_NON_CONFLATION_TESTS = [
    test_rc0_golden_path_ready_is_a_work_candidate_state,
    test_rc1_online_is_not_a_work_candidate_state,
    test_rc2_armed_is_also_a_work_candidate_state,
    test_rc3_offline_degraded_unknown_are_all_not_work_candidates,
    test_rc4_unrecognized_presence_state_fails_closed,
    test_rc5_transport_available_true_never_allows_work,
    test_rc6_transport_available_false_also_never_allows_work,
    test_rc7_non_bool_transport_value_fails_closed,
    test_rc8_positive_looking_inputs_never_combine_into_a_derived_authority_field,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in READINESS_NON_CONFLATION_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v17_validation selftest: "
          f"{len(READINESS_NON_CONFLATION_TESTS) - len(failures)}/"
          f"{len(READINESS_NON_CONFLATION_TESTS)} PASS")
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
