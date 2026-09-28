#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.18 (attested work-consumption gate:
the first candidate to compose v0.1's ReceiptTrail with v0.14's
workload identity attestation binder).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code
exists here.

This module **imports** (does not copy) `ReceiptTrail`, `ReceiptError`
from v0.1 and `bind_source_identity` from v0.14. It composes them; it
duplicates neither. It has no dependency on v0.2 through v0.13 or
v0.15 through v0.17.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.18/attested-work-consumption-gate-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))

from signalvev_reference_v01_validation import ReceiptError, ReceiptTrail  # noqa: E402
from signalvev_reference_v14_validation import bind_source_identity  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The attested work-consumption gate
# ---------------------------------------------------------------------------

class AttestationGateError(ValueError):
    pass


# X4's own normative text ties exactly these two receipt stages to
# actor/workload identity: "WORK_CONSUMED requires the exact actor/
# workload to claim the item." / "WORK_STARTED requires worker-owned
# evidence." No other stage is named this way -- this gate does not
# extend identity-gating to stages the source text never ties to it.
IDENTITY_GATED_STAGES = frozenset({"WORK_CONSUMED", "WORK_STARTED"})

_EXPECTED_KEYS = frozenset({"status", "reason"})
_VALID_STATUSES = frozenset({"EMITTED", "REJECTED"})


def emit_identity_gated_stage(
    *,
    trail: ReceiptTrail,
    stage: str,
    claimed_source: dict,
    attestation: dict | None,
) -> dict:
    """Only allows `trail.emit(stage, ...)` for WORK_CONSUMED/WORK_STARTED
    when `bind_source_identity` reports TRUSTED_BOUND for the exact
    actor/generation the trail itself was opened with. An ASSERTED_ONLY
    or MISMATCH identity blocks the emission entirely -- the trail is
    never mutated on rejection. v0.1's own structural rules (predecessor
    satisfaction, RELEASED actor/generation matching, etc.) still apply
    in full underneath this gate; this candidate adds a requirement on
    top of them, it does not relax or bypass any of them.
    """
    if stage not in IDENTITY_GATED_STAGES:
        raise AttestationGateError(
            f"this gate only governs {sorted(IDENTITY_GATED_STAGES)}, got {stage!r}"
        )

    identity_report = bind_source_identity(
        claimed_source=claimed_source, attestation=attestation
    )

    if identity_report["status"] != "TRUSTED_BOUND":
        return {
            "status": "REJECTED",
            "reason": f"IDENTITY_NOT_TRUSTED_BOUND:{identity_report['status']}",
        }

    # Also require the trusted identity's actor_id/generation to match
    # the exact attempt this trail was opened for -- an attested but
    # *different* actor must not be allowed to claim this attempt.
    if (
        claimed_source["actor_id"] != trail.actor_id
        or claimed_source["generation"] != trail.generation
    ):
        return {"status": "REJECTED", "reason": "ATTESTED_IDENTITY_DOES_NOT_MATCH_ATTEMPT_OWNER"}

    try:
        trail.emit(stage, actor_id=claimed_source["actor_id"], generation=claimed_source["generation"])
    except ReceiptError as exc:
        # v0.1's own structural rules (predecessor satisfaction, etc.)
        # still apply underneath this gate -- a trusted identity does
        # not bypass them.
        return {"status": "REJECTED", "reason": f"RECEIPT_TRAIL_REJECTED:{exc}"}

    return {"status": "EMITTED", "reason": None}


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers AG1-AG8
# ---------------------------------------------------------------------------

def _source(**overrides) -> dict:
    base = {
        "actor_id": "actor-worker-7f",
        "generation": "gen-2026-09-29-b",
        "workload_ref": "workload-cerebro-consumer",
        "machine_ref": "machine-node-3",
        "session_ref": "session-abc123",
        "custody_ref": "custody-worker-7f",
    }
    base.update(overrides)
    return base


