#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.16 (state delta truth-boundary guard:
the EVENT != STATE_TRUTH negative rule from the Human text's own
Section 5, which explicitly demands these be "testable invariants, not
just documentation" -- a rule no prior candidate has tested).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here.

This module **imports** (does not copy) `load_registry` from v0.1, used
read-only as a cross-version consistency canary (ET8) -- it does not
modify v0.1's registry file. It has no dependency on v0.2 through
v0.15.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.16/state-delta-truth-boundary-guard-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))

from signalvev_reference_v01_validation import load_registry  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The state delta truth-boundary guard
# ---------------------------------------------------------------------------

class StateTruthBoundaryError(ValueError):
    pass


_EXPECTED_KEYS = frozenset({"status", "reason", "truth"})
_VALID_STATUSES = frozenset({"TRUTH_ESTABLISHED", "DELTA_NOT_TRUTH"})


def resolve_state_delta_truth(
    *,
    message_type: str,
    declared_state_owner_ref: str,
    delta_payload: dict | None,
    reread: dict | None,
) -> dict:
    """Pure, stateless truth-boundary guard for STATE_DELTA messages.

    Human text, Communication classes v0.1: "STATE_DELTA -- Informerer
    om endring; state-owner er fortsatt sannhetskilden." Section 5's own
    negative law: "EVENT != STATE_TRUTH" -- "Dette skal vaere testbare
    invariants, ikke bare dokumentasjon."

    `delta_payload` is the notification's own carried content -- an
    assertion about a change, never truth on its own. `reread` is
    either None (no canonical reread performed) or a dict with
    `source_ref` and `content`, representing an independent read
    actually taken from the declared state owner. Truth is established
    only from `reread["content"]`, and only when `reread["source_ref"]`
    matches `declared_state_owner_ref` exactly -- `delta_payload` is
    never returned as truth, even when it happens to match the reread
    byte-for-byte.
    """
    if message_type != "STATE_DELTA":
        raise StateTruthBoundaryError(
            f"this guard only governs STATE_DELTA messages, got {message_type!r}"
        )
    if not declared_state_owner_ref:
        raise StateTruthBoundaryError("declared_state_owner_ref must be non-empty")

    if reread is None:
        return {"status": "DELTA_NOT_TRUTH", "reason": "NO_REREAD_PERFORMED", "truth": None}

    if not isinstance(reread, dict) or "source_ref" not in reread or "content" not in reread:
        raise StateTruthBoundaryError("reread must be a dict with source_ref and content")

    if reread["source_ref"] != declared_state_owner_ref:
        return {"status": "DELTA_NOT_TRUTH", "reason": "REREAD_SOURCE_MISMATCH", "truth": None}

    # Truth comes exclusively from the reread's own content -- the
    # delta_payload argument is deliberately never read again past this
    # point, even though it was accepted as a parameter.
    return {"status": "TRUTH_ESTABLISHED", "reason": None, "truth": reread["content"]}


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers ET1-ET8
# ---------------------------------------------------------------------------

OWNER = "state-owner-referent-C123"


def test_et0_golden_path_matching_reread_from_declared_owner_establishes_truth() -> None:
    content = {"revision": 7, "status": "READY"}
    report = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload=dict(content),
        reread={"source_ref": OWNER, "content": content},
    )
    assert report["status"] == "TRUTH_ESTABLISHED"
    assert report["truth"] == content


def test_et1_no_reread_at_all_is_delta_not_truth() -> None:
    # Direct proof of Section 5's own "EVENT != STATE_TRUTH": a delta
    # notification, with no independent reread, can never be truth.
    report = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload={"revision": 7, "status": "READY"},
        reread=None,
    )
    assert report["status"] == "DELTA_NOT_TRUTH"
    assert report["reason"] == "NO_REREAD_PERFORMED"
    assert report["truth"] is None


def test_et2_reread_from_wrong_source_is_delta_not_truth() -> None:
    report = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload={"revision": 7},
        reread={"source_ref": "some-OTHER-actor-not-the-owner", "content": {"revision": 7}},
    )
    assert report["status"] == "DELTA_NOT_TRUTH"
    assert report["reason"] == "REREAD_SOURCE_MISMATCH"


