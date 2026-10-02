"""Shared constants, typed vocabularies, errors and canonical helpers. Pure; no I/O."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

AUTHORITY = "NONE"

# --- typed vocabularies (closed sets; a value outside them is a bug, not a new case) -----------------
ACK_READ = "ACK_READ"
NOT_APPLICABLE = "NOT_APPLICABLE"
STALE_SUPERSEDED = "STALE_SUPERSEDED"
EXPIRED = "EXPIRED"
DUPLICATE = "DUPLICATE"
HOLD_UNREADABLE = "HOLD_UNREADABLE"
HOLD_IDENTITY = "HOLD_IDENTITY"
CONFLICT_HOLD = "CONFLICT_HOLD"
HOLD_APPLICABILITY = "HOLD_APPLICABILITY"
HOLD_SCHEMA = "HOLD_SCHEMA"
DISPOSITIONS = frozenset({ACK_READ, NOT_APPLICABLE, STALE_SUPERSEDED, EXPIRED, DUPLICATE, HOLD_UNREADABLE,
                          HOLD_IDENTITY, CONFLICT_HOLD, HOLD_APPLICABILITY, HOLD_SCHEMA})

APPLIES = "APPLIES"
APPLICABILITY_NOT_APPLICABLE = "NOT_APPLICABLE"
APPLICABILITY_HOLD = "HOLD"
APPLICABILITIES = frozenset({APPLIES, APPLICABILITY_NOT_APPLICABLE, APPLICABILITY_HOLD})

DETERMINISTIC = "DETERMINISTIC_DISPOSITION_COMPLETE"
JUDGMENT_REQUIRED = "JUDGMENT_REQUIRED"
ACTIVATIONS = frozenset({DETERMINISTIC, JUDGMENT_REQUIRED})

MECHANICAL = "MECHANICAL"
SEMANTIC = "SEMANTIC"
CHANGE_CLASSES = frozenset({MECHANICAL, SEMANTIC})

DEPTH_REVISION_CHECK = "REVISION_CHECK"   # inline small delta: cheapest owner check (revision + fingerprint)
DEPTH_POINTER_GROUND = "POINTER_GROUND"   # pointer: owner-bound bounded grounding of the pointed section
DEPTHS = frozenset({DEPTH_REVISION_CHECK, DEPTH_POINTER_GROUND})

# --- wire / size limits (STATE_STAYS_HOME / DELTA_TRAVELS / DEPTH_STAYS_REACHABLE) ---------------------
D0_VERSION = "sensing.d0/v0.1"
KNOWN_D0_VERSIONS = frozenset({D0_VERSION})
KNOWN_ENVELOPE_VERSIONS = frozenset({"1.0.0-draft"})
MAX_FRAME_BYTES = 4096
MAX_D0_BYTES = 1024
MAX_INLINE_FIELDS = 8
MAX_INLINE_VALUE_CHARS = 64
MAX_GROUNDING_BYTES = 4096
MAX_FUTURE_SKEW_SECONDS = 5

SUBJECT_STATE_DELTA = "cerebro.v1.state.delta"
SUBJECT_ARTIFACT_POINTER = "cerebro.v1.artifact.pointer"
SUBJECT_MESSAGE_TYPE = {SUBJECT_STATE_DELTA: "STATE_DELTA", SUBJECT_ARTIFACT_POINTER: "ARTIFACT_POINTER"}

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/#=-]{0,159}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class SensingError(ValueError):
    """A caller/programming error (bad owner event, oversize D0 ...). Never used for wire verdicts."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


class Hold(Exception):
    """A typed, expected wire verdict raised inside ingress. Carries disposition + machine reason."""

    def __init__(self, disposition: str, reason: str) -> None:
        super().__init__(f"{disposition}:{reason}")
        assert disposition in DISPOSITIONS
        self.disposition, self.reason = disposition, reason


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(text: Any) -> float:
    if not isinstance(text, str) or not text.endswith("Z"):
        raise ValueError("timestamp must be ISO-8601 UTC ending in Z")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))
