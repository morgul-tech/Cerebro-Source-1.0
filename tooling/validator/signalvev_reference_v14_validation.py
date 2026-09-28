#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.14 (workload identity attestation
binder: the L1 IDENTITY primitive X4 runtime-fagpass's own crosswalk
names as a material gap -- second/third-wave scope, not one of the
original 12 numbered falsifiers).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here.

This module has no dependency on any other signalvev-reference
candidate's code -- it is a pure, stateless binder over two caller
-supplied dicts (a claimed source and an independently-obtained
attestation record). See component.yaml explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.14/workload-identity-attestation-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))


# ---------------------------------------------------------------------------
# 1. The workload identity attestation binder
# ---------------------------------------------------------------------------

class WorkloadAttestationError(ValueError):
    pass


# The six source-envelope identity fields X4's own text names as needing
# a trusted binding to "current machine/session/custody" -- everything
# in CEREBRO_MESSAGE_V1's `source` object except `principal_ref`, which
# v0.1's validate_envelope already checks is present as the top-level
# claim (this candidate does not re-check or duplicate that).
ATTESTABLE_FIELDS = (
    "actor_id",
    "generation",
    "workload_ref",
    "machine_ref",
    "session_ref",
    "custody_ref",
)

_EXPECTED_KEYS = frozenset({"status", "mismatched_fields", "bound_fields"})
_VALID_STATUSES = frozenset({"TRUSTED_BOUND", "ASSERTED_ONLY", "MISMATCH"})


def bind_source_identity(*, claimed_source: dict, attestation: dict | None) -> dict:
    """Pure, stateless workload-identity binder.

    `claimed_source` is the envelope's own `source` object -- asserted
    metadata a sender wrote about itself. `attestation` is a record from
    a *separate* trusted identity/authority check (X4's own phrasing);
    None means no such check was performed for this call.

    Never treats a claim as trusted on its own. Returns TRUSTED_BOUND
    only when an attestation record exists and every attestable field
    matches exactly; ASSERTED_ONLY when no attestation was supplied at
    all (the claim stands alone, unverified); MISMATCH when an
    attestation exists but disagrees with the claim on one or more
    fields -- every mismatched field is reported, not just the first.
    """
    if not isinstance(claimed_source, dict):
        raise WorkloadAttestationError("claimed_source must be an object")
    missing = [f for f in ATTESTABLE_FIELDS if f not in claimed_source]
    if missing:
        raise WorkloadAttestationError(f"claimed_source missing fields: {missing}")

    if attestation is None:
        return {
            "status": "ASSERTED_ONLY",
            "mismatched_fields": (),
            "bound_fields": (),
        }

    if not isinstance(attestation, dict):
        raise WorkloadAttestationError("attestation must be an object or None")
    missing_att = [f for f in ATTESTABLE_FIELDS if f not in attestation]
    if missing_att:
        raise WorkloadAttestationError(f"attestation missing fields: {missing_att}")

    mismatched: list[str] = []
    bound: list[str] = []
    for field in ATTESTABLE_FIELDS:
        claimed_val = claimed_source[field]
        attested_val = attestation[field]
        # A None claim is not a verifiable assertion -- it can never be
        # treated as bound, even if the attestation itself holds a
        # concrete value. Binding requires the claim to be present AND
        # correct, not merely absent-and-therefore-unfalsifiable.
        if claimed_val is None or claimed_val != attested_val:
            mismatched.append(field)
        else:
            bound.append(field)

    if mismatched:
        return {
            "status": "MISMATCH",
            "mismatched_fields": tuple(mismatched),
            "bound_fields": tuple(bound),
        }

    return {
        "status": "TRUSTED_BOUND",
        "mismatched_fields": (),
        "bound_fields": tuple(bound),
    }


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers WI1-WI8
# ---------------------------------------------------------------------------

