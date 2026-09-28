#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.15 (schema/version compatibility
gate: the "Tredje bolge" / third-wave primitive the Human text's own
maturation ordering names, and the concrete `compatibility` policy
strings the Subject Registry already carries on every entry but that no
prior candidate ever dispatches on).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here.

This module **imports** (does not copy) `load_registry` from v0.1, used
read-only as a cross-version consistency canary (SC7) -- it does not
modify v0.1's registry file or import anything else from v0.1. It has
no dependency on v0.2 through v0.14.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.15/schema-version-compatibility-gate-candidate.yaml
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
# 1. The schema/version compatibility gate
# ---------------------------------------------------------------------------

class SchemaCompatibilityError(ValueError):
    pass


# The exact `compatibility` policy strings found verbatim on every entry
# of the real Subject Registry v0.1 draft. This candidate does not
# invent new policy names -- it implements dispatch for the ones the
# registry already declares but no prior candidate ever enforces.
VERSION_POLICIES = frozenset({
    "exact_schema_version_or_typed_hold",
    "version_negotiated_before_reply",
    "no_implicit_upgrade;unknown_version_holds",
    "content_type_and_schema_version_checked",
})
# Not a version-compatibility policy at all -- it governs RECEIPT STAGE
# compatibility (v0.1's own STAGE_PREDECESSORS / ReceiptTrail already
# owns that axis). Recognized here only so this gate can correctly say
# "not mine to adjudicate" instead of misapplying version logic to it.
STAGE_POLICY = "unknown_stage_holds"

_EXPECTED_KEYS = frozenset({"status", "reason"})
_VALID_STATUSES = frozenset({"ACCEPTED", "TYPED_HOLD", "NOT_APPLICABLE"})


def evaluate_schema_compatibility(
    *,
    compatibility_policy: str,
    declared_version: str,
    known_versions: frozenset[str],
    negotiated: bool = False,
    content_type_declared: bool = True,
) -> dict:
    """Pure, stateless schema/version compatibility gate.

    X4's own normative text: "Unknown subject/version/producer is a
    typed HOLD or quarantine, not an improvised fallback." This
    function implements that rule for the `version` axis specifically,
    dispatching on the registry's own `compatibility` policy string.
    An unknown version is always TYPED_HOLD regardless of policy --
    there is no version-ordering heuristic that treats a numerically
    "newer" unknown version as probably-fine ("no implicit upgrade").
    """
    if compatibility_policy == STAGE_POLICY:
        return {
            "status": "NOT_APPLICABLE",
            "reason": "STAGE_COMPATIBILITY_NOT_VERSION_COMPATIBILITY",
        }

    if compatibility_policy not in VERSION_POLICIES:
        raise SchemaCompatibilityError(
            f"unrecognized compatibility policy: {compatibility_policy!r}"
        )

    if declared_version not in known_versions:
        # No implicit upgrade: this is the ONLY branch that ever fires
        # for an unrecognized version string, whether it looks older,
        # equal-looking, or numerically "newer" than anything known.
        return {"status": "TYPED_HOLD", "reason": "UNKNOWN_VERSION"}

    if compatibility_policy == "version_negotiated_before_reply":
        if not negotiated:
            return {"status": "TYPED_HOLD", "reason": "VERSION_NOT_NEGOTIATED"}
        return {"status": "ACCEPTED", "reason": None}

    if compatibility_policy == "content_type_and_schema_version_checked":
        if not content_type_declared:
            return {"status": "TYPED_HOLD", "reason": "CONTENT_TYPE_NOT_DECLARED"}
        return {"status": "ACCEPTED", "reason": None}

    # exact_schema_version_or_typed_hold / no_implicit_upgrade;unknown_version_holds:
    # version is already confirmed known above, nothing further to check.
    return {"status": "ACCEPTED", "reason": None}


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers SC1-SC8
# ---------------------------------------------------------------------------

KNOWN = frozenset({"1.0.0-draft"})


def test_sc0_golden_path_known_version_exact_policy_is_accepted() -> None:
    report = evaluate_schema_compatibility(
        compatibility_policy="exact_schema_version_or_typed_hold",
        declared_version="1.0.0-draft",
        known_versions=KNOWN,
    )
    assert report["status"] == "ACCEPTED"
    assert report["reason"] is None


def test_sc1_unknown_version_is_typed_hold_not_improvised_fallback() -> None:
    # Direct proof of X4's own sentence: unknown version is a typed
    # HOLD, never an improvised fallback (e.g. silently treating it as
    # the closest known version).
    report = evaluate_schema_compatibility(
        compatibility_policy="exact_schema_version_or_typed_hold",
        declared_version="0.9.0-draft",
        known_versions=KNOWN,
    )
    assert report["status"] == "TYPED_HOLD"
    assert report["reason"] == "UNKNOWN_VERSION"


