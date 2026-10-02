"""Evidence-only append-only JSONL store shared by the cursor, the sender ledger and the flight recorder.

Not a truth store: every record is ids, hashes, ints and machine reason codes. The store REFUSES free text, so
owner state (inline delta values, grounding text) cannot leak into local evidence even by mistake.
Crash behaviour: an incomplete final line (no newline) is dropped and the file truncated to the last good line.
A complete line with a bad checksum fails closed, even when it is the final line: silently losing an INTENT or CLAIM
would reopen a send or owner reread. Fail closed at open, never guess.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .model import SensingError, canonical, sha256_hex

STORE_SCHEMA = "sensing.store/v0.1"
_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:@/#=|+-]{1,200}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class StoreCorrupt(RuntimeError):
    pass


def check_evidence_value(key: str, value: Any) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str) and _VALUE_RE.match(value):
        return
    if isinstance(value, (list, tuple)) and len(value) <= 16 and all(isinstance(v, str) and _VALUE_RE.match(v) for v in value):
        return
    raise SensingError("EVIDENCE_NOT_ID_ONLY", f"{key}: evidence stores ids/hashes/codes only, never free text or state")


class JsonlStore:
    def __init__(self, path: Path | str | None, *, name: str) -> None:
        self.name = name
        self.path = Path(path) if path is not None else None
        self.repaired_tail = False
        self._records: list[dict[str, Any]] = []
        self._fh = None
        header = {"kind": "HEADER", "store": name, "schema": STORE_SCHEMA, "evidence_only": True, "canonical": False}
        if self.path is None:
            self._records.append(header)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            self._load()
        self._fh = open(self.path, "ab")
        if not self._records:
            self._write(header)
            self._records.append(header)
        elif self._records[0].get("store") != name:
            self.close()
            raise StoreCorrupt(f"{self.path}: header is for store {self._records[0].get('store')!r}, expected {name!r}")

    def _load(self) -> None:
        raw = self.path.read_bytes()
        pieces = raw.split(b"\n")
        tail = pieces.pop()                       # bytes after the last newline (b"" when file ends cleanly)
        good_len, records = 0, []
        for i, piece in enumerate(pieces):
            rec = self._decode(piece)
            if rec is None:
                raise StoreCorrupt(f"{self.path}: corrupt complete record at line {i + 1}; refusing to guess")
            records.append(rec)
            good_len += len(piece) + 1
        if tail:
            self.repaired_tail = True
        if good_len != len(raw):
            os.truncate(self.path, good_len)
        self._records = records

    @staticmethod
    def _decode(piece: bytes) -> dict[str, Any] | None:
        try:
            text = piece.decode("utf-8")
            digest, payload = text.split(" ", 1)
            if sha256_hex(payload.encode("utf-8"))[:16] != digest:
                return None
            rec = json.loads(payload)
            return rec if isinstance(rec, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None

    def _write(self, rec: dict[str, Any]) -> None:
        payload = canonical(rec).decode("utf-8")
        self._fh.write(f"{sha256_hex(payload.encode('utf-8'))[:16]} {payload}\n".encode("utf-8"))
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def append(self, rec: dict[str, Any]) -> None:
        if rec.get("kind") in (None, "HEADER"):
            raise SensingError("EVIDENCE_RECORD_KIND_INVALID")
        for k, v in rec.items():
            if not _KEY_RE.match(k):
                raise SensingError("EVIDENCE_KEY_INVALID", k)
            check_evidence_value(k, v)
        if self.path is not None:
            if self._fh is None:
                raise SensingError("STORE_CLOSED")
            self._write(rec)
        self._records.append(dict(rec))
