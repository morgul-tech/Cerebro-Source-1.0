"""1. OWNER EVENT INPUT. An owner-local event is accepted ONLY after the source reports commit + readback.

The event keeps referent/event identity, the revision/currentness basis, provenance and Way Home. Revision
ids are opaque strings (they are never ordered by this runtime). owner_seq is an owner-assigned, per-referent
monotonic integer used only as a cheap local staleness hint; the owner's own reread stays the judge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .model import (CHANGE_CLASSES, HEX64, MAX_INLINE_FIELDS, MAX_INLINE_VALUE_CHARS, SensingError, is_id)

COMMITTED = "COMMITTED_READBACK"


@dataclass(frozen=True)
class OwnerEvent:
    event_id: str
    owner_ref: str
    referent_type: str
    referent_id: str
    owner_seq: int
    revision_after: str
    revision_before: str | None
    change_class: str
    delta_kind: str                       # "INLINE" | "POINTER"
    expected_sha256: str                  # owner's fingerprint of the state/section the event is about
    inline_fields: Mapping[str, Any] | None
    pointer_ref: str | None
    commit_readback_ref: str
    commit_observed_at: str
    source_ref: str
    way_home: tuple[str, ...]
    requires_ack: bool = False


def _need(cond: bool, code: str, detail: str = "") -> None:
    if not cond:
        raise SensingError(code, detail)


def accept_owner_event(raw: Mapping[str, Any]) -> OwnerEvent:
    """Validate a raw owner-event mapping. Raises SensingError; nothing is sent for a rejected event."""
    _need(isinstance(raw, Mapping), "OWNER_EVENT_NOT_A_MAPPING")
    commit = raw.get("commit")
    _need(isinstance(commit, Mapping) and commit.get("state") == COMMITTED
          and is_id(commit.get("readback_ref")) and isinstance(commit.get("observed_at"), str),
          "OWNER_EVENT_NOT_COMMITTED", "source must report commit AND readback (state, readback_ref, observed_at)")
    for key in ("event_id", "owner_ref", "source_ref"):
        _need(is_id(raw.get(key)), "OWNER_EVENT_IDENTITY_INVALID", key)
    ref = raw.get("referent")
    _need(isinstance(ref, Mapping) and is_id(ref.get("type")) and is_id(ref.get("id")),
          "OWNER_EVENT_IDENTITY_INVALID", "referent")
    seq = raw.get("owner_seq")
    _need(type(seq) is int and seq >= 1, "OWNER_EVENT_SEQ_INVALID")
    basis = raw.get("revision_basis")
    _need(isinstance(basis, Mapping) and is_id(basis.get("after"))
          and (basis.get("before") is None or is_id(basis.get("before"))), "OWNER_EVENT_REVISION_BASIS_INVALID")
    _need(raw.get("change_class") in CHANGE_CLASSES, "OWNER_EVENT_CHANGE_CLASS_INVALID")
    way = raw.get("way_home")
    _need(isinstance(way, (list, tuple)) and way and all(is_id(w) for w in way), "OWNER_EVENT_WAY_HOME_MISSING")
    delta = raw.get("delta")
    _need(isinstance(delta, Mapping) and delta.get("kind") in ("INLINE", "POINTER"), "OWNER_EVENT_DELTA_INVALID")
    _need(isinstance(delta.get("expected_sha256"), str) and bool(HEX64.match(delta["expected_sha256"])),
          "OWNER_EVENT_DELTA_INVALID", "expected_sha256")
    fields = pointer = None
    if delta["kind"] == "INLINE":
        fields = delta.get("fields")
        _need(isinstance(fields, Mapping) and 0 < len(fields) <= MAX_INLINE_FIELDS
              and set(delta) == {"kind", "expected_sha256", "fields"}, "OWNER_EVENT_DELTA_INVALID", "inline fields")
        for k, v in fields.items():
            _need(is_id(k) and (v is None or isinstance(v, (bool, int))
                                or (isinstance(v, str) and len(v) <= MAX_INLINE_VALUE_CHARS)),
                  "OWNER_EVENT_DELTA_NOT_SMALL_FLAT", k)
        fields = dict(fields)
    else:
        pointer = delta.get("ref")
        _need(is_id(pointer) and set(delta) == {"kind", "expected_sha256", "ref"}, "OWNER_EVENT_DELTA_INVALID", "pointer")
    return OwnerEvent(
        event_id=raw["event_id"], owner_ref=raw["owner_ref"], referent_type=ref["type"], referent_id=ref["id"],
        owner_seq=seq, revision_after=basis["after"], revision_before=basis.get("before"),
        change_class=raw["change_class"], delta_kind=delta["kind"], expected_sha256=delta["expected_sha256"],
        inline_fields=fields, pointer_ref=pointer, commit_readback_ref=commit["readback_ref"],
        commit_observed_at=commit["observed_at"], source_ref=raw["source_ref"], way_home=tuple(way),
        requires_ack=bool(raw.get("requires_ack", False)),
    )
