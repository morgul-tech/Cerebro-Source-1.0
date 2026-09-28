#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.11 (effect quarantine classifier: the
QUARANTINE + IDEMPOTENCY primitive the Human text requires and X4
runtime-fagpass's own Receipts paragraph restates as a normative rule).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code,
and no replay execution, exists here.

This module has no dependency on any other signalvev-reference
candidate -- it is a pure, stateless classifier over caller-supplied
inputs. See component.yaml explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.11/effect-quarantine-classifier-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))


# ---------------------------------------------------------------------------
# 1. The effect quarantine classifier
# ---------------------------------------------------------------------------

class EffectQuarantineError(ValueError):
    pass


_EXPECTED_KEYS = frozenset({"status", "quarantined", "replay_may_be_safe"})


def classify_timeout(
    *,
    has_independent_pre_effect_proof: bool,
    effect_boundary_possibly_crossed: bool,
) -> dict:
    """Pure, stateless classification of one timeout/missing-receipt
    situation on a potentially-effectful command. Never caches or holds
    state across calls -- every call is judged only on the inputs given
    to it, and never labels NO_EFFECT without independent proof."""
    if not isinstance(has_independent_pre_effect_proof, bool):
        raise EffectQuarantineError("has_independent_pre_effect_proof must be a bool")
    if not isinstance(effect_boundary_possibly_crossed, bool):
        raise EffectQuarantineError("effect_boundary_possibly_crossed must be a bool")

    if has_independent_pre_effect_proof and effect_boundary_possibly_crossed:
        raise EffectQuarantineError(
            "contradiction: independent pre-effect proof cannot coexist with "
            "'the boundary was possibly crossed' -- this is not a valid classifiable state"
        )

    if has_independent_pre_effect_proof:
        # boundary_possibly_crossed is necessarily False here (contradiction above)
        return {
            "status": "NO_EFFECT",
            "quarantined": False,
            "replay_may_be_safe": True,
        }

    # No independent proof -- fail closed to UNKNOWN + quarantine regardless
    # of what the caller believes about the boundary.
    return {
        "status": "UNKNOWN_QUARANTINED",
        "quarantined": True,
        "replay_may_be_safe": False,
    }


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers EQ1-EQ8
# ---------------------------------------------------------------------------

def test_eq0_golden_path_independent_proof_is_no_effect_not_quarantined() -> None:
    report = classify_timeout(
        has_independent_pre_effect_proof=True,
        effect_boundary_possibly_crossed=False,
    )
    assert report["status"] == "NO_EFFECT"
    assert report["quarantined"] is False
    assert report["replay_may_be_safe"] is True


def test_eq1_timeout_without_proof_never_labeled_no_effect() -> None:
    # This is the core falsifier: a timeout after possible effect must
    # never be labeled NO_EFFECT without independent proof.
    report = classify_timeout(
        has_independent_pre_effect_proof=False,
        effect_boundary_possibly_crossed=True,
    )
    assert report["status"] != "NO_EFFECT", (
        "a timeout after possible effect must never be labeled NO_EFFECT without independent proof"
    )
    assert report["status"] == "UNKNOWN_QUARANTINED"


def test_eq2_timeout_without_proof_never_allows_replay() -> None:
    report = classify_timeout(
        has_independent_pre_effect_proof=False,
        effect_boundary_possibly_crossed=True,
    )
    assert report["replay_may_be_safe"] is False, (
        "an UNKNOWN_QUARANTINED classification must never carry replay_may_be_safe = true"
    )


def test_eq3_unverified_caller_claim_is_not_a_substitute_for_proof() -> None:
    # The caller believes the boundary was NOT crossed, but supplies no
    # independent proof -- the system must not simply trust the claim.
    report = classify_timeout(
        has_independent_pre_effect_proof=False,
        effect_boundary_possibly_crossed=False,
    )
    assert report["status"] == "UNKNOWN_QUARANTINED", (
        "an unverified caller belief that the boundary was not crossed is not a "
        "substitute for independent pre-effect proof"
    )
    assert report["quarantined"] is True


def test_eq4_quarantined_outcome_never_pairs_with_replay_allowed() -> None:
    for boundary_flag in (True, False):
        report = classify_timeout(
            has_independent_pre_effect_proof=False,
            effect_boundary_possibly_crossed=boundary_flag,
        )
        assert report["quarantined"] is True
        assert report["replay_may_be_safe"] is False


def test_eq5_contradictory_inputs_raise() -> None:
    try:
        classify_timeout(
            has_independent_pre_effect_proof=True,
            effect_boundary_possibly_crossed=True,
        )
    except EffectQuarantineError:
        return
    raise AssertionError(
        "independent pre-effect proof together with 'boundary possibly crossed' "
        "is a contradiction and must raise, never silently resolve"
    )


def test_eq6_pure_function_no_hidden_state_across_calls() -> None:
    first = classify_timeout(has_independent_pre_effect_proof=True, effect_boundary_possibly_crossed=False)
    second = classify_timeout(has_independent_pre_effect_proof=False, effect_boundary_possibly_crossed=True)
    third = classify_timeout(has_independent_pre_effect_proof=True, effect_boundary_possibly_crossed=False)
    assert first["status"] == "NO_EFFECT"
    assert second["status"] == "UNKNOWN_QUARANTINED", (
        "a prior NO_EFFECT classification must never leak into a later independent call"
    )
    assert third["status"] == "NO_EFFECT" and third == first


def test_eq7_output_shape_carries_no_extra_authority_bearing_fields() -> None:
    # Architectural discipline check: this classifier must not sneak in
    # any field that would look like it grants execution authority
    # (e.g. an "authorize_new_attempt" key) -- only the three documented
    # advisory fields.
    report = classify_timeout(has_independent_pre_effect_proof=True, effect_boundary_possibly_crossed=False)
    assert set(report.keys()) == _EXPECTED_KEYS, (
        f"unexpected keys in classification output: {set(report.keys()) - _EXPECTED_KEYS}"
    )


def test_eq8_invalid_input_types_raise() -> None:
    try:
        classify_timeout(has_independent_pre_effect_proof="yes", effect_boundary_possibly_crossed=False)
    except EffectQuarantineError:
        pass
    else:
        raise AssertionError("a non-bool has_independent_pre_effect_proof must raise")

    try:
        classify_timeout(has_independent_pre_effect_proof=False, effect_boundary_possibly_crossed=1)
    except EffectQuarantineError:
        return
    raise AssertionError("a non-bool effect_boundary_possibly_crossed must raise")


EFFECT_QUARANTINE_CLASSIFIER_TESTS = [
    test_eq0_golden_path_independent_proof_is_no_effect_not_quarantined,
    test_eq1_timeout_without_proof_never_labeled_no_effect,
    test_eq2_timeout_without_proof_never_allows_replay,
    test_eq3_unverified_caller_claim_is_not_a_substitute_for_proof,
    test_eq4_quarantined_outcome_never_pairs_with_replay_allowed,
    test_eq5_contradictory_inputs_raise,
    test_eq6_pure_function_no_hidden_state_across_calls,
    test_eq7_output_shape_carries_no_extra_authority_bearing_fields,
    test_eq8_invalid_input_types_raise,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in EFFECT_QUARANTINE_CLASSIFIER_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v11_validation selftest: "
          f"{len(EFFECT_QUARANTINE_CLASSIFIER_TESTS) - len(failures)}/"
          f"{len(EFFECT_QUARANTINE_CLASSIFIER_TESTS)} PASS")
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
