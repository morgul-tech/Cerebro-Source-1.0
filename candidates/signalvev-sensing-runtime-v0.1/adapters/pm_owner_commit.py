"""Default-off PM ready-hint receipt bridge for the Signalvev candidate.

This is a consumer of a *host-bound, authenticated* PM port, not that port's
implementation. No Sheets row, caller payload, or claimed COMMITTED_READBACK
field can turn itself into owner evidence. The host must supply a port whose
read is one committed, consistent claim/packet/queue snapshot and whose
reread judges opaque revisions. No production port is wired here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from signalvev_sensing.a7_owner_receipt import VerifiedOwnerCommitReceipt
from signalvev_sensing.model import HEX64, SensingError, canonical, is_id, sha256_hex


@dataclass(frozen=True)
class ReadyHintExpectation:
    owner_ref: str
    claim_ref: str
    packet_ref: str
    queue_ref: str
    packet_sha256: str


@dataclass(frozen=True)
class PmCommittedReadyHint:
    # Returned only by the configured owner port. These flags are attestations
    # from that trust boundary, never accepted from a caller's event payload.
    authenticated: bool
    committed: bool
    readback_verified: bool
    consistent_snapshot: bool
    owner_ref: str
    receipt_ref: str
    event_id: str
    referent_id: str
    owner_seq: int
    revision_after: str
    revision_before: str | None
    provider_revision: int
    claim_ref: str
    packet_ref: str
    queue_ref: str
    claim_revision: str
    packet_revision: str
    queue_revision: str
    packet_sha256: str
    ready_state: str                   # BIND_AVAILABLE | MATERIAL_READY
    snapshot_ref: str                 # bounded owner snapshot, never row address
    snapshot_sha256: str
    readback_ref: str
    observed_at: str
    way_home: tuple[str, ...]


@dataclass(frozen=True)
class PmCurrentRead:
    authenticated: bool
    readback_verified: bool
    owner_ref: str
    referent_id: str
    current_revision: str
    relation: str                     # SAME | SUPERSEDED | UNKNOWN, owner judged
    snapshot_sha256: str
    owner_seq: int
    # BK07 projection (optional for legacy callers, required by the PM->X9 bridge via
    # require_projection=True). One consistent owner cut, supplied by the same trusted
    # port; never filled from independent rows. material_sha256/ready_state describe the
    # CURRENT material; source_cut is the provider's opaque cut id (never ordered here).
    claim_ref: str | None = None
    packet_ref: str | None = None
    queue_ref: str | None = None
    packet_sha256: str | None = None
    ready_state: str | None = None    # BIND_AVAILABLE | MATERIAL_READY
    material_sha256: str | None = None
    source_cut: str | None = None
    consistent_snapshot: bool | None = None
    active_hold: dict[str, object] | None = None  # owner-produced, validated by current X9 disposition policy


class PmOwnerReadPort(Protocol):
    """Production host owes authentication, ACL, atomicity, and owner custody."""

    def read_committed_ready_hint(
        self, receipt_ref: str, *, expected_owner_ref: str
    ) -> PmCommittedReadyHint: ...

    def reread_ready_hint(
        self, referent_id: str, *, expected_owner_ref: str,
        expected_revision: str,
    ) -> PmCurrentRead: ...


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise SensingError(code)


def _stable_ref(value: object, prefix: str) -> bool:
    return is_id(value) and value.startswith(prefix) and len(value) > len(prefix)


def _sha256(value: object) -> bool:
    return isinstance(value, str) and bool(HEX64.fullmatch(value))


class PmOwnerCommitReader:
    """Constructor-bound candidate adapter; disabled unless explicitly wired.

    The in-process duplicate guard is defense in depth only. Durable event
    identity, authenticated atomic read, and revision judgment belong to PM.
    """

    def __init__(
        self, expectation: ReadyHintExpectation, *,
        port: PmOwnerReadPort | None = None, enabled: bool = False,
    ) -> None:
        _need(type(expectation) is ReadyHintExpectation, "PM_EXPECTATION_INVALID")
        _need(all(is_id(x) for x in (
            expectation.owner_ref, expectation.claim_ref,
            expectation.packet_ref, expectation.queue_ref,
        )), "PM_EXPECTATION_INVALID")
        _need(_sha256(expectation.packet_sha256), "PM_EXPECTATION_INVALID")
        self._expectation = expectation
        self._port = port
        self._enabled = enabled is True
        self._seen: dict[str, str] = {}

    def _bound_port(self) -> PmOwnerReadPort:
        _need(self._enabled and self._port is not None, "PM_OWNER_PORT_UNBOUND")
        return self._port

    def reread_current(
        self, referent_id: str, revision: str, *,
        expected_sha256: str, expected_seq: int,
        require_projection: bool = False,
    ) -> PmCurrentRead:
        port = self._bound_port()
        _need(is_id(referent_id) and is_id(revision)
              and _sha256(expected_sha256)
              and type(expected_seq) is int and expected_seq >= 1,
              "PM_REREAD_REQUEST_INVALID")
        try:
            result = port.reread_ready_hint(
                referent_id, expected_owner_ref=self._expectation.owner_ref,
                expected_revision=revision,
            )
        except Exception as exc:
            raise SensingError("PM_OWNER_PORT_UNAVAILABLE") from exc
        _need(type(result) is PmCurrentRead, "PM_REREAD_INVALID")
        _need(result.authenticated is True and result.readback_verified is True,
              "PM_REREAD_UNVERIFIED")
        _need(result.owner_ref == self._expectation.owner_ref
              and result.referent_id == referent_id,
              "PM_REREAD_IDENTITY_MISMATCH")
        _need(isinstance(result.relation, str)
              and result.relation in {"SAME", "SUPERSEDED", "UNKNOWN"}
              and is_id(result.current_revision)
              and _sha256(result.snapshot_sha256)
              and type(result.owner_seq) is int and result.owner_seq >= 1,
              "PM_REREAD_INVALID")
        projection = (result.claim_ref, result.packet_ref, result.queue_ref,
                      result.packet_sha256, result.ready_state,
                      result.material_sha256, result.source_cut,
                      result.consistent_snapshot)
        if require_projection is True or any(v is not None for v in projection):
            _need(all(v is not None for v in projection), "PM_REREAD_PROJECTION_MISSING")
            _need(result.consistent_snapshot is True, "PM_REREAD_SPLIT_SNAPSHOT")
            _need(all(is_id(x) for x in (result.claim_ref, result.packet_ref,
                                          result.queue_ref, result.source_cut))
                  and _sha256(result.packet_sha256)
                  and _sha256(result.material_sha256)
                  and isinstance(result.ready_state, str)
                  and result.ready_state in {"BIND_AVAILABLE", "MATERIAL_READY"},
                  "PM_REREAD_PROJECTION_INVALID")
            _need((result.claim_ref, result.packet_ref, result.queue_ref)
                  == (self._expectation.claim_ref, self._expectation.packet_ref,
                      self._expectation.queue_ref),
                  "PM_REREAD_COORDINATE_MISMATCH")
        if result.relation == "SAME":
            _need(result.current_revision == revision
                  and result.snapshot_sha256 == expected_sha256
                  and result.owner_seq == expected_seq,
                  "PM_REREAD_SAME_MISMATCH")
        return result

    def read_verified(
        self, receipt_ref: str, *, expected_owner_ref: str,
    ) -> VerifiedOwnerCommitReceipt:
        port = self._bound_port()
        expected = self._expectation
        _need(expected_owner_ref == expected.owner_ref, "PM_OWNER_MISMATCH")
        _need(_stable_ref(receipt_ref, "pm-receipt:"), "PM_RECEIPT_ID_UNSTABLE")
        try:
            snap = port.read_committed_ready_hint(
                receipt_ref, expected_owner_ref=expected.owner_ref,
            )
        except Exception as exc:
            raise SensingError("PM_OWNER_PORT_UNAVAILABLE") from exc
        _need(type(snap) is PmCommittedReadyHint, "PM_OWNER_SNAPSHOT_INVALID")
        _need(snap.authenticated is True and snap.committed is True
              and snap.readback_verified is True and snap.consistent_snapshot is True,
              "PM_OWNER_SNAPSHOT_UNVERIFIED")
        _need(snap.owner_ref == expected.owner_ref
              and snap.receipt_ref == receipt_ref
              and (snap.claim_ref, snap.packet_ref, snap.queue_ref, snap.packet_sha256)
              == (expected.claim_ref, expected.packet_ref,
                  expected.queue_ref, expected.packet_sha256),
              "PM_OWNER_BINDING_MISMATCH")
        _need(_stable_ref(snap.event_id, "pm-event:")
              and _stable_ref(snap.snapshot_ref, "pm-snapshot:")
              and _stable_ref(snap.referent_id, "pm-ready:")
              and is_id(snap.readback_ref)
              and isinstance(snap.observed_at, str) and bool(snap.observed_at)
              and isinstance(snap.ready_state, str)
              and snap.ready_state in {"BIND_AVAILABLE", "MATERIAL_READY"}
              and type(snap.owner_seq) is int and snap.owner_seq >= 1
              and type(snap.provider_revision) is int and snap.provider_revision >= 0,
              "PM_OWNER_SNAPSHOT_INVALID")
        _need(is_id(snap.revision_after)
              and (snap.revision_before is None or is_id(snap.revision_before))
              and snap.claim_revision == snap.revision_after
              and snap.packet_revision == snap.revision_after
              and snap.queue_revision == snap.revision_after,
              "PM_OWNER_JOIN_REVISION_MISMATCH")
        _need(_sha256(snap.snapshot_sha256)
              and _sha256(snap.packet_sha256)
              and isinstance(snap.way_home, tuple) and bool(snap.way_home)
              and all(is_id(ref) for ref in snap.way_home),
              "PM_OWNER_SNAPSHOT_INVALID")
        current = self.reread_current(
            snap.referent_id, snap.revision_after,
            expected_sha256=snap.snapshot_sha256, expected_seq=snap.owner_seq,
        )
        _need(current.relation == "SAME", "PM_OWNER_SNAPSHOT_STALE_OR_UNKNOWN")

        # Fingerprint binds the event to all three PM records and the readback.
        # It is a local consistency fingerprint; the trusted port, not this
        # unkeyed hash, is the authentication boundary.
        fingerprint = sha256_hex(canonical({
            "owner_ref": snap.owner_ref, "receipt_ref": snap.receipt_ref,
            "event_id": snap.event_id, "referent_id": snap.referent_id,
            "owner_seq": snap.owner_seq, "revision": snap.revision_after,
            "claim_ref": snap.claim_ref, "packet_ref": snap.packet_ref,
            "queue_ref": snap.queue_ref, "packet_sha256": snap.packet_sha256,
            "ready_state": snap.ready_state, "snapshot_ref": snap.snapshot_ref,
            "snapshot_sha256": snap.snapshot_sha256,
            "readback_ref": snap.readback_ref,
        }))
        previous = self._seen.get(snap.event_id)
        _need(previous is None or previous == fingerprint,
              "PM_EVENT_ID_CONTENT_CONFLICT")
        self._seen[snap.event_id] = fingerprint
        payload = {
            "event_id": snap.event_id,
            "owner_ref": snap.owner_ref,
            "source_ref": snap.owner_ref,
            "referent": {"type": "PM_READY_HINT", "id": snap.referent_id},
            "owner_seq": snap.owner_seq,
            "revision_basis": {"after": snap.revision_after,
                               "before": snap.revision_before},
            "change_class": "SEMANTIC",
            "delta": {"kind": "POINTER", "ref": snap.snapshot_ref,
                      "expected_sha256": snap.snapshot_sha256},
            "way_home": list(snap.way_home),
            "requires_ack": False,
        }
        return VerifiedOwnerCommitReceipt(
            receipt_ref=receipt_ref, receipt_fingerprint=fingerprint,
            owner_ref=snap.owner_ref, provider_revision=snap.provider_revision,
            readback_ref=snap.readback_ref, observed_at=snap.observed_at,
            event_payload=payload, verified=True, readback_verified=True,
        )
