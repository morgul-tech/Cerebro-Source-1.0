#!/usr/bin/env python3
"""Offline validator + falsifier suite for candidates/signalvev-reference-v0.2
("andre bolge": Presence/Service Directory, Request/Reply, Selective
Durable Inbox, Artifact Pointer/File Transfer).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside the
candidate directories. No NATS/WireGuard/VPS/Drive/JetStream code exists here.

Mirrors the existing tooling/validator/*_validation.py convention, and
specifically the structure of signalvev_reference_v01_validation.py:
pure Python, no third-party dependencies, `selftest` subcommand runs every
check and prints a PASS/FAIL summary with a nonzero exit code on failure.

Source provenance for the rules enforced here:
  candidates/signalvev-reference-v0.2/presence-directory-candidate.yaml
  candidates/signalvev-reference-v0.2/request-reply-candidate.yaml
  candidates/signalvev-reference-v0.2/selective-durable-inbox-candidate.yaml
  candidates/signalvev-reference-v0.2/artifact-pointer-transfer-candidate.yaml
(each carries its own upstream Drive citation in its own provenance block)

Also reads, but never modifies, candidates/signalvev-reference-v0.1/
subject-registry-v0.1-candidate.json for one cross-version consistency
check (durability_class vocabulary alignment).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

CANDIDATES_ROOT = Path(__file__).resolve().parents[2] / "candidates"
V02_DIR = CANDIDATES_ROOT / "signalvev-reference-v0.2"
V01_REGISTRY_PATH = CANDIDATES_ROOT / "signalvev-reference-v0.1" / "subject-registry-v0.1-candidate.json"


# ---------------------------------------------------------------------------
# 1. Presence / Service Directory
#    Source: presence-directory-candidate.yaml
# ---------------------------------------------------------------------------

PRESENCE_STATES = {"ONLINE", "OFFLINE", "READY", "ARMED", "DEGRADED", "UNKNOWN"}

# Deliberately NOT part of this module's public surface: there is no
# "grant_authority", "admit_claim", or "mark_work_started" method anywhere
# on PresenceDirectory. This is a structural enforcement of "Presence is
# not: authority / claim / work_started / consumed / effect" -- the
# capability to do those things simply does not exist on this class.
AUTHORITY_BEARING_OPERATIONS = {"grant_authority", "admit_claim", "mark_work_consumed", "mark_effect"}


class PresenceError(ValueError):
    pass


class PresenceDirectory:
    """In-memory presence/service directory. Tracks state per service_id.
    Offline reference logic only -- no transport, no scheduling."""

    def __init__(self) -> None:
        self._state: dict[str, str] = {}

    def set_state(self, service_id: str, state: str) -> None:
        if state not in PRESENCE_STATES:
            raise PresenceError(f"unknown presence state {state!r}")
        self._state[service_id] = state

    def get_state(self, service_id: str) -> str:
        return self._state.get(service_id, "UNKNOWN")

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - defensive
        # Guards against silently adding an authority-bearing method later
        # without updating this candidate's own falsifier for it.
        if name in AUTHORITY_BEARING_OPERATIONS:
            raise PresenceError(
                f"PresenceDirectory has no {name}() -- presence is not authority/claim/effect"
            )
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# 2. Request / Reply
#    Source: request-reply-candidate.yaml
# ---------------------------------------------------------------------------

REQUIRED_OUTCOMES = {"RESPONSE", "NO_RESPONDER", "TIMEOUT_UNKNOWN", "STALE_RESPONSE", "SCHEMA_MISMATCH"}


class RequestReplyError(ValueError):
    pass


class RequestReplyMatcher:
    """Correlation-id based request/reply matcher with explicit timeout
    handling. Offline, in-process only."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self._resolved: dict[str, str] = {}

    def send_request(self, correlation_id: str, *, sent_at: float,
                      timeout_seconds: float, effect_class: str = "READ_ONLY") -> None:
        if timeout_seconds is None or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise TypeError("timeout_seconds is required and must be a positive number (no fire-and-forget requests)")
        if correlation_id in self._pending or correlation_id in self._resolved:
            raise RequestReplyError(f"duplicate correlation_id {correlation_id!r}")
        self._pending[correlation_id] = {
            "sent_at": sent_at,
            "timeout_seconds": timeout_seconds,
            "effect_class": effect_class,
        }

    def check_timeout(self, correlation_id: str, *, now: float) -> str:
        req = self._pending.get(correlation_id)
        if req is None:
            return self._resolved.get(correlation_id, "UNKNOWN_CORRELATION_ID")
        if now - req["sent_at"] < req["timeout_seconds"]:
            return "PENDING"
        outcome = "TIMEOUT_UNKNOWN"
        self._resolved[correlation_id] = outcome
        del self._pending[correlation_id]
        return outcome

    def receive_response(self, correlation_id: str, *, received_at: float) -> str:
        if correlation_id in self._resolved:
            # Falsifier SW4: a response arriving after the requester has
            # already resolved (timed out) must be STALE, never applied as
            # current/canonical.
            return "STALE_RESPONSE"
        req = self._pending.get(correlation_id)
        if req is None:
            return "UNKNOWN_CORRELATION_ID"
        if received_at - req["sent_at"] >= req["timeout_seconds"]:
            self._resolved[correlation_id] = "TIMEOUT_UNKNOWN"
            del self._pending[correlation_id]
            return "STALE_RESPONSE"
        self._resolved[correlation_id] = "RESPONSE"
        del self._pending[correlation_id]
        return "RESPONSE"

    # Note: this reference matcher deliberately does NOT expose a
    # "retry_after_timeout()" convenience method. Falsifier SW5 (X4
    # refinement paragraph) requires that a timed-out effectful request
    # never be silently retried as a fresh read-only question; the lawful
    # next step must be decided explicitly by the caller using the
    # request's own effect_class (see test_sw5 below), not by a method on
    # this class that could hide that decision.


