"""Receiver-side SYNTHETIC bookkeeping (sqlite): disposition journal, dedupe, delivery log, holds, advisories, counters.

Fixture/application bookkeeping, NOT canonical truth. It survives process restart (a file), and every terminal
disposition is written and then read back on a NEW connection, exact, bound to stream/sequence + raw pointer digest,
BEFORE the transit ACK is allowed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .faults import Faults

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispositions(
  stream TEXT, stream_seq INTEGER, pointer_sha256 TEXT, message_id TEXT, owner_event_key TEXT, owner_revision INTEGER,
  d1_disposition TEXT, gateway_disposition TEXT, reason TEXT, receipt_ref TEXT, first_delivery_id TEXT,
  recorded_at REAL, PRIMARY KEY(stream, stream_seq));
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY, stream TEXT, stream_seq INTEGER, consumer_seq INTEGER, num_delivered INTEGER,
  message_id TEXT, pointer_sha256 TEXT, at REAL);
CREATE TABLE IF NOT EXISTS holds(
  delivery_id TEXT, stream TEXT, stream_seq INTEGER, message_id TEXT, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS advisories(
  advisory_id TEXT PRIMARY KEY, kind TEXT, stream TEXT, stream_seq INTEGER, deliveries INTEGER, raw TEXT, at REAL);
CREATE TABLE IF NOT EXISTS dedupe(dedupe_key TEXT PRIMARY KEY, event_fingerprint TEXT);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS samples(name TEXT, value REAL, at REAL);
"""

TERMINAL_FIELDS = ("pointer_sha256", "message_id", "owner_event_key", "owner_revision", "d1_disposition",
                   "gateway_disposition", "reason", "receipt_ref")


class JournalUnproven(RuntimeError):
    """Disposition write or its independent readback failed: the transit ACK must not be sent."""


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


