"""Fail-closed adapter for the existing X9 receiver-session channel port.

This file defines the narrow host API the receiver still needs. It is not a
Google Sheets client, credential store, pulse scheduler, or live binding. The
existing X9 heartbeat selects a pointer from PROSJEKTMANN_CHANNEL. Its actual
host is the Codex workspace C:/Users/morgu/Documents/Codex/2026-09-27/goo,
with pulse C:/Users/morgu/.codex/automations/forskningshuset-r1-r2-videref-ring/
automation.toml. This candidate does not mutate or register that live pulse.
Production qualification requires the host to implement
``AuthenticatedX9SessionAPI`` with provider-authenticated identity, atomic
append-once by event ID, and fresh readback (including provider revision).

The receiver identity comes from an explicit host-owned role assignment and a
fresh authenticated current-session read; this adapter contains no principal
or session aliases. ``enabled`` is false by default. A test double can check
the interface contract, but cannot establish provider custody or live effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from adapters.x9_channel_ingress import (
    CHANNEL,
    DISPOSITION_SCHEMA,
    HEX64,
    ID,
    MATERIAL,
    HOLD,
    REFINE,
    PMOwnerCut,
    PMOwnerReadPort,
    NO_DELTA,
    SCHEMA,
    STALE,
    ChannelIdentity,
    PointerRecord,
    Readback,
    Result,
    X9Disposition,
)

NOT_SENT = "NOT_SENT"
UNKNOWN_SEND = "UNKNOWN_SEND"
ACCEPTED = "ACCEPTED"

REQUIRED_SCOPES = frozenset({"pointer:read", "disposition:append", "disposition:read"})


@dataclass(frozen=True)
class ProviderSessionIdentity:
    """Must come from a host-authenticated identity endpoint, never request data."""

    channel: str
    principal: str
    session_ref: str
    authenticated: bool
    current: bool
    scopes: frozenset[str]


class AuthenticatedX9SessionAPI(Protocol):
    """Missing host-owned execution port required for production qualification.

    ``identity`` must be selected through the trusted receiver role assignment
    and report the actual authenticated current session. Principal names,
    consumer labels, request fields, and transport sessions alone cannot assign role.

    Append methods must enforce event-ID uniqueness atomically across processes.
    Read methods must perform a fresh authenticated provider read and return
    the canonical content hash, authenticated writer principal, and revision
    token. Returning an append response is never a readback receipt.
    """

    def identity(self) -> ProviderSessionIdentity: ...

    def read_pointer_by_event_id(self, event_id: str) -> Readback | None: ...

    def append_disposition_once(self, event_id: str, content_sha256: str, record: X9Disposition) -> str: ...

    def read_disposition_by_event_id(self, event_id: str) -> Readback | None: ...


class X9SessionChannelPort:
    """X9-role adapter implementing the existing ``ChannelPort`` shape."""

    def __init__(self, *, api: AuthenticatedX9SessionAPI | None = None, enabled: bool = False) -> None:
        self._api = api
        self._enabled = enabled is True

    def _session(self) -> ProviderSessionIdentity | None:
        if not self._enabled or self._api is None:
            return None
        try:
            identity = self._api.identity()
        except Exception:  # noqa: BLE001 - provider unavailability is a closed gate
            return None
        if not isinstance(identity, ProviderSessionIdentity):
            return None
        if (identity.channel != CHANNEL
                or not isinstance(identity.principal, str) or not identity.principal.strip()
                or not isinstance(identity.session_ref, str) or not identity.session_ref.strip()
                or identity.authenticated is not True
                or identity.current is not True or not isinstance(identity.scopes, frozenset)
                or any(not isinstance(scope, str) for scope in identity.scopes)
                or not REQUIRED_SCOPES.issubset(identity.scopes)):
            return None
        return identity

    def identity(self) -> ChannelIdentity:
        current = self._session()
        return ChannelIdentity(
            CHANNEL,
            current.principal if current is not None else "",
            authenticated=current is not None,
            append_allowed=current is not None and "disposition:append" in current.scopes,
            read_allowed=current is not None and "pointer:read" in current.scopes
            and "disposition:read" in current.scopes,
        )

    def append_pointer_once(self, record: PointerRecord) -> str:
        """Refuse producer writes from X9's receiver-only session.

        The bound PM/Claude producer port owns ``append_pointer_once``. A
        receiver session may not impersonate that principal; a shared provider
        API still has to qualify the independent PM sender and X9 reader ACLs.
        """

        return NOT_SENT

    def read_pointer_by_event_id(self, event_id: str) -> Readback | None:
        identity = self._session()
        if identity is None or not isinstance(event_id, str) or not ID.fullmatch(event_id):
            return None
        try:
            readback = self._api.read_pointer_by_event_id(event_id)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None
        if readback is None:
            return None
        if (not isinstance(readback, Readback) or not isinstance(readback.record, PointerRecord)
                or readback.record.schema != SCHEMA or readback.record.event_id != event_id
                or readback.record.receiver_ref != identity.session_ref
                or readback.producer_principal != readback.record.producer_id
                or readback.content_sha256 != readback.record.content_sha256
                or not isinstance(readback.revision_token, str) or not ID.fullmatch(readback.revision_token)):
            return None
        return readback

    def append_disposition_once(self, record: X9Disposition) -> str:
        identity = self._session()
        if (identity is None or not isinstance(record, X9Disposition)
                or record.schema != DISPOSITION_SCHEMA or record.work_consumed is not False
                or record.effect != "NONE_CLAIMED" or record.disposition not in {STALE, NO_DELTA, MATERIAL}
                or not ID.fullmatch(record.event_id) or not ID.fullmatch(record.attempt_id)
                or not HEX64.fullmatch(record.pointer_sha256)
                or not HEX64.fullmatch(record.owner_material_sha256)
                or not ID.fullmatch(record.owner_revision) or not ID.fullmatch(record.reason)):
            return NOT_SENT
        pointer = self.read_pointer_by_event_id(record.event_id)
        if (pointer is None or not isinstance(pointer.record, PointerRecord)
                or pointer.record.attempt_id != record.attempt_id
                or pointer.record.content_sha256 != record.pointer_sha256
                or pointer.record.receiver_ref != identity.session_ref):
            return NOT_SENT
        try:
            result = self._api.append_disposition_once(  # type: ignore[union-attr]
                record.event_id, record.content_sha256, record
            )
        except Exception:  # noqa: BLE001 - the ingress reconciles once; no retry
            return UNKNOWN_SEND
        return result if result in {ACCEPTED, NOT_SENT, UNKNOWN_SEND} else UNKNOWN_SEND

    def read_disposition_by_event_id(self, event_id: str) -> Readback | None:
        identity = self._session()
        if identity is None or not isinstance(event_id, str) or not ID.fullmatch(event_id):
            return None
        try:
            readback = self._api.read_disposition_by_event_id(event_id)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None
        if readback is None:
            return None
        if (not isinstance(readback, Readback) or not isinstance(readback.record, X9Disposition)
                or readback.record.schema != DISPOSITION_SCHEMA or readback.record.event_id != event_id
                or readback.record.work_consumed is not False or readback.record.effect != "NONE_CLAIMED"
                or readback.record.disposition not in {STALE, NO_DELTA, MATERIAL}
                or not ID.fullmatch(readback.record.attempt_id)
                or not HEX64.fullmatch(readback.record.pointer_sha256)
                or not HEX64.fullmatch(readback.record.owner_material_sha256)
                or not ID.fullmatch(readback.record.owner_revision)
                or not ID.fullmatch(readback.record.reason)
                or readback.producer_principal != identity.principal
                or readback.content_sha256 != readback.record.content_sha256
                or not isinstance(readback.revision_token, str) or not ID.fullmatch(readback.revision_token)):
            return None
        return readback


class X9PulseIngress(Protocol):
    def consume(self, event_id: str | None, *, now: datetime,
                prior_material_sha256: str | None = None) -> object: ...


def consume_current_x9_pulse(ingress: X9PulseIngress, channel: X9SessionChannelPort, *,
                             event_id: str | None, now: datetime,
                             prior_material_sha256: str | None = None,
                             pm_reader: PMOwnerReadPort | None = None,
                             recovery_scope: tuple[str, str, str] | None = None) -> object:
    """Call target for the existing pulse's durable inbox selection.

    event_id selects existing inbox data; it never supplies receiver custody.
    The authenticated channel pins custody and the ingress verifies the exact
    pointer. On missed D0, one known PM tuple may be freshly reread via the
    existing owner port. The returned cut is recovery input for the ordinary
    pulse, never a delivery/disposition/consume receipt.
    """

    if not channel.identity().authenticated:
        return Result(REFINE, "AUTHENTICATED_X9_SESSION_PORT_MISSING", event_id)
    if event_id is not None:
        if not isinstance(event_id, str) or not ID.fullmatch(event_id):
            return Result(HOLD, "INVALID_INBOX_EVENT_ID")
        return ingress.consume(event_id, now=now, prior_material_sha256=prior_material_sha256)
    if pm_reader is None or recovery_scope is None:
        return ingress.consume(None, now=now, prior_material_sha256=prior_material_sha256)
    if (not isinstance(recovery_scope, tuple) or len(recovery_scope) != 3
            or any(not isinstance(ref, str) or not ID.fullmatch(ref) for ref in recovery_scope)):
        return Result(HOLD, "INVALID_MISSED_D0_RECOVERY_SCOPE")
    try:
        cut = pm_reader.read_current(*recovery_scope)  # one bounded owner reread; no retry
    except Exception:
        return Result(HOLD, "MISSED_D0_PM_OWNER_REREAD_UNAVAILABLE")
    if (not isinstance(cut, PMOwnerCut) or cut.authenticated is not True
            or cut.committed_readback is not True
            or (cut.claim_ref, cut.packet_ref, cut.queue_ref) != recovery_scope):
        return Result(HOLD, "MISSED_D0_PM_OWNER_CUSTODY_UNPROVEN")
    return cut