def test_sc2_numerically_newer_looking_version_is_not_implicitly_upgraded() -> None:
    # The registry's own words for work.command: "no_implicit_upgrade;
    # unknown_version_holds". A version string that looks newer must
    # not be treated as a probably-compatible upgrade -- it is just as
    # unknown as any other unrecognized string.
    report = evaluate_schema_compatibility(
        compatibility_policy="no_implicit_upgrade;unknown_version_holds",
        declared_version="2.0.0-draft",
        known_versions=KNOWN,
    )
    assert report["status"] == "TYPED_HOLD"
    assert report["reason"] == "UNKNOWN_VERSION"


def test_sc3_negotiated_before_reply_policy_holds_without_negotiation() -> None:
    # Even a known version is not enough for status.request/response --
    # X4's own registry names an explicit negotiation step.
    report = evaluate_schema_compatibility(
        compatibility_policy="version_negotiated_before_reply",
        declared_version="1.0.0-draft",
        known_versions=KNOWN,
        negotiated=False,
    )
    assert report["status"] == "TYPED_HOLD"
    assert report["reason"] == "VERSION_NOT_NEGOTIATED"


def test_sc4_negotiated_before_reply_policy_accepts_once_negotiated() -> None:
    report = evaluate_schema_compatibility(
        compatibility_policy="version_negotiated_before_reply",
        declared_version="1.0.0-draft",
        known_versions=KNOWN,
        negotiated=True,
    )
    assert report["status"] == "ACCEPTED"


def test_sc5_content_type_policy_holds_without_declared_content_type() -> None:
    # artifact.pointer's own policy: "content_type_and_schema_version_
    # checked" -- a matching version alone is not sufficient.
    report = evaluate_schema_compatibility(
        compatibility_policy="content_type_and_schema_version_checked",
        declared_version="1.0.0-draft",
        known_versions=KNOWN,
        content_type_declared=False,
    )
    assert report["status"] == "TYPED_HOLD"
    assert report["reason"] == "CONTENT_TYPE_NOT_DECLARED"


def test_sc6_stage_policy_is_not_applicable_not_misjudged_as_version() -> None:
    # lifecycle.receipt's own policy is "unknown_stage_holds" -- that
    # governs RECEIPT STAGE compatibility (v0.1's own STAGE_PREDECESSORS
    # / ReceiptTrail already owns that), not schema version. This gate
    # must recognize it as a different axis, not misapply version logic
    # (e.g. never silently ACCEPTED, never a version-flavored TYPED_HOLD).
    report = evaluate_schema_compatibility(
        compatibility_policy="unknown_stage_holds",
        declared_version="1.0.0-draft",
        known_versions=KNOWN,
    )
    assert report["status"] == "NOT_APPLICABLE"
    assert report["reason"] == "STAGE_COMPATIBILITY_NOT_VERSION_COMPATIBILITY"


def test_sc7_every_real_registry_entry_declares_a_policy_this_gate_recognizes() -> None:
    # Cross-version consistency canary: read (never modify) v0.1's own,
    # already-merged Subject Registry file, and prove every one of its
    # seven real entries' own `compatibility` string is something this
    # gate can dispatch on without raising -- ties this candidate to
    # v0.1's actual data instead of only synthetic policy strings.
    registry = load_registry()
    entries = registry["entries"]
    assert len(entries) == 7
    for entry in entries:
        policy = entry["compatibility"]
        # Must not raise SchemaCompatibilityError for any real entry.
        report = evaluate_schema_compatibility(
            compatibility_policy=policy,
            declared_version="1.0.0-draft",
            known_versions=KNOWN,
            negotiated=True,
            content_type_declared=True,
        )
        assert report["status"] in _VALID_STATUSES, (
            f"real registry entry {entry['subject']!r} with policy {policy!r} "
            f"produced an unexpected status {report['status']!r}"
        )


def test_sc8_unrecognized_policy_string_fails_closed_not_silently_accepted() -> None:
    try:
        evaluate_schema_compatibility(
            compatibility_policy="some_future_typo_or_unreleased_policy",
            declared_version="1.0.0-draft",
            known_versions=KNOWN,
        )
        raise AssertionError("expected SchemaCompatibilityError for unknown policy")
    except SchemaCompatibilityError:
        pass


SCHEMA_COMPATIBILITY_TESTS = [
    test_sc0_golden_path_known_version_exact_policy_is_accepted,
    test_sc1_unknown_version_is_typed_hold_not_improvised_fallback,
    test_sc2_numerically_newer_looking_version_is_not_implicitly_upgraded,
    test_sc3_negotiated_before_reply_policy_holds_without_negotiation,
    test_sc4_negotiated_before_reply_policy_accepts_once_negotiated,
    test_sc5_content_type_policy_holds_without_declared_content_type,
    test_sc6_stage_policy_is_not_applicable_not_misjudged_as_version,
    test_sc7_every_real_registry_entry_declares_a_policy_this_gate_recognizes,
    test_sc8_unrecognized_policy_string_fails_closed_not_silently_accepted,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in SCHEMA_COMPATIBILITY_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v15_validation selftest: "
          f"{len(SCHEMA_COMPATIBILITY_TESTS) - len(failures)}/"
          f"{len(SCHEMA_COMPATIBILITY_TESTS)} PASS")
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
