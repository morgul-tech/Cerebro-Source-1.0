"""2. LOWEST-SUFFICIENT D0 ENVELOPE and wire frame.

Wire frame = {"frame": ..., "envelope": <CEREBRO_MESSAGE_V1, UNMODIFIED, validated by v0.1>, "d0": <compact body>}.
The v0.1 envelope is strict (no extra fields), so the D0 body rides beside it and is bound to it by
envelope.payload_hash == sha256(canonical(d0)) (unkeyed: consistency, not authentication). No new protocol:
subjects/message types come from the v0.1 Subject Registry (state.delta / artifact.pointer).
The D0 never carries complete owner state: INLINE = <= 8 small flat values, POINTER = reference + hash only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import _reference as ref
from .model import (CHANGE_CLASSES, D0_VERSION, HEX64, KNOWN_D0_VERSIONS, MAX_D0_BYTES, MAX_FRAME_BYTES,
                    MAX_INLINE_FIELDS, MAX_INLINE_VALUE_CHARS, SUBJECT_ARTIFACT_POINTER, SUBJECT_MESSAGE_TYPE,
                    SUBJECT_STATE_DELTA, Hold, SensingError, canonical, is_id, iso_utc, sha256_hex)
from .owner_event import OwnerEvent

FRAME_VERSION = "sensing.frame/v0.1"
MAX_TTL_SECONDS = 3600
_D0_KEYS_BASE = {"d0_version", "event_id", "owner_ref", "referent", "owner_seq", "revision_after", "revision_before",
                 "change_class", "delta", "commit_readback_ref", "source_ref", "way_home"}


@dataclass(frozen=True)
class BuiltFrame:
    data: bytes
    envelope: Mapping[str, Any]
    d0: Mapping[str, Any]
    fingerprint: str
    subject: str


def referent_key(owner_ref: str, referent_type: str, referent_id: str) -> str:
    """Hashed scope key: ids may be long (<=160 each); evidence stores hold only the fixed-size hash."""
    return sha256_hex(f"{owner_ref}|{referent_type}|{referent_id}".encode("utf-8"))


def idempotency_hash(d0: Mapping[str, Any]) -> str:
    return sha256_hex(idempotency_key(d0).encode("utf-8"))


def content_fingerprint(d0: Mapping[str, Any]) -> str:
    """Hash of the substantive content (NOT event_id/provenance): same event_id + different content => conflict."""
    body = {k: d0[k] for k in ("owner_ref", "referent", "owner_seq", "revision_after", "revision_before",
                               "change_class", "delta")}
    return sha256_hex(canonical(body))


def idempotency_key(d0: Mapping[str, Any]) -> str:
    """Registry idempotency scopes: state.delta=referent_plus_state_revision; pointer=artifact identity + content hash."""
    delta = d0["delta"]
    if delta["kind"] == "INLINE":
        return f"{d0['owner_ref']}|{d0['referent']['type']}|{d0['referent']['id']}@{d0['revision_after']}"
    return f"{d0['owner_ref']}|{delta['ref']}#{delta['expected_sha256']}"


def d0_from_event(ev: OwnerEvent) -> dict[str, Any]:
    delta: dict[str, Any] = {"kind": ev.delta_kind, "expected_sha256": ev.expected_sha256}
    if ev.delta_kind == "INLINE":
        delta["fields"] = dict(ev.inline_fields or {})
    else:
        delta["ref"] = ev.pointer_ref
    return {
        "d0_version": D0_VERSION, "event_id": ev.event_id, "owner_ref": ev.owner_ref,
        "referent": {"type": ev.referent_type, "id": ev.referent_id}, "owner_seq": ev.owner_seq,
        "revision_after": ev.revision_after, "revision_before": ev.revision_before, "change_class": ev.change_class,
        "delta": delta, "commit_readback_ref": ev.commit_readback_ref, "source_ref": ev.source_ref,
        "way_home": list(ev.way_home),
    }


def build_frame(ev: OwnerEvent, *, now_epoch: float, ttl_seconds: int = 30) -> BuiltFrame:
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise SensingError("TTL_OUT_OF_BOUNDS", f"1..{MAX_TTL_SECONDS}")
    d0 = d0_from_event(ev)
    d0_bytes = canonical(d0)
    if len(d0_bytes) > MAX_D0_BYTES:
        raise SensingError("D0_OVER_BOUND", f"{len(d0_bytes)} > {MAX_D0_BYTES}: use a POINTER delta (never truncated)")
    subject = SUBJECT_STATE_DELTA if ev.delta_kind == "INLINE" else SUBJECT_ARTIFACT_POINTER
    envelope = {
        "message_id": ev.event_id, "message_type": SUBJECT_MESSAGE_TYPE[subject], "schema_version": "1.0.0-draft",
        "subject": subject,
        "source": {"principal_ref": ev.owner_ref, "actor_id": None, "generation": None, "workload_ref": None,
                   "machine_ref": None, "session_ref": None, "custody_ref": None},
        "target": {"referent_type": ev.referent_type, "referent_id": ev.referent_id, "state_owner_ref": ev.owner_ref},
        "issued_at": iso_utc(now_epoch), "ttl_seconds": ttl_seconds, "trace_id": ev.event_id,
        "correlation_id": ev.event_id, "causation_id": None, "idempotency_key": idempotency_key(d0),
        "authority_class": "NONE", "authority_ref": None, "effect_class": "NONE", "reply_to": None,
        "requires_ack": ev.requires_ack, "payload_ref": f"d0:{ev.event_id}", "payload_hash": sha256_hex(d0_bytes),
    }
    ref.validate_envelope(envelope)       # the sender also obeys v0.1; a bad envelope is a bug, not a wire verdict
    data = canonical({"frame": FRAME_VERSION, "envelope": envelope, "d0": d0})
    if len(data) > MAX_FRAME_BYTES:
        raise SensingError("FRAME_OVER_BOUND", f"{len(data)} > {MAX_FRAME_BYTES}")
    return BuiltFrame(data=data, envelope=envelope, d0=d0, fingerprint=content_fingerprint(d0), subject=subject)


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON number {name}")


def _no_duplicate_keys(pairs):
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _json_loads(data: bytes):
    import json
    return json.loads(data.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)


def decode_frame(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    """Receiver-side structural decode. Raises Hold(HOLD_SCHEMA|HOLD_IDENTITY). Never trusts the sender."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_FRAME_BYTES:
        raise Hold("HOLD_SCHEMA", "FRAME_OVER_BOUND_OR_NOT_BYTES")
    try:
        frame = _json_loads(bytes(data))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Hold("HOLD_SCHEMA", "FRAME_NOT_STRICT_JSON") from None
    if not isinstance(frame, dict) or set(frame) != {"frame", "envelope", "d0"} or frame["frame"] != FRAME_VERSION:
        raise Hold("HOLD_SCHEMA", "FRAME_SHAPE_OR_VERSION")
    env, d0 = frame["envelope"], frame["d0"]
    if not isinstance(env, dict) or not isinstance(d0, dict):
        raise Hold("HOLD_SCHEMA", "FRAME_SHAPE_OR_VERSION")
    return env, d0


