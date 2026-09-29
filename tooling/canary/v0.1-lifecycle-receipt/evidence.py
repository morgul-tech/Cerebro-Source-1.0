#!/usr/bin/env python3
"""Evidence logging for the v0.1 bounded canary.

STATUS: canary preparation artifact. authority: NONE. Not yet executed.

Both producer.py and consumer.py write one JSON-lines evidence file each
(never the same file -- producer and consumer evidence must stay
independently attributable so a third party can distinctly verify them
against each other, per PRINCIPAL DECISION point 3: "en annen aktor gjor
uavhengig verifikasjon"). No log entry is ever mutated after being
written; each is appended once.

See EVIDENCE_SCHEMA.md for the field-by-field meaning of each record.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, TextIO


def canonical_bytes(msg: dict[str, Any]) -> bytes:
    """Deterministic wire representation used for hashing. Sorted keys,
    no whitespace variance, so producer-side and consumer-side hashes of
    the same logical message are byte-identical regardless of dict
    ordering on either side of the wire."""
    return json.dumps(msg, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EvidenceWriter:
    """Append-only JSON-lines evidence writer. One instance per role
    (producer or consumer) per run."""

    def __init__(self, path: Path, role: str, run_id: str) -> None:
        self.path = path
        self.role = role
        self.run_id = run_id
        self._fh: TextIO | None = None

    def __enter__(self) -> "EvidenceWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is not None:
            self._fh.close()

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry = {
            "run_id": self.run_id,
            "role": self.role,
            "event": event,
            "wall_clock_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        assert self._fh is not None, "EvidenceWriter used outside 'with' block"
        self._fh.write(json.dumps(entry, sort_keys=True) + "\n")
        self._fh.flush()
        return entry


class DeadlineExceeded(RuntimeError):
    pass


class Deadline:
    """Hard wall-clock kill switch. PRINCIPAL DECISION: 'Maks 60 minutter;
    steng etter fast testsett.' Both producer and consumer check this
    before every send/receive and abort (not just log) once exceeded."""

    def __init__(self, max_seconds: int = 3600) -> None:
        self.start = time.monotonic()
        self.max_seconds = max_seconds

    def remaining(self) -> float:
        return self.max_seconds - (time.monotonic() - self.start)

    def check(self) -> None:
        if self.remaining() <= 0:
            raise DeadlineExceeded(
                f"canary run exceeded its {self.max_seconds}s hard limit; "
                "abort per PRINCIPAL DECISION scope bound"
            )