def _delivered_trail(actor_id: str = "actor-worker-7f", generation: str = "gen-2026-09-29-b") -> ReceiptTrail:
    trail = ReceiptTrail(attempt_id="attempt-1", actor_id=actor_id, generation=generation)
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    return trail


def test_ag0_golden_path_trusted_bound_identity_emits_work_consumed() -> None:
    trail = _delivered_trail()
    claimed = _source()
    attested = {k: claimed[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    report = emit_identity_gated_stage(
        trail=trail, stage="WORK_CONSUMED", claimed_source=claimed, attestation=attested
    )
    assert report["status"] == "EMITTED"
    assert trail.stages_reached[-1] == "WORK_CONSUMED"


def test_ag1_asserted_only_identity_is_rejected_without_mutating_trail() -> None:
    # No attestation at all -- a bare claim must never be enough to
    # consume work, per X4's "WORK_CONSUMED requires the exact actor/
    # workload to claim the item."
    trail = _delivered_trail()
    before = list(trail.stages_reached)
    claimed = _source()
    report = emit_identity_gated_stage(
        trail=trail, stage="WORK_CONSUMED", claimed_source=claimed, attestation=None
    )
    assert report["status"] == "REJECTED"
    assert report["reason"].startswith("IDENTITY_NOT_TRUSTED_BOUND")
    assert trail.stages_reached == before, "a rejected attempt must never mutate the trail"


def test_ag2_mismatched_attestation_is_rejected_without_mutating_trail() -> None:
    trail = _delivered_trail()
    before = list(trail.stages_reached)
    claimed = _source()
    attested = {k: claimed[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    attested["custody_ref"] = "custody-SOMEONE-ELSE"
    report = emit_identity_gated_stage(
        trail=trail, stage="WORK_STARTED", claimed_source=claimed, attestation=attested
    )
    assert report["status"] == "REJECTED"
    assert report["reason"].startswith("IDENTITY_NOT_TRUSTED_BOUND")
    assert trail.stages_reached == before


def test_ag3_trusted_identity_for_a_different_actor_than_the_attempt_owner_is_rejected() -> None:
    # An attested identity is not automatically the RIGHT identity --
    # it must match the exact attempt/actor this trail belongs to.
    trail = _delivered_trail(actor_id="actor-ORIGINAL-owner", generation="gen-1")
    claimed = _source(actor_id="actor-DIFFERENT-claimant", generation="gen-1")
    attested = {k: claimed[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    report = emit_identity_gated_stage(
        trail=trail, stage="WORK_CONSUMED", claimed_source=claimed, attestation=attested
    )
    assert report["status"] == "REJECTED"
    assert report["reason"] == "ATTESTED_IDENTITY_DOES_NOT_MATCH_ATTEMPT_OWNER"


def test_ag4_underlying_receipt_trail_rules_still_apply_beneath_the_gate() -> None:
    # A trusted identity does not bypass v0.1's own predecessor rule --
    # WORK_CONSUMED still requires DELIVERED first.
    trail = ReceiptTrail(attempt_id="attempt-2", actor_id="actor-worker-7f", generation="gen-2026-09-29-b")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    # deliberately no DELIVERED emitted
    claimed = _source()
    attested = {k: claimed[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    report = emit_identity_gated_stage(
        trail=trail, stage="WORK_CONSUMED", claimed_source=claimed, attestation=attested
    )
    assert report["status"] == "REJECTED"
    assert report["reason"].startswith("RECEIPT_TRAIL_REJECTED")
    assert "WORK_CONSUMED" not in trail.stages_reached


def test_ag5_stage_outside_the_gated_set_fails_closed() -> None:
    # This gate is scoped to WORK_CONSUMED/WORK_STARTED only -- it does
    # not silently extend identity-gating to stages the source text
    # never ties to actor/workload identity.
    trail = _delivered_trail()
    claimed = _source()
    attested = {k: claimed[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    try:
        emit_identity_gated_stage(
            trail=trail, stage="DELIVERED", claimed_source=claimed, attestation=attested
        )
        raise AssertionError("expected AttestationGateError for an ungated stage")
    except AttestationGateError:
        pass


def test_ag6_a_rejected_attempt_can_still_succeed_later_with_real_attestation() -> None:
    # Rejection is not a poisoned/permanent state -- once real
    # attestation is supplied, the same trail can still legitimately
    # progress.
    trail = _delivered_trail()
    claimed = _source()

    first = emit_identity_gated_stage(
        trail=trail, stage="WORK_CONSUMED", claimed_source=claimed, attestation=None
    )
    assert first["status"] == "REJECTED"

    attested = {k: claimed[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    second = emit_identity_gated_stage(
        trail=trail, stage="WORK_CONSUMED", claimed_source=claimed, attestation=attested
    )
    assert second["status"] == "EMITTED"
    assert trail.stages_reached[-1] == "WORK_CONSUMED"


def test_ag7_a_trusted_result_for_one_trail_never_leaks_into_an_unrelated_trail() -> None:
    trail_a = _delivered_trail(actor_id="actor-A", generation="gen-A")
    claimed_a = _source(actor_id="actor-A", generation="gen-A")
    attested_a = {k: claimed_a[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    result_a = emit_identity_gated_stage(
        trail=trail_a, stage="WORK_CONSUMED", claimed_source=claimed_a, attestation=attested_a
    )

    trail_b = _delivered_trail(actor_id="actor-B", generation="gen-B")
    claimed_b = _source(actor_id="actor-B", generation="gen-B")
    result_b = emit_identity_gated_stage(
        trail=trail_b, stage="WORK_CONSUMED", claimed_source=claimed_b, attestation=None
    )

    assert result_a["status"] == "EMITTED"
    assert result_b["status"] == "REJECTED", (
        "a trusted result for one trail must never leak into an unrelated trail's gate check"
    )


def test_ag8_output_shape_and_status_field_coupling_is_consistent() -> None:
    trail = _delivered_trail()
    claimed = _source()
    attested = {k: claimed[k] for k in (
        "actor_id", "generation", "workload_ref", "machine_ref", "session_ref", "custody_ref"
    )}
    emitted = emit_identity_gated_stage(
        trail=trail, stage="WORK_CONSUMED", claimed_source=claimed, attestation=attested
    )
    rejected = emit_identity_gated_stage(
        trail=_delivered_trail(), stage="WORK_CONSUMED", claimed_source=claimed, attestation=None
    )
    for report in (emitted, rejected):
        assert set(report.keys()) == _EXPECTED_KEYS
        assert report["status"] in _VALID_STATUSES
    assert (emitted["status"] == "EMITTED") == (emitted["reason"] is None)
    assert (rejected["status"] == "REJECTED") == (rejected["reason"] is not None)


ATTESTED_WORK_CONSUMPTION_TESTS = [
    test_ag0_golden_path_trusted_bound_identity_emits_work_consumed,
    test_ag1_asserted_only_identity_is_rejected_without_mutating_trail,
    test_ag2_mismatched_attestation_is_rejected_without_mutating_trail,
    test_ag3_trusted_identity_for_a_different_actor_than_the_attempt_owner_is_rejected,
    test_ag4_underlying_receipt_trail_rules_still_apply_beneath_the_gate,
    test_ag5_stage_outside_the_gated_set_fails_closed,
    test_ag6_a_rejected_attempt_can_still_succeed_later_with_real_attestation,
    test_ag7_a_trusted_result_for_one_trail_never_leaks_into_an_unrelated_trail,
    test_ag8_output_shape_and_status_field_coupling_is_consistent,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in ATTESTED_WORK_CONSUMPTION_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v18_validation selftest: "
          f"{len(ATTESTED_WORK_CONSUMPTION_TESTS) - len(failures)}/"
          f"{len(ATTESTED_WORK_CONSUMPTION_TESTS)} PASS")
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