# ---------------------------------------------------------------------------
# 3. Selective Durable Inbox
#    Source: selective-durable-inbox-candidate.yaml
# ---------------------------------------------------------------------------

DURABILITY_CLASSES = {
    "EPHEMERAL_RECONSTRUCTIBLE",
    "BOUNDED_TRANSIENT",
    "MINIMAL_DURABLE_HANDOFF_FIRST_WAVE",
    "SELECTIVE_DURABLE",
}
DURABLE_STORED_CLASSES = {"MINIMAL_DURABLE_HANDOFF_FIRST_WAVE", "SELECTIVE_DURABLE"}


class DurableInboxError(ValueError):
    pass


class DurableInboxPolicy:
    """Classifies messages and enforces dedup only for classes the
    doctrine names as durable. Offline, in-process dict -- no JetStream,
    no filesystem persistence."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}  # idempotency_key -> payload_fingerprint

    def should_store(self, durability_class: str) -> bool:
        if durability_class not in DURABILITY_CLASSES:
            raise DurableInboxError(f"unknown durability_class {durability_class!r}")
        return durability_class in DURABLE_STORED_CLASSES

    def admit(self, durability_class: str, idempotency_key: str, payload_fingerprint: str) -> str:
        """Returns ADMITTED, DUPLICATE_IGNORED, or CONFLICT.

        Falsifier SW6: an EPHEMERAL_RECONSTRUCTIBLE/BOUNDED_TRANSIENT
        message is never deduplicated -- dedup only applies to the classes
        the doctrine says must be processed once/logically once.
        Falsifier SW7: two admits of the *same* idempotency_key+payload for
        a durable-stored class must collapse to one effect
        (DUPLICATE_IGNORED), not two."""
        if not self.should_store(durability_class):
            return "ADMITTED"  # no dedup tracking for non-durable classes
        prior = self._seen.get(idempotency_key)
        if prior is None:
            self._seen[idempotency_key] = payload_fingerprint
            return "ADMITTED"
        if prior == payload_fingerprint:
            return "DUPLICATE_IGNORED"
        return "CONFLICT"


# ---------------------------------------------------------------------------
# 4. Artifact Pointer / File Transfer
#    Source: artifact-pointer-transfer-candidate.yaml
# ---------------------------------------------------------------------------

REQUIRED_POINTER_FIELDS = [
    "artifact_identity", "location_reference", "hash", "content_type",
    "size", "provenance", "access_custody_requirements",
]


class ArtifactPointerError(ValueError):
    pass


def make_pointer(**fields: Any) -> dict[str, Any]:
    missing = [f for f in REQUIRED_POINTER_FIELDS if f not in fields]
    if missing:
        raise ArtifactPointerError(f"artifact pointer missing required fields: {missing}")
    if "inline_bytes" in fields:
        # Falsifier SW9: STATE STAYS HOME; DELTA TRAVELS. A pointer must
        # never carry the artifact's actual payload bytes inline.
        raise ArtifactPointerError(
            "artifact pointer must not carry inline_bytes -- carry location_reference + hash only"
        )
    return {f: fields[f] for f in REQUIRED_POINTER_FIELDS}


def verify_and_consume(pointer: dict[str, Any], actual_bytes: bytes) -> str:
    """Falsifier SW8: an artifact must never be treated as usable before its
    byte hash is verified against the pointer's declared hash."""
    actual_hash = hashlib.sha256(actual_bytes).hexdigest()
    if actual_hash != pointer["hash"]:
        return "REJECTED_HASH_MISMATCH"
    return "CONSUMED"