class ReceiverJournal:
    SYNTHETIC_TEST_ONLY = True
    label = "SYNTHETIC_RECEIVER_BOOKKEEPING_NOT_TRUTH"

    def __init__(self, path: Path | str, *, faults: Faults | None = None) -> None:
        self.path = Path(path)
        self.faults = faults or Faults()
        with closing(_connect(self.path)) as con:
            con.executescript(_SCHEMA)

    # ------------------------------------------------------------------ terminal dispositions
    def terminal(self, stream: str, stream_seq: int) -> dict[str, Any] | None:
        with closing(_connect(self.path)) as con:
            row = con.execute("SELECT * FROM dispositions WHERE stream=? AND stream_seq=?",
                              (stream, stream_seq)).fetchone()
            return dict(row) if row else None

    def terminal_for_message(self, message_id: str) -> list[dict[str, Any]]:
        with closing(_connect(self.path)) as con:
            return [dict(r) for r in con.execute("SELECT * FROM dispositions WHERE message_id=? ORDER BY stream_seq",
                                                 (message_id,)).fetchall()]

    def write_terminal(self, *, stream: str, stream_seq: int, delivery_id: str, **fields: Any) -> dict[str, Any]:
        """Insert-if-absent, then independent exact readback. Returns the durable row (which may be an EARLIER row for
        the same stream/seq: the caller must then honour that recorded disposition). Raises JournalUnproven."""
        try:
            self.faults.hit("journal.write")
            with closing(_connect(self.path)) as con:
                con.execute("INSERT OR IGNORE INTO dispositions(stream, stream_seq, pointer_sha256, message_id, "
                            "owner_event_key, owner_revision, d1_disposition, gateway_disposition, reason, receipt_ref, "
                            "first_delivery_id, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (stream, stream_seq, *(fields[k] for k in TERMINAL_FIELDS), delivery_id, time.time()))
        except Exception as exc:
            raise JournalUnproven(f"write:{type(exc).__name__}") from None
        try:
            self.faults.hit("journal.readback")
            row = self.terminal(stream, stream_seq)               # NEW connection
        except Exception as exc:
            raise JournalUnproven(f"readback:{type(exc).__name__}") from None
        if row is None or row["pointer_sha256"] != fields["pointer_sha256"]:
            raise JournalUnproven("readback:absent_or_other_pointer_bytes")
        return row

    # ------------------------------------------------------------------ deliveries / holds / advisories
    def record_delivery(self, *, delivery_id: str, stream: str, stream_seq: int, consumer_seq: int,
                        num_delivered: int, message_id: str | None, pointer_sha256: str) -> None:
        with closing(_connect(self.path)) as con:
            con.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,?,?,?)",
                        (delivery_id, stream, stream_seq, consumer_seq, num_delivered, message_id, pointer_sha256,
                         time.time()))

    def max_delivered(self, stream: str, stream_seq: int) -> int:
        with closing(_connect(self.path)) as con:
            row = con.execute("SELECT MAX(num_delivered) FROM deliveries WHERE stream=? AND stream_seq=?",
                              (stream, stream_seq)).fetchone()
            return row[0] or 0

    def deliveries(self) -> list[dict[str, Any]]:
        with closing(_connect(self.path)) as con:
            return [dict(r) for r in con.execute("SELECT * FROM deliveries ORDER BY at").fetchall()]

    def record_hold(self, *, delivery_id: str, stream: str, stream_seq: int, message_id: str | None,
                    reason: str) -> None:
        with closing(_connect(self.path)) as con:
            con.execute("INSERT INTO holds VALUES (?,?,?,?,?,?)",
                        (delivery_id, stream, stream_seq, message_id, reason, time.time()))

    def holds(self) -> list[dict[str, Any]]:
        with closing(_connect(self.path)) as con:
            return [dict(r) for r in con.execute("SELECT * FROM holds ORDER BY at").fetchall()]

    def record_advisory(self, advisory: dict[str, Any]) -> None:
        with closing(_connect(self.path)) as con:
            con.execute("INSERT OR IGNORE INTO advisories VALUES (?,?,?,?,?,?,?)",
                        (advisory.get("id"), advisory.get("type"), advisory.get("stream"),
                         advisory.get("stream_seq"), advisory.get("deliveries"), json.dumps(advisory), time.time()))

    def advisories(self) -> list[dict[str, Any]]:
        with closing(_connect(self.path)) as con:
            return [dict(r) for r in con.execute("SELECT * FROM advisories ORDER BY at").fetchall()]

    # ------------------------------------------------------------------ DedupePort (gateway contract)
    def assess(self, owner_event_key: str, event_fingerprint: str) -> str:
        with closing(_connect(self.path)) as con:
            row = con.execute("SELECT event_fingerprint FROM dedupe WHERE dedupe_key=?", (owner_event_key,)).fetchone()
        if row is None:
            return "NEW"
        return "SAME" if row[0] == event_fingerprint else "CONFLICT"

    def remember(self, owner_event_key: str, event_fingerprint: str) -> None:
        with closing(_connect(self.path)) as con:
            con.execute("INSERT OR IGNORE INTO dedupe VALUES (?,?)", (owner_event_key, event_fingerprint))
            row = con.execute("SELECT event_fingerprint FROM dedupe WHERE dedupe_key=?", (owner_event_key,)).fetchone()
        if row is None or row[0] != event_fingerprint:
            raise RuntimeError("DEDUPE_REMEMBER_CONFLICT")

    # ------------------------------------------------------------------ counters / samples
    def bump(self, name: str, by: float = 1) -> None:
        with closing(_connect(self.path)) as con:
            con.execute("INSERT INTO counters VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = value + ?",
                        (name, by, by))

    def sample(self, name: str, value: float) -> None:
        with closing(_connect(self.path)) as con:
            con.execute("INSERT INTO samples VALUES (?,?,?)", (name, value, time.time()))

    def counters(self) -> dict[str, float]:
        with closing(_connect(self.path)) as con:
            return {r[0]: r[1] for r in con.execute("SELECT name, value FROM counters").fetchall()}

    def samples(self, name: str) -> list[float]:
        with closing(_connect(self.path)) as con:
            return [r[0] for r in con.execute("SELECT value FROM samples WHERE name=? ORDER BY at", (name,))]