def test_et3_byte_identical_delta_payload_without_reread_is_still_not_truth() -> None:
    # Matching content alone never substitutes for the reread step --
    # the delta and the (missing) reread are not interchangeable just
    # because their content would have agreed.
    content = {"revision": 9, "status": "ARMED"}
    report = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload=dict(content),
        reread=None,
    )
    assert report["status"] == "DELTA_NOT_TRUTH"


def test_et4_stale_delta_payload_never_overrides_a_fresher_reread() -> None:
    # The state owner's fresh read always wins over the notification --
    # even though delta_payload disagrees with the reread, the function
    # returns the reread's content as truth, never the delta's.
    stale_delta = {"revision": 5, "status": "OFFLINE"}
    fresh_reread_content = {"revision": 9, "status": "ARMED"}
    report = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload=stale_delta,
        reread={"source_ref": OWNER, "content": fresh_reread_content},
    )
    assert report["status"] == "TRUTH_ESTABLISHED"
    assert report["truth"] == fresh_reread_content
    assert report["truth"] != stale_delta


def test_et5_wrong_message_type_fails_closed() -> None:
    # This guard is scoped to STATE_DELTA only -- it does not silently
    # process any other message class.
    try:
        resolve_state_delta_truth(
            message_type="EPHEMERAL_SIGNAL",
            declared_state_owner_ref=OWNER,
            delta_payload={"revision": 1},
            reread=None,
        )
        raise AssertionError("expected StateTruthBoundaryError for non-STATE_DELTA type")
    except StateTruthBoundaryError:
        pass


def test_et6_a_trusted_result_never_leaks_into_an_unrelated_later_call() -> None:
    content = {"revision": 1}
    trusted = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload=dict(content),
        reread={"source_ref": OWNER, "content": content},
    )
    later_without_reread = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload={"revision": 2},
        reread=None,
    )
    assert trusted["status"] == "TRUTH_ESTABLISHED"
    assert later_without_reread["status"] == "DELTA_NOT_TRUTH", (
        "an earlier TRUTH_ESTABLISHED result must never leak into an unrelated later call "
        "missing its own reread"
    )


def test_et7_output_shape_and_status_field_coupling_is_consistent() -> None:
    content = {"revision": 3}
    ok = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload=dict(content),
        reread={"source_ref": OWNER, "content": content},
    )
    missing = resolve_state_delta_truth(
        message_type="STATE_DELTA",
        declared_state_owner_ref=OWNER,
        delta_payload=dict(content),
        reread=None,
    )
    for report in (ok, missing):
        assert set(report.keys()) == _EXPECTED_KEYS
        assert report["status"] in _VALID_STATUSES
    assert (ok["status"] == "TRUTH_ESTABLISHED") == (ok["truth"] is not None and ok["reason"] is None)
    assert (missing["status"] == "DELTA_NOT_TRUTH") == (missing["truth"] is None and missing["reason"] is not None)


def test_et8_real_registry_entry_names_exactly_this_requirement() -> None:
    # Cross-version consistency canary: v0.1's own, already-merged
    # Subject Registry entry for cerebro.v1.state.delta names the exact
    # requirement this candidate implements -- read only, never modified.
    registry = load_registry()
    entries = {e["subject"]: e for e in registry["entries"]}
    state_delta_entry = entries["cerebro.v1.state.delta"]
    assert state_delta_entry["authority_requirement"] == (
        "source_attribution_and_canonical_state_reread"
    ), (
        "this candidate's design must match the registry's own already-declared "
        "requirement for cerebro.v1.state.delta, not an invented one"
    )


STATE_DELTA_TRUTH_TESTS = [
    test_et0_golden_path_matching_reread_from_declared_owner_establishes_truth,
    test_et1_no_reread_at_all_is_delta_not_truth,
    test_et2_reread_from_wrong_source_is_delta_not_truth,
    test_et3_byte_identical_delta_payload_without_reread_is_still_not_truth,
    test_et4_stale_delta_payload_never_overrides_a_fresher_reread,
    test_et5_wrong_message_type_fails_closed,
    test_et6_a_trusted_result_never_leaks_into_an_unrelated_later_call,
    test_et7_output_shape_and_status_field_coupling_is_consistent,
    test_et8_real_registry_entry_names_exactly_this_requirement,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in STATE_DELTA_TRUTH_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v16_validation selftest: "
          f"{len(STATE_DELTA_TRUTH_TESTS) - len(failures)}/"
          f"{len(STATE_DELTA_TRUTH_TESTS)} PASS")
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
