#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.12 (presence envelope authority-
boundary guard: the PRESENCE / SERVICE DIRECTORY primitive the Human
text requires, where presence must never supply authority, claim,
work_started, consumed, or effect).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code,
and no presence/service-directory transport or scheduler, exists here.

This module has no dependency on any other signalvev-reference
candidate -- it is a pure, stateless structural guard over a
caller-supplied presence message. See component.yaml
explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.12/presence-envelope-authority-boundary-guard-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))


# ---------------------------------------------------------------------------
# 1. The presence envelope authority-boundary guard
# ---------------------------------------------------------------------------

# Per the Human text's own closed list.
PRESENCE_STATES = frozenset({"ONLINE", "OFFLINE", "READY", "ARMED", "DEGRADED", "UNKNOWN"})
PRESENCE_EFFECT_CLASS = "NONE"


class PresenceAuthorityViolation(ValueError):
    pass


class PresenceEnvelopeGuard:
    """Structural, stateless guard: rejects a PRESENCE message that
    tries to carry authority, claim, occupancy, work_started, consumed,
    or effect semantics. Never grants or tracks presence/occupancy
    itself -- only rejects violations."""

    def validate(
        self,
        *,
        presence_state: str,
        effect_class: str = "NONE",
        authority_ref: str | None = None,
        claims_occupancy: bool = False,
        claims_work_started: bool = False,
        claims_consumed: bool = False,
        claims_effect: bool = False,
    ) -> dict:
        if presence_state not in PRESENCE_STATES:
            raise PresenceAuthorityViolation(f"unrecognized presence_state: {presence_state!r}")

        violations: list[str] = []
        if effect_class != PRESENCE_EFFECT_CLASS:
            violations.append(f"effect_class must be NONE for a PRESENCE message, got {effect_class!r}")
        if authority_ref is not None:
            violations.append("a PRESENCE message must never carry a non-null authority_ref")
        if claims_occupancy:
            violations.append("presence must never supply occupancy/claim")
        if claims_work_started:
            violations.append("presence must never supply work_started")
        if claims_consumed:
            violations.append("presence must never supply consumed")
        if claims_effect:
            violations.append("presence must never supply effect")

        if violations:
            raise PresenceAuthorityViolation(
                f"presence_state={presence_state!r} rejected: " + "; ".join(violations)
            )

        return {"presence_state": presence_state, "validated": True, "authority_bearing": False}


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers PG1-PG8
# ---------------------------------------------------------------------------

def test_pg0_golden_path_clean_ready_presence_validates() -> None:
    guard = PresenceEnvelopeGuard()
    report = guard.validate(presence_state="READY")
    assert report["validated"] is True
    assert report["authority_bearing"] is False


def test_pg1_ready_presence_claiming_occupancy_is_rejected() -> None:
    # This is the core falsifier for READY: presence must never supply
    # command authority or occupancy.
    guard = PresenceEnvelopeGuard()
    try:
        guard.validate(presence_state="READY", claims_occupancy=True)
    except PresenceAuthorityViolation:
        return
    raise AssertionError("READY presence claiming occupancy must be rejected, never validated")


def test_pg2_armed_presence_claiming_occupancy_is_rejected() -> None:
    # The falsifier names ARMED specifically alongside READY -- no
    # exemption for it either.
    guard = PresenceEnvelopeGuard()
    try:
        guard.validate(presence_state="ARMED", claims_occupancy=True)
    except PresenceAuthorityViolation:
        return
    raise AssertionError("ARMED presence claiming occupancy must be rejected, never validated")


def test_pg3_nonnull_authority_ref_is_rejected_regardless_of_state() -> None:
    guard = PresenceEnvelopeGuard()
    for state in ("ONLINE", "READY", "ARMED"):
        try:
            guard.validate(presence_state=state, authority_ref="some-authority-token")
        except PresenceAuthorityViolation:
            continue
        raise AssertionError(f"presence_state={state} carrying an authority_ref must be rejected")


def test_pg4_nonzero_effect_class_is_rejected() -> None:
    guard = PresenceEnvelopeGuard()
    for effect_class in ("READ_ONLY", "WRITE_POSSIBLE", "UNKNOWN"):
        try:
            guard.validate(presence_state="ONLINE", effect_class=effect_class)
        except PresenceAuthorityViolation:
            continue
        raise AssertionError(f"a PRESENCE message with effect_class={effect_class} must be rejected")


def test_pg5_claims_work_started_is_rejected() -> None:
    guard = PresenceEnvelopeGuard()
    try:
        guard.validate(presence_state="ARMED", claims_work_started=True)
    except PresenceAuthorityViolation:
        return
    raise AssertionError("presence claiming work_started must be rejected")


def test_pg6_claims_consumed_is_rejected() -> None:
    guard = PresenceEnvelopeGuard()
    try:
        guard.validate(presence_state="READY", claims_consumed=True)
    except PresenceAuthorityViolation:
        return
    raise AssertionError("presence claiming consumed must be rejected")


def test_pg7_claims_effect_is_rejected() -> None:
    guard = PresenceEnvelopeGuard()
    try:
        guard.validate(presence_state="ONLINE", claims_effect=True)
    except PresenceAuthorityViolation:
        return
    raise AssertionError("presence claiming effect must be rejected")


def test_pg8_unrecognized_state_rejected_and_all_six_clean_states_pass() -> None:
    guard = PresenceEnvelopeGuard()
    try:
        guard.validate(presence_state="SUPER_READY")
    except PresenceAuthorityViolation:
        pass
    else:
        raise AssertionError("an unrecognized presence_state must raise, never be silently accepted")

    for state in PRESENCE_STATES:
        report = guard.validate(presence_state=state)
        assert report["validated"] is True, f"clean presence_state={state} must validate"


PRESENCE_ENVELOPE_GUARD_TESTS = [
    test_pg0_golden_path_clean_ready_presence_validates,
    test_pg1_ready_presence_claiming_occupancy_is_rejected,
    test_pg2_armed_presence_claiming_occupancy_is_rejected,
    test_pg3_nonnull_authority_ref_is_rejected_regardless_of_state,
    test_pg4_nonzero_effect_class_is_rejected,
    test_pg5_claims_work_started_is_rejected,
    test_pg6_claims_consumed_is_rejected,
    test_pg7_claims_effect_is_rejected,
    test_pg8_unrecognized_state_rejected_and_all_six_clean_states_pass,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in PRESENCE_ENVELOPE_GUARD_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v12_validation selftest: "
          f"{len(PRESENCE_ENVELOPE_GUARD_TESTS) - len(failures)}/"
          f"{len(PRESENCE_ENVELOPE_GUARD_TESTS)} PASS")
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