def _identity(**overrides) -> dict:
    base = {
        "principal_ref": "principal-cerebro-p21",
        "actor_id": "actor-claude-session-9f",
        "generation": "gen-2026-09-29-a",
        "workload_ref": "workload-signalvev-candidate-builder",
        "machine_ref": "machine-cloud-container-a1",
        "session_ref": "session-dc1cbbc3",
        "custody_ref": "custody-andreas-morgul-tech",
    }
    base.update(overrides)
    return base


def test_wi0_golden_path_matching_attestation_is_trusted_bound() -> None:
    claimed = _identity()
    attested = {k: claimed[k] for k in ATTESTABLE_FIELDS}
    report = bind_source_identity(claimed_source=claimed, attestation=attested)
    assert report["status"] == "TRUSTED_BOUND"
    assert report["mismatched_fields"] == ()
    assert set(report["bound_fields"]) == set(ATTESTABLE_FIELDS)


def test_wi1_no_attestation_is_asserted_only_never_trusted() -> None:
    # X4's own text: "source alone must remain a claim." An envelope's
    # own source fields, with no independent check performed, must never
    # be reported as bound.
    claimed = _identity()
    report = bind_source_identity(claimed_source=claimed, attestation=None)
    assert report["status"] == "ASSERTED_ONLY", (
        "an unattested claim must never be reported as TRUSTED_BOUND"
    )
    assert report["bound_fields"] == ()


def test_wi2_single_field_mismatch_is_reported_by_name() -> None:
    claimed = _identity(actor_id="actor-claude-session-9f")
    attested = {k: claimed[k] for k in ATTESTABLE_FIELDS}
    attested["actor_id"] = "actor-DIFFERENT-session-00"
    report = bind_source_identity(claimed_source=claimed, attestation=attested)
    assert report["status"] == "MISMATCH"
    assert report["mismatched_fields"] == ("actor_id",)


def test_wi3_custody_mismatch_alone_blocks_full_binding() -> None:
    # The crosswalk's own named triple is "machine/session/custody" --
    # custody drift (someone else now holds the item) must be caught
    # even when every other field still matches.
    claimed = _identity()
    attested = {k: claimed[k] for k in ATTESTABLE_FIELDS}
    attested["custody_ref"] = "custody-SOMEONE-ELSE-now"
    report = bind_source_identity(claimed_source=claimed, attestation=attested)
    assert report["status"] == "MISMATCH"
    assert "custody_ref" in report["mismatched_fields"]
    assert "custody_ref" not in report["bound_fields"]


def test_wi4_multiple_simultaneous_mismatches_are_all_reported() -> None:
    claimed = _identity()
    attested = {k: claimed[k] for k in ATTESTABLE_FIELDS}
    attested["generation"] = "gen-STALE"
    attested["machine_ref"] = "machine-STALE"
    report = bind_source_identity(claimed_source=claimed, attestation=attested)
    assert report["status"] == "MISMATCH"
    assert set(report["mismatched_fields"]) == {"generation", "machine_ref"}, (
        "every mismatched field must be reported, not just the first"
    )
    assert set(report["bound_fields"]) == set(ATTESTABLE_FIELDS) - {"generation", "machine_ref"}


def test_wi5_null_claimed_field_never_counts_as_bound_even_if_attestation_agrees() -> None:
    # A claim of None is an absent assertion, not a falsifiable one --
    # it can never be treated as verified, even against an attestation
    # record that happens to hold a value for that field.
    claimed = _identity(session_ref=None)
    attested = {k: claimed[k] for k in ATTESTABLE_FIELDS}
    attested["session_ref"] = "session-dc1cbbc3"  # attestation has a real value
    report = bind_source_identity(claimed_source=claimed, attestation=attested)
    assert report["status"] == "MISMATCH"
    assert "session_ref" in report["mismatched_fields"]


