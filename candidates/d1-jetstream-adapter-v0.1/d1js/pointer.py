"""Strict typed D1 pointer (wire JSON). The pointer is a reference to owner truth, never the material itself."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA = "cerebro.d1.pointer"
VERSION = 1
CLASS = "D1"
SUBJECT = "cerebro.test.d1.pointer"
KINDS = ("HINT", "DECISION", "DISSENT", "AUTHORITY_RECEIPT", "EFFECT_RECEIPT")
COALESCIBLE_KINDS = frozenset({"HINT"})
MAX_POINTER_BYTES = 4096
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_KEYS = frozenset({"schema", "version", "tenant_ref", "workspace_ref", "project_ref", "owner_ref", "owner_event_key",
                   "owner_revision", "event_fingerprint", "referent", "receiver_ref", "message_id", "dedupe_key",
                   "class", "kind", "created_at", "expires_at", "body_sha256"})


class PointerReject(ValueError):
    """Typed syntactic/semantic reject of the raw pointer bytes (never a statement about the owner obligation)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str) -> datetime:
    if not isinstance(value, str) or not _TS.match(value):
        raise ValueError("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def message_identity(tenant: str, workspace: str, project: str, receiver: str, owner_event_key: str,
                     owner_revision: int) -> str:
    """Stable scope+identity => Nats-Msg-Id and dedupe key. Same event+revision+scope => same id, forever."""
    body = canonical({"t": tenant, "w": workspace, "p": project, "r": receiver, "k": owner_event_key,
                      "v": owner_revision})
    return "d1:" + sha256_hex(body)[:40]


@dataclass(frozen=True)
class D1Pointer:
    tenant_ref: str
    workspace_ref: str
    project_ref: str
    owner_ref: str
    owner_event_key: str
    owner_revision: int
    event_fingerprint: str
    referent_type: str
    referent_id: str
    receiver_ref: str
    message_id: str
    kind: str
    created_at: str
    expires_at: str
    raw_sha256: str               # digest of the exact wire bytes

    @property
    def scope(self) -> tuple[str, str, str, str]:
        return (self.tenant_ref, self.workspace_ref, self.project_ref, self.receiver_ref)

    @property
    def coalesce_key(self) -> tuple[str, ...]:
        return (self.owner_ref, self.referent_type, self.referent_id, *self.scope)


def build_pointer(*, tenant_ref: str, workspace_ref: str, project_ref: str, owner_ref: str, owner_event_key: str,
                  owner_revision: int, event_fingerprint: str, referent_type: str, referent_id: str,
                  receiver_ref: str, kind: str, created_at: datetime, ttl_seconds: int) -> bytes:
    msg_id = message_identity(tenant_ref, workspace_ref, project_ref, receiver_ref, owner_event_key, owner_revision)
    body = {"schema": SCHEMA, "version": VERSION, "tenant_ref": tenant_ref, "workspace_ref": workspace_ref,
            "project_ref": project_ref, "owner_ref": owner_ref, "owner_event_key": owner_event_key,
            "owner_revision": owner_revision, "event_fingerprint": event_fingerprint,
            "referent": {"type": referent_type, "id": referent_id}, "receiver_ref": receiver_ref,
            "message_id": msg_id, "dedupe_key": msg_id, "class": CLASS, "kind": kind,
            "created_at": iso(created_at), "expires_at": iso(created_at + timedelta(seconds=ttl_seconds))}
    body["body_sha256"] = sha256_hex(canonical(body))
    raw = canonical(body)
    parse_pointer(raw, max_ttl_seconds=ttl_seconds)          # the producer obeys its own schema
    return raw


def _no_dupes(pairs):
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise PointerReject("REJECT_SCHEMA", "duplicate key")
        out[k] = v
    return out


def _constant(name: str):
    raise PointerReject("REJECT_SCHEMA", f"non-finite {name}")


def parse_pointer(raw: bytes, *, max_ttl_seconds: int) -> D1Pointer:
    """Strict decode. Raises PointerReject(REJECT_SCHEMA | REJECT_DIGEST | REJECT_IDENTITY | REJECT_TTL_SHAPE)."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_POINTER_BYTES:
        raise PointerReject("REJECT_SCHEMA", "size")
    try:
        doc = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_constant)
    except PointerReject:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise PointerReject("REJECT_SCHEMA", "not strict JSON") from None
    if not isinstance(doc, dict) or set(doc) != _KEYS:
        raise PointerReject("REJECT_SCHEMA", "keys")
    if doc["schema"] != SCHEMA or type(doc["version"]) is not int or doc["version"] != VERSION or doc["class"] != CLASS:
        raise PointerReject("REJECT_SCHEMA", "schema/version/class")
    if doc["kind"] not in KINDS:
        raise PointerReject("REJECT_SCHEMA", "kind")
    for key in ("tenant_ref", "workspace_ref", "project_ref", "owner_ref", "owner_event_key", "receiver_ref",
                "message_id", "dedupe_key"):
        if not (isinstance(doc[key], str) and _ID.match(doc[key])):
            raise PointerReject("REJECT_SCHEMA", key)
    ref = doc["referent"]
    if not (isinstance(ref, dict) and set(ref) == {"type", "id"} and all(isinstance(ref[k], str) and _ID.match(ref[k])
                                                                         for k in ("type", "id"))):
        raise PointerReject("REJECT_SCHEMA", "referent")
    rev = doc["owner_revision"]
    if type(rev) is not int or rev < 1:
        raise PointerReject("REJECT_SCHEMA", "owner_revision")
    if not (isinstance(doc["event_fingerprint"], str) and _HEX64.match(doc["event_fingerprint"])
            and isinstance(doc["body_sha256"], str) and _HEX64.match(doc["body_sha256"])):
        raise PointerReject("REJECT_SCHEMA", "digest shape")
    body = {k: v for k, v in doc.items() if k != "body_sha256"}
    if sha256_hex(canonical(body)) != doc["body_sha256"]:
        raise PointerReject("REJECT_DIGEST", "body_sha256 does not match the pointer body")
    expected_id = message_identity(doc["tenant_ref"], doc["workspace_ref"], doc["project_ref"], doc["receiver_ref"],
                                   doc["owner_event_key"], rev)
    if doc["message_id"] != expected_id or doc["dedupe_key"] != expected_id:
        raise PointerReject("REJECT_IDENTITY", "message_id/dedupe_key not the stable scope+identity")
    try:
        created, expires = parse_ts(doc["created_at"]), parse_ts(doc["expires_at"])
    except ValueError:
        raise PointerReject("REJECT_SCHEMA", "timestamps") from None
    if not created < expires or (expires - created).total_seconds() > max_ttl_seconds:
        raise PointerReject("REJECT_TTL_SHAPE", "expires_at must follow created_at within the configured TTL")
    return D1Pointer(doc["tenant_ref"], doc["workspace_ref"], doc["project_ref"], doc["owner_ref"],
                     doc["owner_event_key"], rev, doc["event_fingerprint"], ref["type"], ref["id"],
                     doc["receiver_ref"], doc["message_id"], doc["kind"], doc["created_at"], doc["expires_at"],
                     sha256_hex(bytes(raw)))


def is_expired(pointer: D1Pointer, now: datetime) -> bool:
    return now >= parse_ts(pointer.expires_at)
