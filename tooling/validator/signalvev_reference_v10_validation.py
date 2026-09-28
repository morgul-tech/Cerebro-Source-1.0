#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.10 (reply currentness and
effectful-timeout guard: the request/reply primitive X4 runtime-fagpass's
own crosswalk names as a material L3 gap).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code,
and no request/reply transport implementation, exists here.

This module has no dependency on any other signalvev-reference
candidate -- it is a pure, stateless classifier over caller-supplied
inputs. See component.yaml explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.10/reply-currentness-effectful-timeout-guard-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))


# ---------------------------------------------------------------------------
# 1. The reply currentness / effectful-timeout guard
# ---------------------------------------------------------------------------

REPLY_OUTCOMES = frozenset({"RESPONSE", "NO_RESPONDER", "TIMEOUT_UNKNOWN"})

# Per CEREBRO_MESSAGE_V1's own effect_class enum.
EFFECT_CLASSES = frozenset({"NONE", "READ_ONLY", "WRITE_POSSIBLE", "UNKNOWN"})
SAFE_RETRY_EFFECT_CLASSES = frozenset({"NONE", "READ_ONLY"})


class ReplyCurrentnessError(ValueError):
    pass


def evaluate_reply(
    outcome: str,
    *,
    response_revision: int | None = None,
    current_revision: int | None = None,
    effect_class: str = "READ_ONLY",
) -> dict:
    """Pure, stateless classification of one reply. Never caches or
    holds state across calls -- every call is judged only on the inputs
    given to it."""
    if outcome not in REPLY_OUTCOMES:
        raise ReplyCurrentnessError(f"unrecognized reply outcome: {outcome!r}")
    if effect_class not in EFFECT_CLASSES:
        raise ReplyCurrentnessError(f"unrecognized effect_class: {effect_class!r}")

    if outcome == "NO_RESPONDER":
        return {
            "status": "UNKNOWN",
            "reason": "NO_RESPONDER",
            "safe_to_retry_as_read_only": True,
        }

    if outcome == "TIMEOUT_UNKNOWN":
        safe_retry = effect_class in SAFE_RETRY_EFFECT_CLASSES
        return {
            "status": "UNKNOWN",
            "reason": "TIMEOUT_UNKNOWN",
            "safe_to_retry_as_read_only": safe_retry,
        }

    # outcome == "RESPONSE"
    if response_revision is None:
        raise ReplyCurrentnessError(
            "a RESPONSE outcome must carry a response_revision -- it must never "
            "be treated as current by default"
        )
    if current_revision is None:
        raise ReplyCurrentnessError(
            "evaluating a RESPONSE requires the referent's current_revision"
        )

    if response_revision != current_revision:
        return {
            "status": "STALE",
            "reason": "RESPONSE_REVISION_NOT_CURRENT",
            "safe_to_retry_as_read_only": True,
        }
    return {
        "status": "CURRENT",
        "reason": None,
        "safe_to_retry_as_read_only": True,
    }


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers SR1-SR8
# ---------------------------------------------------------------------------

def test_sr0_golden_path_matching_revision_is_current() -> None:
    report = evaluate_reply("RESPONSE", response_revision=5, current_revision=5)
    assert report["status"] == "CURRENT"
    assert report["reason"] is None


def test_sr1_mismatched_response_revision_is_stale_never_canonical() -> None:
    # This is the core falsifier: a reply lacking current referent/version
    # must never be treated as canonical status.
    report = evaluate_reply("RESPONSE", response_revision=3, current_revision=5)
    assert report["status"] == "STALE", (
        "a reply carrying a non-current revision must never be treated as canonical status"
    )
    assert report["status"] != "CURRENT"


def test_sr2_no_responder_is_unknown_never_canonical() -> None:
    report = evaluate_reply("NO_RESPONDER")
    assert report["status"] == "UNKNOWN"
    assert report["status"] != "CURRENT"


def test_sr3_timeout_unknown_read_only_request_is_safe_to_retry() -> None:
    report = evaluate_reply("TIMEOUT_UNKNOWN", effect_class="READ_ONLY")
    assert report["status"] == "UNKNOWN"
    assert report["safe_to_retry_as_read_only"] is True


def test_sr4_timeout_unknown_write_possible_request_blocks_retry() -> None:
    # X4's own text: "A timed-out request that could have crossed an
    # effect boundary cannot be retried as if it were a read-only question."
    report = evaluate_reply("TIMEOUT_UNKNOWN", effect_class="WRITE_POSSIBLE")
    assert report["status"] == "UNKNOWN"
    assert report["safe_to_retry_as_read_only"] is False, (
        "a timed-out request that may have crossed an effect boundary must never "
        "be treated as safely retryable as a read-only question"
    )


def test_sr5_timeout_unknown_unknown_effect_class_fails_closed() -> None:
    # An UNKNOWN effect_class must fail closed the same way WRITE_POSSIBLE
    # does -- absence of proof that it was read-only is not proof it was.
    report = evaluate_reply("TIMEOUT_UNKNOWN", effect_class="UNKNOWN")
    assert report["safe_to_retry_as_read_only"] is False


def test_sr6_response_missing_revision_raises_never_defaults_to_current() -> None:
    try:
        evaluate_reply("RESPONSE", current_revision=5)
    except ReplyCurrentnessError:
        return
    raise AssertionError(
        "a RESPONSE with no response_revision must raise, never be treated as current by default"
    )


def test_sr7_unrecognized_outcome_raises() -> None:
    try:
        evaluate_reply("MAYBE_A_RESPONSE")
    except ReplyCurrentnessError:
        return
    raise AssertionError("an unrecognized reply outcome must raise, never be silently classified")


def test_sr8_pure_function_no_hidden_state_and_invalid_effect_class_raises() -> None:
    # Same referent, two independent calls with different current_revision
    # values must not interfere with each other -- there is no cache.
    first = evaluate_reply("RESPONSE", response_revision=5, current_revision=5)
    second = evaluate_reply("RESPONSE", response_revision=5, current_revision=9)
    assert first["status"] == "CURRENT"
    assert second["status"] == "STALE", (
        "evaluate_reply must be a pure function of its inputs -- no hidden "
        "state may carry a prior call's currentness into a later one"
    )

    try:
        evaluate_reply("RESPONSE", response_revision=1, current_revision=1, effect_class="NOT_A_REAL_CLASS")
    except ReplyCurrentnessError:
        return
    raise AssertionError("an unrecognized effect_class must raise, never be silently accepted")


REPLY_CURRENTNESS_GUARD_TESTS = [
    test_sr0_golden_path_matching_revision_is_current,
    test_sr1_mismatched_response_revision_is_stale_never_canonical,
    test_sr2_no_responder_is_unknown_never_canonical,
    test_sr3_timeout_unknown_read_only_request_is_safe_to_retry,
    test_sr4_timeout_unknown_write_possible_request_blocks_retry,
    test_sr5_timeout_unknown_unknown_effect_class_fails_closed,
    test_sr6_response_missing_revision_raises_never_defaults_to_current,
    test_sr7_unrecognized_outcome_raises,
    test_sr8_pure_function_no_hidden_state_and_invalid_effect_class_raises,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in REPLY_CURRENTNESS_GUARD_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v10_validation selftest: "
          f"{len(REPLY_CURRENTNESS_GUARD_TESTS) - len(failures)}/"
          f"{len(REPLY_CURRENTNESS_GUARD_TESTS)} PASS")
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