def test_wi6_principal_ref_is_out_of_scope_for_this_binder() -> None:
    # v0.1's validate_envelope already requires principal_ref to be a
    # non-empty string; this candidate does not duplicate that check or
    # let a principal_ref difference affect its own six-field binding
    # decision -- it only ever reads the six ATTESTABLE_FIELDS.
    claimed = _identity(principal_ref="principal-A")
    attested = {k: claimed[k] for k in ATTESTABLE_FIELDS}
    # attestation dict deliberately carries no principal_ref key at all --
    # if the function looked at it, this would KeyError.
    assert "principal_ref" not in attested
    report = bind_source_identity(claimed_source=claimed, attestation=attested)
    assert report["status"] == "TRUSTED_BOUND"


def test_wi7_trusted_bound_is_an_identity_fact_not_an_authority_grant() -> None:
    # Mirrors the project's own MESSAGE != AUTHORITY invariant: this
    # function's output has no authority/effect field at all, and a
    # TRUSTED_BOUND result for one identity must never leak into or
    # influence an unrelated later call for a different identity.
    claimed_a = _identity(actor_id="actor-A")
    attested_a = {k: claimed_a[k] for k in ATTESTABLE_FIELDS}
    result_a = bind_source_identity(claimed_source=claimed_a, attestation=attested_a)

    claimed_b = _identity(actor_id="actor-B", custody_ref="custody-B")
    result_b = bind_source_identity(claimed_source=claimed_b, attestation=None)

    assert result_a["status"] == "TRUSTED_BOUND"
    assert result_b["status"] == "ASSERTED_ONLY", (
        "a TRUSTED_BOUND result for one identity must never leak into an unrelated call"
    )
    assert set(result_a.keys()) == _EXPECTED_KEYS
    assert "authority" not in result_a and "effect" not in result_a, (
        "binder output must never carry an authority or effect field of its own"
    )


def test_wi8_output_shape_and_status_field_coupling_is_consistent() -> None:
    claimed = _identity()
    attested_ok = {k: claimed[k] for k in ATTESTABLE_FIELDS}
    attested_bad = dict(attested_ok)
    attested_bad["workload_ref"] = "workload-WRONG"

    bound = bind_source_identity(claimed_source=claimed, attestation=attested_ok)
    mismatch = bind_source_identity(claimed_source=claimed, attestation=attested_bad)
    asserted = bind_source_identity(claimed_source=claimed, attestation=None)

    for report in (bound, mismatch, asserted):
        assert set(report.keys()) == _EXPECTED_KEYS
        assert report["status"] in _VALID_STATUSES

    assert (bound["status"] == "TRUSTED_BOUND") == (bound["mismatched_fields"] == () and bound["bound_fields"] != ())
    assert (mismatch["status"] == "MISMATCH") == (len(mismatch["mismatched_fields"]) > 0)
    assert (asserted["status"] == "ASSERTED_ONLY") == (asserted["bound_fields"] == () and asserted["mismatched_fields"] == ())


WORKLOAD_ATTESTATION_TESTS = [
    test_wi0_golden_path_matching_attestation_is_trusted_bound,
    test_wi1_no_attestation_is_asserted_only_never_trusted,
    test_wi2_single_field_mismatch_is_reported_by_name,
    test_wi3_custody_mismatch_alone_blocks_full_binding,
    test_wi4_multiple_simultaneous_mismatches_are_all_reported,
    test_wi5_null_claimed_field_never_counts_as_bound_even_if_attestation_agrees,
    test_wi6_principal_ref_is_out_of_scope_for_this_binder,
    test_wi7_trusted_bound_is_an_identity_fact_not_an_authority_grant,
    test_wi8_output_shape_and_status_field_coupling_is_consistent,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in WORKLOAD_ATTESTATION_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v14_validation selftest: "
          f"{len(WORKLOAD_ATTESTATION_TESTS) - len(failures)}/"
          f"{len(WORKLOAD_ATTESTATION_TESTS)} PASS")
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
