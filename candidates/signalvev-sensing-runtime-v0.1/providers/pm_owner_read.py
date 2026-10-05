"""Server-side PM ready-hint reader. Disabled until PM supplies trusted ports.

This module performs authorization, validates one owner-produced committed
claim/packet/queue record, and judges a fresh current reread. It cannot turn
three unrelated Sheet reads into an atomic PM transaction. The injected store
must be the PM owner's committed, single-snapshot provider; its production
implementation, credentials and ACL are not supplied by this candidate.

The client-side PmOwnerReadPort adapter is intentionally outside this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, fields
from typing import Protocol


_SHA = re.compile(r"[0-9a-f]{64}\Z")
_READY = frozenset({"BIND_AVAILABLE", "MATERIAL_READY"})
_ACTIONS = frozenset({"read_receipt", "read_current"})


class PmProviderError(ValueError):
    """A typed fail-closed provider result; never contains a credential."""


@dataclass(frozen=True)
class AuthenticatedPmPrincipal:
    principal_ref: str
    owner_ref: str
    audience: str


class PmCredentialPort(Protocol):
    """Host-owned verification of the presented secret and exact PM read ACL."""

    def authenticate_and_authorize(
        self, credential: str, *, owner_ref: str, audience: str, action: str
    ) -> AuthenticatedPmPrincipal | None: ...


@dataclass(frozen=True)
class PmReadyRecord:
    """One committed owner projection, obtained under one PM snapshot revision.

    The owner store, not the caller, assigns receipt/event/referent IDs, sequence,
    revision, commit/readback references and the immutable packet SHA-256.
    ``snapshot_sha256`` hashes the canonical public fields below, excluding
    itself. It detects representation drift; it does not authenticate PM.
    """

    owner_ref: str
    receipt_ref: str
    event_id: str
    referent_id: str
    owner_seq: int
    provider_revision: int
    revision_after: str
    revision_before: str | None
    claim_ref: str
    packet_ref: str
    queue_ref: str
    claim_revision: str
    packet_revision: str
    queue_revision: str
    packet_sha256: str
    ready_state: str
    snapshot_ref: str
    snapshot_sha256: str
    commit_ref: str
    readback_ref: str
    observed_at: str
    way_home: tuple[str, ...]


class PmAtomicSnapshotPort(Protocol):
    """PM-owned provider methods, each returning one committed snapshot.

    No implementation is bundled. A Sheets batchGet of separate rows does not
    satisfy this contract without an owner transaction/revision and ACL proof.
    """

    def read_receipt(
        self, receipt_ref: str, *, owner_ref: str, principal_ref: str
    ) -> PmReadyRecord | None: ...

    def read_current(
        self, referent_id: str, *, owner_ref: str, principal_ref: str
    ) -> PmReadyRecord | None: ...


def snapshot_sha256(record: PmReadyRecord) -> str:
    """Deterministic local consistency hash, never a proof of owner custody."""

    subject = {field.name: getattr(record, field.name) for field in fields(record)
               if field.name != "snapshot_sha256"}
    raw = json.dumps(subject, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise PmProviderError(code)


def _ref(value: object, prefix: str | None = None) -> bool:
    return isinstance(value, str) and bool(value) and not value.isspace() \
        and "\n" not in value and "\r" not in value \
        and (prefix is None or value.startswith(prefix) and len(value) > len(prefix))


def _valid(record: object, owner_ref: str) -> PmReadyRecord:
    _need(type(record) is PmReadyRecord, "PM_ATOMIC_RECORD_MISSING_OR_INVALID")
    assert isinstance(record, PmReadyRecord)
    _need(record.owner_ref == owner_ref, "PM_OWNER_MISMATCH")
    for value, prefix in ((record.receipt_ref, "pm-receipt:"),
                          (record.event_id, "pm-event:"),
                          (record.referent_id, "pm-ready:"),
                          (record.snapshot_ref, "pm-snapshot:")):
        _need(_ref(value, prefix), "PM_STABLE_ID_REQUIRED")
    _need(all(_ref(x) for x in (record.claim_ref, record.packet_ref,
                               record.queue_ref, record.commit_ref,
                               record.readback_ref, record.revision_after,
                               record.observed_at)), "PM_RECORD_FIELD_MISSING")
    _need(record.revision_before is None or _ref(record.revision_before),
          "PM_RECORD_FIELD_INVALID")
    _need(type(record.owner_seq) is int and record.owner_seq >= 1 and
          type(record.provider_revision) is int and record.provider_revision >= 0,
          "PM_OWNER_SEQUENCE_INVALID")
    _need(isinstance(record.ready_state, str) and record.ready_state in _READY,
          "PM_NOT_READY")
    _need(record.claim_revision == record.packet_revision ==
          record.queue_revision == record.revision_after,
          "PM_SPLIT_REVISION")
    _need(isinstance(record.packet_sha256, str) and _SHA.fullmatch(record.packet_sha256)
          is not None, "PM_PACKET_SHA_INVALID")
    _need(type(record.way_home) is tuple and bool(record.way_home) and
          all(_ref(x) for x in record.way_home), "PM_WAY_HOME_REQUIRED")
    _need(isinstance(record.snapshot_sha256, str) and
          record.snapshot_sha256 == snapshot_sha256(record),
          "PM_SNAPSHOT_HASH_MISMATCH")
    return record


class ServerPmOwnerProvider:
    """Callable PM-side reader; no caller-supplied authentication/currentness flags.

    Host must bind exact PM-owned credential and atomic snapshot implementations.
    Without them, every call fails before reading; `enabled` defaults to False.
    No token is logged, cached or included in a returned record.
    """

    def __init__(self, *, owner_ref: str, audience: str,
                 credentials: PmCredentialPort | None = None,
                 snapshots: PmAtomicSnapshotPort | None = None,
                 enabled: bool = False) -> None:
        _need(_ref(owner_ref) and _ref(audience), "PM_PROVIDER_CONFIG_INVALID")
        self._owner_ref = owner_ref
        self._audience = audience
        self._credentials = credentials
        self._snapshots = snapshots
        self._enabled = enabled is True
        self._seen: dict[str, str] = {}  # defense in depth; owner owes durable uniqueness
        self._seen_lock = threading.Lock()

    def _authorize(self, credential: str, action: str) -> str:
        _need(self._enabled and self._credentials is not None and
              self._snapshots is not None, "PM_PROVIDER_PORT_UNBOUND")
        _need(_ref(credential) and action in _ACTIONS, "PM_CREDENTIAL_INVALID")
        try:
            principal = self._credentials.authenticate_and_authorize(
                credential, owner_ref=self._owner_ref, audience=self._audience,
                action=action)
        except Exception as exc:
            raise PmProviderError("PM_AUTH_UNAVAILABLE") from exc
        _need(type(principal) is AuthenticatedPmPrincipal and
              principal.owner_ref == self._owner_ref and
              principal.audience == self._audience and _ref(principal.principal_ref),
              "PM_AUTH_DENIED")
        return principal.principal_ref

    def _current(self, referent_id: str, principal_ref: str) -> PmReadyRecord:
        try:
            result = self._snapshots.read_current(
                referent_id, owner_ref=self._owner_ref, principal_ref=principal_ref)
        except Exception as exc:
            raise PmProviderError("PM_OWNER_READ_UNAVAILABLE") from exc
        record = _valid(result, self._owner_ref)
        _need(record.referent_id == referent_id, "PM_REFERENT_MISMATCH")
        self._remember(record)
        return record

    def _remember(self, record: PmReadyRecord) -> None:
        with self._seen_lock:
            prior = self._seen.get(record.event_id)
            _need(prior is None or prior == record.snapshot_sha256,
                  "PM_EVENT_ID_CONTENT_CONFLICT")
            self._seen[record.event_id] = record.snapshot_sha256

    def read_committed_ready_hint(self, receipt_ref: str, *,
                                  expected_owner_ref: str,
                                  credential: str) -> PmReadyRecord:
        _need(expected_owner_ref == self._owner_ref and
              _ref(receipt_ref, "pm-receipt:"), "PM_REQUEST_INVALID")
        principal = self._authorize(credential, "read_receipt")
        try:
            result = self._snapshots.read_receipt(
                receipt_ref, owner_ref=self._owner_ref, principal_ref=principal)
        except Exception as exc:
            raise PmProviderError("PM_OWNER_READ_UNAVAILABLE") from exc
        record = _valid(result, self._owner_ref)
        _need(record.receipt_ref == receipt_ref, "PM_RECEIPT_MISMATCH")
        current_principal = self._authorize(credential, "read_current")
        current = self._current(record.referent_id, current_principal)
        _need((current.revision_after, current.owner_seq, current.snapshot_sha256)
              == (record.revision_after, record.owner_seq, record.snapshot_sha256),
              "PM_SNAPSHOT_STALE_OR_CONFLICT")
        self._remember(record)
        return record

    def reread_ready_hint(self, referent_id: str, *, expected_owner_ref: str,
                          expected_revision: str, expected_seq: int,
                          expected_sha256: str, credential: str) -> tuple[str, PmReadyRecord]:
        _need(expected_owner_ref == self._owner_ref and _ref(referent_id, "pm-ready:")
              and _ref(expected_revision) and type(expected_seq) is int and
              expected_seq >= 1 and isinstance(expected_sha256, str) and
              _SHA.fullmatch(expected_sha256) is not None,
              "PM_REREAD_REQUEST_INVALID")
        principal = self._authorize(credential, "read_current")
        current = self._current(referent_id, principal)
        if current.owner_seq < expected_seq:
            return "UNKNOWN", current
        if current.owner_seq == expected_seq:
            relation = "SAME" if (current.revision_after == expected_revision
                                  and current.snapshot_sha256 == expected_sha256) \
                else "UNKNOWN"
            return relation, current
        return "SUPERSEDED", current

    def recover_current_ready_hint(self, referent_id: str, *,
                                   after_seq: int, credential: str) -> PmReadyRecord | None:
        """Bounded missed-D0 recovery: one fresh owner read for one referent."""

        _need(_ref(referent_id, "pm-ready:") and type(after_seq) is int and
              after_seq >= 0, "PM_RECOVERY_REQUEST_INVALID")
        principal = self._authorize(credential, "read_current")
        current = self._current(referent_id, principal)
        return current if current.owner_seq > after_seq else None