def validate_d0(d0: Mapping[str, Any]) -> None:
    ver = ref.evaluate_schema_compatibility(compatibility_policy="exact_schema_version_or_typed_hold",
                                            declared_version=str(d0.get("d0_version")),
                                            known_versions=KNOWN_D0_VERSIONS)
    if ver["status"] != "ACCEPTED":
        raise Hold("HOLD_SCHEMA", f"D0_{ver['reason']}")
    if len(canonical(d0)) > MAX_D0_BYTES:
        raise Hold("HOLD_SCHEMA", "D0_OVER_BOUND")
    delta = d0.get("delta")
    if not isinstance(delta, dict) or delta.get("kind") not in ("INLINE", "POINTER"):
        raise Hold("HOLD_SCHEMA", "D0_DELTA_SHAPE")
    expected = _D0_KEYS_BASE
    if set(d0) != expected:
        raise Hold("HOLD_SCHEMA", "D0_KEYS")
    if type(d0["change_class"]) is not str or d0["change_class"] not in CHANGE_CLASSES:
        raise Hold("HOLD_SCHEMA", "D0_CHANGE_CLASS")
    if not (isinstance(delta.get("expected_sha256"), str) and HEX64.match(delta["expected_sha256"])):
        raise Hold("HOLD_SCHEMA", "D0_DELTA_HASH")
    if delta["kind"] == "INLINE":
        fields = delta.get("fields")
        if set(delta) != {"kind", "expected_sha256", "fields"} or not isinstance(fields, dict) \
                or not 0 < len(fields) <= MAX_INLINE_FIELDS:
            raise Hold("HOLD_SCHEMA", "D0_INLINE_SHAPE")
        for k, v in fields.items():
            if not (is_id(k) and (v is None or isinstance(v, (bool, int))
                                  or (isinstance(v, str) and len(v) <= MAX_INLINE_VALUE_CHARS))):
                raise Hold("HOLD_SCHEMA", "D0_INLINE_NOT_SMALL_FLAT")
    else:
        if set(delta) != {"kind", "expected_sha256", "ref"}:
            raise Hold("HOLD_SCHEMA", "D0_POINTER_SHAPE")
        if not is_id(delta["ref"]):
            raise Hold("HOLD_IDENTITY", "POINTER_REF_INVALID")
    r = d0["referent"]
    if not (isinstance(r, dict) and set(r) == {"type", "id"} and is_id(r["type"]) and is_id(r["id"])):
        raise Hold("HOLD_IDENTITY", "REFERENT_INVALID")
    for key in ("event_id", "owner_ref", "source_ref", "commit_readback_ref", "revision_after"):
        if not is_id(d0[key]):
            raise Hold("HOLD_IDENTITY", f"{key.upper()}_INVALID")
    if not (d0["revision_before"] is None or is_id(d0["revision_before"])):
        raise Hold("HOLD_IDENTITY", "REVISION_BEFORE_INVALID")
    if type(d0["owner_seq"]) is not int or d0["owner_seq"] < 1:
        raise Hold("HOLD_IDENTITY", "OWNER_SEQ_INVALID")
    wh = d0["way_home"]
    if not (isinstance(wh, list) and wh and all(is_id(w) for w in wh)):
        raise Hold("HOLD_IDENTITY", "WAY_HOME_MISSING")


