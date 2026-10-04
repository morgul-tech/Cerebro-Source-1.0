"""Default-off X9 channel ingress candidate for a selected D0 POINTER.

This is a port contract and local harness, not a Google Sheets client, a pulse,
or a PM truth store. A real channel implementation must authenticate its own
principal and prove append/readback by event ID and canonical content hash.
The existing X9 pulse must call consume; depositing a row does not wake it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol

from signalvev_sensing.return_sink import ClosureRecord

SUBJECT = "cerebro.v1.artifact.pointer"
CHANNEL = "PROSJEKTMANN_CHANNEL"
SCHEMA = "signalvev.x9.channel.pointer/v0.1"
DISPOSITION_SCHEMA = "signalvev.x9.channel.disposition/v0.1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}$")
REFINE = "REFINE_INGRESS_PORT_UNBOUND"
HOLD = "HOLD_UNREADABLE"
COLLISION = "COLLISION"
STALE = "STALE"
NO_DELTA = "NO_MATERIAL_DELTA"
MATERIAL = "MATERIAL_ROUTE"


def _sha(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return parsed.astimezone(timezone.utc)


def _id(value: str) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


@dataclass(frozen=True)
class PointerContext:
    """Bound D0 and PM coordinates supplied by a future trusted ingress adapter."""

    event_id: str
    attempt_id: str
    owner_ref: str
    referent_type: str
    revision: str
    expected_sha256: str
    claim_ref: str
    packet_ref: str
    packet_sha256: str
    queue_ref: str
    producer_id: str
    receiver_ref: str
    source_cut: str
    expires_at: str
    way_home: tuple[str, ...]
    subject: str = SUBJECT

    def valid(self) -> bool:
        identifiers = (self.event_id, self.attempt_id, self.owner_ref, self.referent_type,
                       self.revision, self.claim_ref,
                       self.packet_ref, self.queue_ref, self.producer_id, self.receiver_ref, self.source_cut)
        try:
            _utc(self.expires_at)
        except (TypeError, ValueError):
            return False
        return (self.subject == SUBJECT and all(_id(v) for v in identifiers)
                and bool(HEX64.fullmatch(self.expected_sha256))
                and bool(HEX64.fullmatch(self.packet_sha256))
                and 1 <= len(self.way_home) <= 8 and all(_id(v) for v in self.way_home))


@dataclass(frozen=True)
class PointerRecord:
    schema: str
    closure_id: str
    event_id: str
    attempt_id: str
    owner_ref: str
    referent_type: str
    revision: str
    expected_sha256: str
    claim_ref: str
    packet_ref: str
    packet_sha256: str
    queue_ref: str
    producer_id: str
    receiver_ref: str
    source_cut: str
    expires_at: str
    way_home: tuple[str, ...]

    @property
    def content_sha256(self) -> str:
        return _sha(asdict(self))


@dataclass(frozen=True)
class ChannelIdentity:
    channel: str
    principal: str
    authenticated: bool
    append_allowed: bool
    read_allowed: bool


@dataclass(frozen=True)
class Readback:
    record: object
    content_sha256: str
    producer_principal: str
    revision_token: str


@dataclass(frozen=True)
class PMOwnerCut:
    owner_ref: str
    claim_ref: str
    packet_ref: str
    packet_sha256: str
    queue_ref: str
    revision: str
    relation_to_hint: str  # SAME | SUPERSEDED; opaque revisions are never ordered here
    material_sha256: str
    source_cut: str
    committed_readback: bool
    authenticated: bool
    material_ready: bool


@dataclass(frozen=True)
class X9Disposition:
    schema: str
    event_id: str
    attempt_id: str
    pointer_sha256: str
    owner_revision: str
    owner_material_sha256: str
    disposition: str
    reason: str
    work_consumed: bool = False
    effect: str = "NONE_CLAIMED"

    @property
    def content_sha256(self) -> str:
        return _sha(asdict(self))


@dataclass(frozen=True)
class Result:
    state: str
    reason: str
    event_id: str | None = None
    pointer_sha256: str | None = None
    disposition: X9Disposition | None = None


class ChannelPort(Protocol):
    # Implementations must be host-authenticated, have an event-ID uniqueness
    # guard, and get Readback from a fresh provider read, never an append ACK
    # or a row address. This candidate supplies no implementation.
    def identity(self) -> ChannelIdentity: ...
    def append_pointer_once(self, record: PointerRecord) -> str: ...  # ACCEPTED | NOT_SENT | UNKNOWN_SEND
    def read_pointer_by_event_id(self, event_id: str) -> Readback | None: ...
    def append_disposition_once(self, record: X9Disposition) -> str: ...
    def read_disposition_by_event_id(self, event_id: str) -> Readback | None: ...


class PMOwnerReadPort(Protocol):
    def read_current(self, claim_ref: str, packet_ref: str, queue_ref: str) -> PMOwnerCut: ...


class X9ChannelIngress:
    """Explicit calls only. There is no subscription, scheduler, model or live host binding."""

    def __init__(self, *, enabled: bool = False, channel: ChannelPort | None = None,
                 pm_reader: PMOwnerReadPort | None = None, producer_principal: str = "",
                 x9_principal: str = "", x9_session_ref: str = "") -> None:
        self.enabled, self.channel, self.pm_reader = enabled, channel, pm_reader
        self.producer_principal, self.x9_principal = producer_principal, x9_principal
        self.x9_session_ref = x9_session_ref
        # Same-process guard only. Cross-process uniqueness belongs to the
        # authenticated channel port's append-once contract.
        self._uncertain_pointers: dict[str, str] = {}
        self._uncertain_dispositions: dict[str, str] = {}

    def _identity(self, *, producer: bool) -> bool:
        if not self.enabled or self.channel is None:
            return False
        try:
            identity = self.channel.identity()
        except Exception:
            return False
        principal = self.producer_principal if producer else self.x9_principal
        return (identity.channel == CHANNEL and identity.authenticated and _id(principal)
                and _id(self.x9_session_ref)
                and identity.principal == principal and identity.append_allowed and identity.read_allowed)

    @staticmethod
    def _pointer_readback(readback: Readback | None, record: PointerRecord, producer: str) -> bool:
        return (isinstance(readback, Readback) and isinstance(readback.record, PointerRecord)
                and readback.record == record and readback.content_sha256 == record.content_sha256
                and readback.producer_principal == producer and _id(readback.revision_token))

    @staticmethod
    def _disposition_readback(readback: Readback | None, record: X9Disposition, x9: str) -> bool:
        return (isinstance(readback, Readback) and isinstance(readback.record, X9Disposition)
                and readback.record == record and readback.content_sha256 == record.content_sha256
                and readback.producer_principal == x9 and _id(readback.revision_token))

    def deposit(self, closure: ClosureRecord, context: PointerContext, *, now: datetime) -> Result:
        if not self._identity(producer=True):
            return Result(REFINE, "AUTHENTICATED_CHANNEL_APPEND_READBACK_PORT_MISSING")
        if not isinstance(context, PointerContext) or not context.valid():
            return Result(HOLD, "INVALID_POINTER_CONTEXT")
        if now.tzinfo is None or now.astimezone(timezone.utc) > _utc(context.expires_at):
            return Result(HOLD, "EXPIRED_OR_INVALID_CLOCK", context.event_id)
        if (not isinstance(closure, ClosureRecord) or closure.disposition != "ACK_READ"
                or closure.receipt_stage != "READ" or closure.work_consumed or closure.effect != "NONE_CLAIMED"
                or closure.authority != "NONE" or closure.is_truth_store or closure.activation.wake_bound
                or closure.event_id != context.event_id or closure.owner_ref != context.owner_ref
                or closure.referent_type != context.referent_type or closure.referent_id != context.claim_ref
                or closure.revision_after != context.revision or closure.way_home != context.way_home
                or closure.observed_sha256 != context.expected_sha256):
            return Result(HOLD, "CLOSURE_D0_BINDING_UNPROVEN", context.event_id)
        record = PointerRecord(
            SCHEMA, closure.closure_id, context.event_id, context.attempt_id, context.owner_ref,
            context.referent_type,
            context.revision, context.expected_sha256, context.claim_ref, context.packet_ref,
            context.packet_sha256, context.queue_ref, context.producer_id, context.receiver_ref,
            context.source_cut,
            context.expires_at, context.way_home)
        if context.producer_id != self.producer_principal or context.receiver_ref != self.x9_session_ref:
            return Result(HOLD, "PRODUCER_OR_RECEIVER_BINDING_MISMATCH", context.event_id)
        try:
            prior = self.channel.read_pointer_by_event_id(record.event_id)
        except Exception:
            return Result(HOLD, "CHANNEL_LOOKUP_UNAVAILABLE", record.event_id)
        if prior is not None:
            if self._pointer_readback(prior, record, self.producer_principal):
                return Result("DEPOSITED_READBACK", "DUPLICATE_SAME_EVENT", record.event_id, record.content_sha256)
            return Result(COLLISION, "EVENT_ID_CONTENT_CONFLICT", record.event_id)
        uncertain = self._uncertain_pointers.get(record.event_id)
        if uncertain is not None:
            if uncertain != record.content_sha256:
                return Result(COLLISION, "EVENT_ID_CONTENT_CONFLICT", record.event_id)
            return Result(HOLD, "UNKNOWN_SEND_NO_REPLAY", record.event_id)
        try:
            send_state = self.channel.append_pointer_once(record)
        except Exception:
            send_state = "UNKNOWN_SEND"
        # Even after UNKNOWN_SEND, reconcile by event ID and exact bytes; never send again.
        try:
            observed = self.channel.read_pointer_by_event_id(record.event_id)
        except Exception:
            observed = None
        if self._pointer_readback(observed, record, self.producer_principal):
            return Result("DEPOSITED_READBACK", "EXACT_EVENT_READBACK", record.event_id, record.content_sha256)
        if observed is not None:
            return Result(COLLISION, "EVENT_ID_CONTENT_CONFLICT", record.event_id)
        if send_state != "NOT_SENT":
            self._uncertain_pointers[record.event_id] = record.content_sha256
        return Result(HOLD, "UNKNOWN_SEND_NO_REPLAY" if send_state != "NOT_SENT" else "NOT_SENT_NO_REPLAY",
                      record.event_id)

    def consume(self, event_id: str | None, *, now: datetime, prior_material_sha256: str | None = None) -> Result:
        # A missed D0 is recovered by the ordinary X9 owner pulse, not claimed as delivery.
        if event_id is None:
            return Result(HOLD, "MISSED_D0_OWNER_PULSE_REQUIRED")
        if not self._identity(producer=False):
            return Result(REFINE, "AUTHENTICATED_X9_CHANNEL_READ_DISPOSITION_PORT_MISSING", event_id)
        if self.pm_reader is None:
            return Result(REFINE, "AUTHENTICATED_PM_OWNER_REREAD_PORT_MISSING", event_id)
        try:
            readback = self.channel.read_pointer_by_event_id(event_id)
        except Exception:
            return Result(HOLD, "CHANNEL_READ_UNAVAILABLE", event_id)
        if readback is None or not isinstance(readback.record, PointerRecord):
            return Result(HOLD, "POINTER_NOT_READBACK", event_id)
        pointer = readback.record
        if (pointer.schema != SCHEMA or pointer.event_id != event_id
                or pointer.receiver_ref != self.x9_session_ref
                or not self._pointer_readback(readback, pointer, self.producer_principal)):
            return Result(COLLISION, "POINTER_READBACK_IDENTITY_OR_HASH", event_id)
        if now.tzinfo is None or now.astimezone(timezone.utc) > _utc(pointer.expires_at):
            return Result(HOLD, "EXPIRED_POINTER_OWNER_PULSE_REQUIRED", event_id)
        try:
            prior = self.channel.read_disposition_by_event_id(event_id)
        except Exception:
            return Result(HOLD, "DISPOSITION_LOOKUP_UNAVAILABLE", event_id)
        if prior is not None:
            disposition = prior.record if isinstance(prior, Readback) else None
            if (isinstance(disposition, X9Disposition)
                    and disposition.schema == DISPOSITION_SCHEMA
                    and disposition.event_id == event_id
                    and disposition.attempt_id == pointer.attempt_id
                    and disposition.pointer_sha256 == pointer.content_sha256
                    and disposition.disposition in {STALE, NO_DELTA, MATERIAL}
                    and _id(disposition.owner_revision)
                    and isinstance(disposition.owner_material_sha256, str)
                    and HEX64.fullmatch(disposition.owner_material_sha256) is not None
                    and _id(disposition.reason)
                    and disposition.work_consumed is False
                    and disposition.effect == "NONE_CLAIMED"
                    and self._disposition_readback(prior, disposition, self.x9_principal)):
                return Result("ALREADY_DISPOSED", "SAME_EVENT_DISPOSITION", event_id, pointer.content_sha256,
                              disposition)
            return Result(COLLISION, "DISPOSITION_EVENT_ID_CONFLICT", event_id)
        try:
            cut = self.pm_reader.read_current(pointer.claim_ref, pointer.packet_ref, pointer.queue_ref)
        except Exception:
            return Result(HOLD, "PM_OWNER_REREAD_UNAVAILABLE", event_id)
        if not isinstance(cut, PMOwnerCut) or not cut.authenticated or not cut.committed_readback:
            return Result(HOLD, "PM_OWNER_CUSTODY_UNPROVEN", event_id)
        if (cut.owner_ref != pointer.owner_ref or cut.claim_ref != pointer.claim_ref
                or cut.packet_ref != pointer.packet_ref or cut.queue_ref != pointer.queue_ref
                or not HEX64.fullmatch(cut.packet_sha256) or not HEX64.fullmatch(cut.material_sha256)
                or not _id(cut.revision) or not _id(cut.source_cut)):
            return Result(COLLISION, "PM_OWNER_IDENTITY_OR_CUT_CONFLICT", event_id)
        if cut.relation_to_hint == "SUPERSEDED":
            disposition, reason = STALE, "PM_OWNER_SUPERSEDED_HINT"
        elif cut.relation_to_hint != "SAME":
            return Result(HOLD, "PM_REVISION_RELATION_UNKNOWN", event_id)
        elif (cut.revision != pointer.revision or cut.packet_sha256 != pointer.packet_sha256
              or cut.source_cut != pointer.source_cut):
            return Result(COLLISION, "PM_SAME_REVISION_CONTENT_CONFLICT", event_id)
        elif not cut.material_ready or (prior_material_sha256 is not None
                                        and cut.material_sha256 == prior_material_sha256):
            disposition, reason = NO_DELTA, "CURRENT_OWNER_HAS_NO_NEW_MATERIAL"
        else:
            disposition, reason = MATERIAL, "NEW_CURRENT_PM_MATERIAL"
        outcome = X9Disposition(DISPOSITION_SCHEMA, event_id, pointer.attempt_id, pointer.content_sha256,
                                cut.revision, cut.material_sha256, disposition, reason)
        uncertain = self._uncertain_dispositions.get(event_id)
        if uncertain is not None:
            if uncertain != outcome.content_sha256:
                return Result(COLLISION, "DISPOSITION_EVENT_ID_CONFLICT", event_id)
            return Result(HOLD, "DISPOSITION_UNKNOWN_SEND_NO_REPLAY", event_id)
        try:
            self.channel.append_disposition_once(outcome)
        except Exception:
            pass  # UNKNOWN_SEND: exactly one append attempt; only reconcile below.
        try:
            observed = self.channel.read_disposition_by_event_id(event_id)
        except Exception:
            observed = None
        if self._disposition_readback(observed, outcome, self.x9_principal):
            return Result("DISPOSITION_READBACK", reason, event_id, pointer.content_sha256, outcome)
        if observed is not None:
            return Result(COLLISION, "DISPOSITION_EVENT_ID_CONFLICT", event_id)
        self._uncertain_dispositions[event_id] = outcome.content_sha256
        return Result(HOLD, "DISPOSITION_UNKNOWN_SEND_NO_REPLAY", event_id)