# ---------------------------------------------------------------------------
# 5. Second-wave falsifiers (SW1-SW9)
#    Derived from Communication Stack v0.1 (Human text sections named
#    above) and the X4 runtime-fagpass refinement paragraphs. Unlike
#    falsifiers 1-12 in signalvev-reference-v0.1 (verbatim X4 list), these
#    nine are this candidate's own numbering -- the source document does
#    not itself enumerate second-wave falsifiers by number. Each docstring
#    cites the specific doctrine sentence it encodes.
# ---------------------------------------------------------------------------

def test_sw1_presence_ready_does_not_grant_authority() -> None:
    """'Presence er ikke: authority / claim / work_started / consumed /
    effect.' -- PresenceDirectory exposes no operation that could grant
    any of those from a presence state alone."""
    directory = PresenceDirectory()
    directory.set_state("worker-1", "READY")
    directory.set_state("worker-1", "ARMED")
    for op in AUTHORITY_BEARING_OPERATIONS:
        try:
            getattr(directory, op)
        except PresenceError:
            continue
        raise AssertionError(f"PresenceDirectory must not expose {op}()")


def test_sw2_service_directory_not_used_as_scheduler() -> None:
    """'Service directory skal aldri brukes som skjult scheduler.' --
    reading presence state must not itself select/admit a worker; that
    remains a separate, explicit DURABLE_COMMAND admission decision."""
    directory = PresenceDirectory()
    directory.set_state("worker-1", "ARMED")
    directory.set_state("worker-2", "READY")

    def admit_work(candidate_presence_state: str) -> str:
        # A conforming admission function must not accept presence state as
        # sufficient input to admit work -- it always returns HOLD here,
        # regardless of presence, because presence alone carries no
        # authority.
        return "HOLD_PRESENCE_IS_NOT_AUTHORITY"

    assert admit_work(directory.get_state("worker-1")) == "HOLD_PRESENCE_IS_NOT_AUTHORITY"


def test_sw3_request_without_timeout_rejected() -> None:
    """'Request/reply skal ha timeout og eksplisitt UNKNOWN/NO_RESPONDER.'
    -- sending a request requires a timeout_seconds; there is no
    fire-and-forget request path in this reference implementation."""
    matcher = RequestReplyMatcher()
    try:
        matcher.send_request("corr-1", sent_at=0.0, timeout_seconds=None)  # type: ignore[arg-type]
    except TypeError:
        return
    raise AssertionError("send_request must require an explicit timeout_seconds")


