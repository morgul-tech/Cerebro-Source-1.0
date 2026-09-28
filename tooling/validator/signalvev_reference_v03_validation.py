#!/usr/bin/env python3
"""Offline validator + falsifier suite for candidates/signalvev-reference-v0.3
("tredje bolge": Communication Flight Recorder, Workload Identity/
Attestation, Schema/version compatibility).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside the
candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code exists here.

Mirrors the existing tooling/validator/*_validation.py convention, and
specifically the structure of signalvev_reference_v01_validation.py and
signalvev_reference_v02_validation.py: pure Python, no third-party
dependencies, `selftest` subcommand runs every check and prints a
PASS/FAIL summary with a nonzero exit code on failure.

Source provenance for the rules enforced here:
  candidates/signalvev-reference-v0.3/flight-recorder-candidate.yaml
  candidates/signalvev-reference-v0.3/workload-identity-attestation-candidate.yaml
  candidates/signalvev-reference-v0.3/schema-version-compatibility-candidate.yaml
(each carries its own upstream Drive citation in its own provenance block)

Reads, but never modifies:
  tooling/validator/signalvev_reference_v01_validation.py (STAGE_PREDECESSORS,
    ReceiptTrail -- reused, not redefined, for the flight recorder)
  candidates/signalvev-reference-v0.1/subject-registry-v0.1-candidate.json
    (compatibility field values)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
CANDIDATES_ROOT = Path(__file__).resolve().parents[2] / "candidates"
V01_REGISTRY_PATH = CANDIDATES_ROOT / "signalvev-reference-v0.1" / "subject-registry-v0.1-candidate.json"

sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))
from signalvev_reference_v01_validation import (  # noqa: E402  (import after sys.path insert, by design)
    STAGE_PREDECESSORS,
    ReceiptTrail,
)


# ---------------------------------------------------------------------------
# 1. Communication Flight Recorder
#    Source: flight-recorder-candidate.yaml
#    Reuses signalvev-reference-v0.1's STAGE_PREDECESSORS -- does not
#    redefine the receipt ladder.
# ---------------------------------------------------------------------------

class FlightRecorder:
    """Read-only analysis over a receipt trail's `stages_reached` list.
    Has no method that emits, mutates, or backfills a receipt -- it can
    only look at a trail that some other code (ReceiptTrail.emit) already
    produced."""

    @staticmethod
    def first_broken_edge(reached: list[str]) -> str | None:
        """Returns the first stage in `reached` whose predecessor set is
        not satisfied by the stages that came before it, or None if the
        trail is internally consistent. Mirrors v0.1's falsifier-11 inline
        function, promoted to a reusable, read-only API."""
        for i in range(1, len(reached)):
            stage = reached[i]
            preds = STAGE_PREDECESSORS.get(stage)
            if preds is None:
                return stage  # unknown stage name is itself a broken edge
            if preds and not (set(reached[:i]) & preds):
                return stage
        return None

    @staticmethod
    def explain(reached: list[str]) -> dict[str, Any]:
        """Returns a report distinguishing evidence (stages actually
        reached) from uncertainty (UNKNOWN beyond the first broken edge),
        never guessing what stage 'should' come next."""
        broken = FlightRecorder.first_broken_edge(reached)
        if broken is None:
            return {"evidence": list(reached), "first_broken_edge": None, "status": "CONSISTENT"}
        idx = reached.index(broken)
        return {
            "evidence": list(reached[:idx]),
            "first_broken_edge": broken,
            "beyond_broken_edge": "UNKNOWN",  # never a guessed stage
            "status": "BROKEN",
        }


# ---------------------------------------------------------------------------
# 2. Workload Identity / Attestation
#    Source: workload-identity-attestation-candidate.yaml
# ---------------------------------------------------------------------------

REQUIRED_ATTESTATION_COMPONENTS = {"actor", "generation", "workload", "machine_session", "current_custody"}
REJECTED_IDENTITY_SOURCES = {"pc_name", "window_title", "credentials_file", "chat_name"}


class AttestationError(ValueError):
    pass


class AttestationBinder:
    """Binds an envelope's claimed `source` fields to an independent
    attestation record. The envelope's `source` alone is never sufficient
    -- it remains a claim until every required attestation component
    matches."""

    @staticmethod
    def attest(**components: str) -> dict[str, str]:
        rejected = set(components) & REJECTED_IDENTITY_SOURCES
        if rejected:
            raise AttestationError(
                f"attestation must not be built from {rejected} -- "
                "PC-name/window-title/credentials-file/chat-name are not identity"
            )
        missing = REQUIRED_ATTESTATION_COMPONENTS - set(components)
        if missing:
            raise AttestationError(f"attestation missing required components: {sorted(missing)}")
        return {k: components[k] for k in REQUIRED_ATTESTATION_COMPONENTS}

    @staticmethod
    def bind(envelope_source: dict[str, Any], attestation: dict[str, str] | None) -> str:
        """Returns BOUND only when every attested component matches the
        envelope's claimed source; otherwise UNATTESTED_CLAIM_ONLY. The
        envelope may carry an authority reference; it may not mint one."""
        if attestation is None:
            return "UNATTESTED_CLAIM_ONLY"
        mapping = {
            "actor": "actor_id",
            "generation": "generation",
            "workload": "workload_ref",
            "machine_session": "session_ref",
            "current_custody": "custody_ref",
        }
        for attested_key, envelope_key in mapping.items():
            if envelope_source.get(envelope_key) != attestation.get(attested_key):
                return "UNATTESTED_CLAIM_ONLY"
        return "BOUND"


# ---------------------------------------------------------------------------
# 3. Schema / version compatibility
#    Source: schema-version-compatibility-candidate.yaml
# ---------------------------------------------------------------------------

KNOWN_COMPATIBILITY_POLICIES = {
    "exact_schema_version_or_typed_hold",
    "version_negotiated_before_reply",
    "no_implicit_upgrade;unknown_version_holds",
    "unknown_stage_holds",
    "content_type_and_schema_version_checked",
}
CANONICAL_SCHEMA_VERSION = "1.0.0-draft"  # unchanged since signalvev-reference-v0.1


class CompatibilityError(ValueError):
    pass


class CompatibilityPolicy:
    """Resolves a subject/version/producer against the registry's own
    compatibility policy vocabulary. Unknown input is always a typed
    HOLD, never an improvised fallback."""

    @staticmethod
    def resolve(*, policy: str, declared_version: str, known_subject: bool,
                known_producer: bool) -> str:
        if policy not in KNOWN_COMPATIBILITY_POLICIES:
            return "HOLD_UNKNOWN_POLICY"
        if not known_subject or not known_producer:
            return "HOLD_UNKNOWN_SUBJECT_OR_PRODUCER"

        if policy == "exact_schema_version_or_typed_hold":
            return "ADMIT" if declared_version == CANONICAL_SCHEMA_VERSION else "HOLD_VERSION_MISMATCH"

        if policy == "no_implicit_upgrade;unknown_version_holds":
            # An older or newer version is never silently treated as
            # compatible -- exact match only, same as
            # exact_schema_version_or_typed_hold, stated as its own named
            # policy per the registry entry it governs
            # (cerebro.v1.work.command).
            return "ADMIT" if declared_version == CANONICAL_SCHEMA_VERSION else "HOLD_NO_IMPLICIT_UPGRADE"

        if policy == "version_negotiated_before_reply":
            return "ADMIT" if declared_version == CANONICAL_SCHEMA_VERSION else "SCHEMA_MISMATCH"

        if policy == "unknown_stage_holds":
            return "ADMIT" if declared_version == CANONICAL_SCHEMA_VERSION else "HOLD_UNKNOWN_STAGE"

        if policy == "content_type_and_schema_version_checked":
            return "ADMIT" if declared_version == CANONICAL_SCHEMA_VERSION else "HOLD_VERSION_MISMATCH"

        return "HOLD_UNKNOWN_POLICY"  # pragma: no cover - exhaustive above


# ---------------------------------------------------------------------------
# 4. Third-wave falsifiers (TW1-TW9)
#    Derived from Communication Stack v0.1 and the X4 runtime-fagpass, as
#    with signalvev-reference-v0.2's SW1-SW9 -- the source document names
#    the third-wave primitives and their governing sentences but does not
#    itself enumerate third-wave falsifiers by number. Each docstring
#    quotes the exact source sentence it encodes.
# ---------------------------------------------------------------------------

def test_tw1_flight_recorder_never_fabricates_missing_receipt() -> None:
    """X4 (Trace paragraph): 'It must not generate missing receipts or
    override canonical state.' FlightRecorder exposes no emit/mutate
    method at all -- only read-only analysis functions."""
    forbidden = {"emit", "mutate", "backfill", "override_canonical_state"}
    for name in forbidden:
        assert not hasattr(FlightRecorder, name), f"FlightRecorder must not expose {name}()"


def test_tw2_gap_reported_as_unknown_not_guessed() -> None:
    """Human text: 'Skal vise forste brutte edge og UNKNOWN uten a
    gjette.'"""
    trail = ReceiptTrail("a1", "actor-1", "g1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    truncated = trail.stages_reached[:2] + ["TERMINAL"]  # skip WORK_CONSUMED..EFFECT_*
    report = FlightRecorder.explain(truncated)
    assert report["status"] == "BROKEN"
    assert report["beyond_broken_edge"] == "UNKNOWN", "a gap must be reported as UNKNOWN, never a guessed stage"


def test_tw3_first_broken_edge_is_exact_not_just_incomplete() -> None:
    """Human text: 'Skal vise forste brutte edge...' -- the recorder must
    name the exact stage, not just say 'incomplete'."""
    trail = ReceiptTrail("a2", "actor-1", "g1")
    trail.emit("PRODUCED")
    trail.emit("TRANSPORT_ACCEPTED")
    trail.emit("DELIVERED")
    trail.emit("WORK_CONSUMED")
    trail.emit("WORK_STARTED")
    trail.emit("EFFECT_ACCEPTED")
    trail.emit("TERMINAL")
    assert FlightRecorder.first_broken_edge(trail.stages_reached) is None, "a legitimate trail must report no broken edge"

    claim_skipping_work_started = trail.stages_reached[:4] + ["EFFECT_ACCEPTED"]
    broken = FlightRecorder.first_broken_edge(claim_skipping_work_started)
    assert broken == "EFFECT_ACCEPTED", f"expected EFFECT_ACCEPTED as the exact broken edge, got {broken!r}"


def test_tw4_unattested_source_is_never_bound() -> None:
    """X4 (Envelope paragraph): 'source and authority_class are asserted
    metadata... The envelope may carry an authority reference; it may not
    mint one.'"""
    envelope_source = {
        "principal_ref": "p-1", "actor_id": "actor-1", "generation": "gen-1",
        "workload_ref": "wl-1", "session_ref": "sess-1", "custody_ref": "cust-1",
    }
    result = AttestationBinder.bind(envelope_source, attestation=None)
    assert result == "UNATTESTED_CLAIM_ONLY", "an envelope with no matching attestation must remain a claim, never BOUND"


def test_tw5_attestation_requires_all_five_components() -> None:
    """Human text: 'Malet er attestert: actor + generation + workload +
    machine/session + current custody.'"""
    try:
        AttestationBinder.attest(actor="a", generation="g", workload="w", machine_session="m")
        # current_custody deliberately omitted
    except AttestationError:
        pass
    else:
        raise AssertionError("attestation missing current_custody must be rejected")

    complete = AttestationBinder.attest(
        actor="a", generation="g", workload="w", machine_session="m", current_custody="c",
    )
    envelope_source = {
        "principal_ref": "p-1", "actor_id": "a", "generation": "g",
        "workload_ref": "w", "session_ref": "m", "custody_ref": "c",
    }
    assert AttestationBinder.bind(envelope_source, complete) == "BOUND"


def test_tw6_pc_name_window_title_credentials_chatname_rejected() -> None:
    """Human text (verbatim negative list): 'ikke bare: PC-navn /
    vindustittel / credentials-fil / eller chatnavn.'"""
    for bad_key in ("pc_name", "window_title", "credentials_file", "chat_name"):
        kwargs = {
            "actor": "a", "generation": "g", "workload": "w",
            "machine_session": "m", "current_custody": "c", bad_key: "anything",
        }
        try:
            AttestationBinder.attest(**kwargs)
        except AttestationError:
            continue
        raise AssertionError(f"attestation must reject {bad_key} as an identity source")


def test_tw7_unknown_subject_or_producer_is_typed_hold() -> None:
    """X4 (Registry paragraph): 'Unknown subject/version/producer is a
    typed HOLD or quarantine, not an improvised fallback.'"""
    outcome = CompatibilityPolicy.resolve(
        policy="exact_schema_version_or_typed_hold", declared_version=CANONICAL_SCHEMA_VERSION,
        known_subject=False, known_producer=True,
    )
    assert outcome == "HOLD_UNKNOWN_SUBJECT_OR_PRODUCER", "an unknown subject must resolve to a typed HOLD, not an improvised admit"

    unknown_policy_outcome = CompatibilityPolicy.resolve(
        policy="made_up_policy_not_in_registry", declared_version=CANONICAL_SCHEMA_VERSION,
        known_subject=True, known_producer=True,
    )
    assert unknown_policy_outcome == "HOLD_UNKNOWN_POLICY"


def test_tw8_no_implicit_version_upgrade() -> None:
    """Registry: 'no_implicit_upgrade;unknown_version_holds'
    (cerebro.v1.work.command) -- an older schema_version must never be
    silently treated as compatible with the current one."""
    outcome = CompatibilityPolicy.resolve(
        policy="no_implicit_upgrade;unknown_version_holds", declared_version="0.9.0-draft",
        known_subject=True, known_producer=True,
    )
    assert outcome == "HOLD_NO_IMPLICIT_UPGRADE", "an older/different schema_version must not be silently upgraded and admitted"


def test_tw9_version_negotiated_before_reply_else_schema_mismatch() -> None:
    """Registry: 'version_negotiated_before_reply'
    (cerebro.v1.status.request / cerebro.v1.status.response) -- ties to
    request-reply-candidate.yaml (signalvev-reference-v0.2)
    required_outcomes, which already lists SCHEMA_MISMATCH as a lawful
    outcome distinct from RESPONSE."""
    mismatched = CompatibilityPolicy.resolve(
        policy="version_negotiated_before_reply", declared_version="2.0.0-draft",
        known_subject=True, known_producer=True,
    )
    assert mismatched == "SCHEMA_MISMATCH"
    matched = CompatibilityPolicy.resolve(
        policy="version_negotiated_before_reply", declared_version=CANONICAL_SCHEMA_VERSION,
        known_subject=True, known_producer=True,
    )
    assert matched == "ADMIT"


THIRD_WAVE_FALSIFIER_TESTS = [
    test_tw1_flight_recorder_never_fabricates_missing_receipt,
    test_tw2_gap_reported_as_unknown_not_guessed,
    test_tw3_first_broken_edge_is_exact_not_just_incomplete,
    test_tw4_unattested_source_is_never_bound,
    test_tw5_attestation_requires_all_five_components,
    test_tw6_pc_name_window_title_credentials_chatname_rejected,
    test_tw7_unknown_subject_or_producer_is_typed_hold,
    test_tw8_no_implicit_version_upgrade,
    test_tw9_version_negotiated_before_reply_else_schema_mismatch,
]


# ---------------------------------------------------------------------------
# 5. Cross-version consistency canaries against signalvev-reference-v0.1
#    (read-only; v0.1 files are never modified by this candidate)
# ---------------------------------------------------------------------------

def load_v01_registry() -> dict[str, Any]:
    return json.loads(V01_REGISTRY_PATH.read_text())


def test_v01_registry_compatibility_values_are_all_known_here() -> None:
    registry = load_v01_registry()
    used_policies = {e["compatibility"] for e in registry["entries"]}
    unknown = used_policies - KNOWN_COMPATIBILITY_POLICIES
    assert not unknown, f"signalvev-reference-v0.1 registry uses compatibility policies this candidate does not model: {unknown}"


def test_v01_receipt_stage_vocabulary_matches_flight_recorder() -> None:
    # FlightRecorder is built directly against STAGE_PREDECESSORS imported
    # from v0.1 -- this canary just confirms the import actually landed
    # the full eleven-stage vocabulary, catching a silent drift if v0.1's
    # module is ever refactored.
    expected_stage_count = 11
    assert len(STAGE_PREDECESSORS) == expected_stage_count, (
        f"expected {expected_stage_count} receipt stages from v0.1, found {len(STAGE_PREDECESSORS)}"
    )


CROSS_VERSION_TESTS = [
    test_v01_registry_compatibility_values_are_all_known_here,
    test_v01_receipt_stage_vocabulary_matches_flight_recorder,
]


def selftest() -> int:
    all_tests = THIRD_WAVE_FALSIFIER_TESTS + CROSS_VERSION_TESTS
    failures: list[tuple[str, str]] = []
    for test in all_tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v03_validation selftest: "
          f"{len(all_tests) - len(failures)}/{len(all_tests)} PASS")
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