def validate_envelope_for_d0(env: Mapping[str, Any], d0: Mapping[str, Any], registry: Mapping[str, Any]) -> None:
    """v0.15 version gate (typed hold, no implicit upgrade) -> v0.1 envelope validation -> binding to the D0."""
    entries = {e["subject"]: e for e in registry["entries"]}
    subject = env.get("subject")
    entry = entries.get(subject) if isinstance(subject, str) else None
    if entry is None or subject not in SUBJECT_MESSAGE_TYPE:
        raise Hold("HOLD_SCHEMA", "SUBJECT_NOT_SENSING_SUBJECT")
    gate = ref.evaluate_schema_compatibility(
        compatibility_policy=entry["compatibility"], declared_version=str(env.get("schema_version")),
        known_versions=frozenset({"1.0.0-draft"}), negotiated=True,
        content_type_declared=isinstance(d0.get("delta"), dict) and d0["delta"].get("kind") in ("INLINE", "POINTER"))
    if gate["status"] != "ACCEPTED":
        raise Hold("HOLD_SCHEMA", f"ENVELOPE_{gate['reason']}")
    try:
        ref.validate_envelope(dict(env))
    except ref.SchemaError:
        raise Hold("HOLD_SCHEMA", "ENVELOPE_INVALID_V01") from None
    if env["ttl_seconds"] > MAX_TTL_SECONDS:
        raise Hold("HOLD_SCHEMA", "TTL_OVER_BOUND")
    if type(env["message_type"]) is not str or env["message_type"] != entry["message_type"]:
        raise Hold("HOLD_SCHEMA", "MESSAGE_TYPE_NOT_REGISTRY_TYPE")
    if env["payload_hash"] != sha256_hex(canonical(dict(d0))) or env["payload_ref"] != f"d0:{d0.get('event_id')}":
        raise Hold("HOLD_SCHEMA", "D0_NOT_BOUND_TO_ENVELOPE")
    kind = d0["delta"]["kind"]
    if (kind == "INLINE") != (subject == SUBJECT_STATE_DELTA):
        raise Hold("HOLD_SCHEMA", "SUBJECT_DELTA_KIND_MISMATCH")
    if env["effect_class"] != "NONE" or env["authority_class"] != "NONE":
        raise Hold("HOLD_SCHEMA", "SIGNAL_CLAIMS_EFFECT_OR_AUTHORITY")   # SIGNAL_NE_TRUTH_NE_AUTHORITY
    tgt = env["target"]
    if (tgt["referent_type"], tgt["referent_id"], tgt["state_owner_ref"]) != (
            d0["referent"]["type"], d0["referent"]["id"], d0["owner_ref"]) or env["message_id"] != d0["event_id"]:
        raise Hold("HOLD_IDENTITY", "ENVELOPE_D0_IDENTITY_MISMATCH")
    if env["source"]["principal_ref"] != d0["owner_ref"]:
        raise Hold("HOLD_IDENTITY", "ENVELOPE_PRINCIPAL_NOT_OWNER")
    if env["idempotency_key"] != idempotency_key(d0):
        raise Hold("HOLD_IDENTITY", "IDEMPOTENCY_KEY_NOT_REGISTRY_SCOPE")