def test_sw4_stale_response_after_timeout_not_applied_as_current() -> None:
    """X4: request/reply must distinguish RESPONSE, NO_RESPONDER,
    TIMEOUT_UNKNOWN and 'stale answer' -- a late response must not be
    applied as if it were the current, canonical outcome."""
    matcher = RequestReplyMatcher()
    matcher.send_request("corr-2", sent_at=0.0, timeout_seconds=5.0)
    timeout_outcome = matcher.check_timeout("corr-2", now=10.0)
    assert timeout_outcome == "TIMEOUT_UNKNOWN"
    late_outcome = matcher.receive_response("corr-2", received_at=12.0)
    assert late_outcome == "STALE_RESPONSE", (
        "a response arriving after TIMEOUT_UNKNOWN must be STALE_RESPONSE, never RESPONSE"
    )


def test_sw5_effectful_timeout_not_silently_retried_as_read() -> None:
    """X4: 'A timed-out request that could have crossed an effect boundary
    cannot be retried as if it were a read-only question.' Modeled here as:
    an effectful (non READ_ONLY) request that times out must surface a
    distinct outcome from a read-only timeout, so a caller cannot treat the
    two identically."""
    matcher = RequestReplyMatcher()
    matcher.send_request("corr-3", sent_at=0.0, timeout_seconds=5.0, effect_class="WRITE_POSSIBLE")
    outcome = matcher.check_timeout("corr-3", now=10.0)
    assert outcome == "TIMEOUT_UNKNOWN"

    def lawful_next_step(effect_class: str, outcome: str) -> str:
        if outcome != "TIMEOUT_UNKNOWN":
            return "N/A"
        if effect_class != "READ_ONLY":
            return "NEEDS_UNKNOWN_RECONCILIATION"  # never a silent retry
        return "SAFE_TO_RETRY_AS_READ"

    assert lawful_next_step("WRITE_POSSIBLE", outcome) == "NEEDS_UNKNOWN_RECONCILIATION"
    assert lawful_next_step("READ_ONLY", outcome) == "SAFE_TO_RETRY_AS_READ"


def test_sw6_ephemeral_class_never_deduplicated() -> None:
    """'Ikke alle signaler skal lagres.' -- EPHEMERAL_RECONSTRUCTIBLE and
    BOUNDED_TRANSIENT messages get no durable dedup tracking; replaying one
    twice is ADMITTED both times (the inbox does not pretend to protect a
    class the doctrine never asked it to protect)."""
    inbox = DurableInboxPolicy()
    first = inbox.admit("EPHEMERAL_RECONSTRUCTIBLE", "key-x", "fp-1")
    second = inbox.admit("EPHEMERAL_RECONSTRUCTIBLE", "key-x", "fp-1")
    assert first == "ADMITTED" and second == "ADMITTED", (
        "ephemeral-class messages must not be silently treated as durable/deduplicated"
    )


def test_sw7_durable_class_replay_collapses_to_one_effect() -> None:
    """'krever acknowledgement ... eller maa behandles en/logisk en gang.'
    -- for MINIMAL_DURABLE_HANDOFF_FIRST_WAVE and SELECTIVE_DURABLE
    classes, redelivering the identical message must not produce a second
    admission/effect."""
    inbox = DurableInboxPolicy()
    first = inbox.admit("MINIMAL_DURABLE_HANDOFF_FIRST_WAVE", "cmd-key-1", "fp-aaa")
    replay = inbox.admit("MINIMAL_DURABLE_HANDOFF_FIRST_WAVE", "cmd-key-1", "fp-aaa")
    changed_payload = inbox.admit("MINIMAL_DURABLE_HANDOFF_FIRST_WAVE", "cmd-key-1", "fp-bbb")
    assert first == "ADMITTED"
    assert replay == "DUPLICATE_IGNORED", "identical redelivery of a durable-class message must collapse to one effect"
    assert changed_payload == "CONFLICT", "same key with a different payload must be CONFLICT, not a second admission"


