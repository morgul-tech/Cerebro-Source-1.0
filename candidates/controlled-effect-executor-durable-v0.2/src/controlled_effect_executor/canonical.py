"""Canonical serialization and digests.

Same convention as the existing Source primitives (tooling/owner_state, mcp/control_owner_effect_receipt):
json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) -> UTF-8 -> SHA-256 hex.
Floats are refused: they have no stable canonical text across implementations.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .errors import BatchSpecError

REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _scan(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise BatchSpecError(f"non-utf8-text:{path}") from None
        return
    if isinstance(value, float):
        raise BatchSpecError(f"float-not-canonical:{path}")
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _scan(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise BatchSpecError(f"non-string-key:{path}")
            _scan(item, f"{path}.{key}")
        return
    raise BatchSpecError(f"unsupported-type:{type(value).__name__}:{path}")


def canonical_text(value: Any) -> str:
    _scan(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def require_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or REF.fullmatch(value) is None:
        raise BatchSpecError(f"ref-invalid:{field}")
    return value