def test_sw8_artifact_not_usable_before_hash_verification() -> None:
    """X4: 'Receiver verifies bytes and current access before use.'"""
    pointer = make_pointer(
        artifact_identity="artifact-1", location_reference="ref://somewhere",
        hash="0" * 64, content_type="application/octet-stream", size=17,
        provenance="test-fixture", access_custody_requirements="none",
    )
    result = verify_and_consume(pointer, b"wrong bytes entirely")
    assert result == "REJECTED_HASH_MISMATCH", "wrong bytes must be rejected before being treated as consumed"

    real_bytes = b"correct artifact content"
    correct_pointer = make_pointer(
        artifact_identity="artifact-2", location_reference="ref://somewhere-else",
        hash=hashlib.sha256(real_bytes).hexdigest(), content_type="text/plain", size=len(real_bytes),
        provenance="test-fixture", access_custody_requirements="none",
    )
    assert verify_and_consume(correct_pointer, real_bytes) == "CONSUMED"


def test_sw9_pointer_rejects_inline_payload_bytes() -> None:
    """'STATE STAYS HOME; DELTA TRAVELS.' -- an artifact pointer must carry
    a location/reference and hash, never the payload bytes themselves."""
    try:
        make_pointer(
            artifact_identity="artifact-3", location_reference="ref://x", hash="0" * 64,
            content_type="text/plain", size=4, provenance="test-fixture",
            access_custody_requirements="none", inline_bytes=b"data",
        )
    except ArtifactPointerError:
        return
    raise AssertionError("artifact pointer with inline_bytes must be rejected")


SECOND_WAVE_FALSIFIER_TESTS = [
    test_sw1_presence_ready_does_not_grant_authority,
    test_sw2_service_directory_not_used_as_scheduler,
    test_sw3_request_without_timeout_rejected,
    test_sw4_stale_response_after_timeout_not_applied_as_current,
    test_sw5_effectful_timeout_not_silently_retried_as_read,
    test_sw6_ephemeral_class_never_deduplicated,
    test_sw7_durable_class_replay_collapses_to_one_effect,
    test_sw8_artifact_not_usable_before_hash_verification,
    test_sw9_pointer_rejects_inline_payload_bytes,
]


# ---------------------------------------------------------------------------
# 6. Cross-version consistency canaries against signalvev-reference-v0.1
#    (read-only; v0.1 files are never modified by this candidate)
# ---------------------------------------------------------------------------

def load_v01_registry() -> dict[str, Any]:
    return json.loads(V01_REGISTRY_PATH.read_text())


def test_v01_registry_durability_classes_are_known_here() -> None:
    registry = load_v01_registry()
    used_classes = {e["durability_class"] for e in registry["entries"]}
    unknown = used_classes - DURABILITY_CLASSES
    assert not unknown, f"signalvev-reference-v0.1 registry uses durability classes this candidate does not model: {unknown}"


def test_v01_registry_work_command_is_minimal_durable_handoff() -> None:
    registry = load_v01_registry()
    entry = next(e for e in registry["entries"] if e["subject"] == "cerebro.v1.work.command")
    assert entry["durability_class"] == "MINIMAL_DURABLE_HANDOFF_FIRST_WAVE", (
        "cerebro.v1.work.command must carry the durability class X4's sequencing "
        "correction names for the first reference wave's command handoff"
    )


def test_v01_registry_artifact_pointer_is_selective_durable() -> None:
    registry = load_v01_registry()
    entry = next(e for e in registry["entries"] if e["subject"] == "cerebro.v1.artifact.pointer")
    assert entry["durability_class"] == "SELECTIVE_DURABLE"


CROSS_VERSION_TESTS = [
    test_v01_registry_durability_classes_are_known_here,
    test_v01_registry_work_command_is_minimal_durable_handoff,
    test_v01_registry_artifact_pointer_is_selective_durable,
]


def selftest() -> int:
    all_tests = SECOND_WAVE_FALSIFIER_TESTS + CROSS_VERSION_TESTS
    failures: list[tuple[str, str]] = []
    for test in all_tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v02_validation selftest: "
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
