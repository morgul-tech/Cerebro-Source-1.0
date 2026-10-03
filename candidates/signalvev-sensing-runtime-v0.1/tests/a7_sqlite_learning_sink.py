"""Disposable SQLite proof adapter for A7-01 tests.

This module intentionally lives outside guarded src/signalvev_sensing.
It proves the ContextLearningSink durability/readback/outbox contract locally;
it is not a production Context provider or runtime dependency.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from signalvev_sensing.a7_learning import (
    LearningConflict,
    LearningReceipt,
    LearningRecord,
)
from signalvev_sensing.model import SensingError, canonical, is_id, sha256_hex


@dataclass(frozen=True)
class PendingEncounter:
    pending_id: str
    learning_key: str
    record: LearningRecord


class EncounterConsumer(Protocol):
    def encounter(self, record: LearningRecord, *, idempotency_key: str) -> bool: ...


class SqliteContextLearningSink:
    """Disposable durable proof of Context-owned learning + outbox semantics."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS learning_record (
                learning_key TEXT PRIMARY KEY,
                payload_fingerprint TEXT NOT NULL,
                record_json TEXT NOT NULL,
                owner_receipt_ref TEXT NOT NULL,
                owner_receipt_fingerprint TEXT NOT NULL,
                provider_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_encounter (
                pending_id TEXT PRIMARY KEY,
                learning_key TEXT NOT NULL UNIQUE
                    REFERENCES learning_record(learning_key) ON DELETE CASCADE,
                record_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('PENDING','ACKED')) DEFAULT 'PENDING'
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _record_from_json(text: str) -> LearningRecord:
        raw = json.loads(text)
        raw["evidence_refs"] = tuple(raw["evidence_refs"])
        raw["way_home"] = tuple(raw["way_home"])
        return LearningRecord(**raw)

    @staticmethod
    def _receipt_for(record: LearningRecord, *, replay: bool) -> LearningReceipt:
        return LearningReceipt(
            learning_key=record.learning_key,
            record_fingerprint=record.payload_fingerprint,
            provider_revision=record.provider_revision,
            readback_verified=True,
            idempotent_replay=replay,
            pending_id=sha256_hex(("encounter|" + record.learning_key).encode())[:40],
        )

    def append_and_readback(self, record: LearningRecord) -> LearningReceipt:
        """Atomically persist one learning record plus one encounter obligation."""
        record_json = canonical(record.as_dict()).decode("utf-8")
        pending_id = sha256_hex(("encounter|" + record.learning_key).encode())[:40]
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT payload_fingerprint, record_json FROM learning_record WHERE learning_key=?",
                (record.learning_key,),
            ).fetchone()
            replay = row is not None
            if row is not None:
                if row["payload_fingerprint"] != record.payload_fingerprint:
                    raise LearningConflict(
                        "A7_LEARNING_CONFLICT",
                        "same event+basis changed semantic payload",
                    )
                stored = self._record_from_json(row["record_json"])
                self._conn.execute(
                    """INSERT OR IGNORE INTO pending_encounter(
                           pending_id, learning_key, record_json
                       ) VALUES(?,?,?)""",
                    (pending_id, stored.learning_key, row["record_json"]),
                )
            else:
                self._conn.execute(
                    """INSERT INTO learning_record(
                           learning_key,payload_fingerprint,record_json,
                           owner_receipt_ref,owner_receipt_fingerprint,provider_revision
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        record.learning_key,
                        record.payload_fingerprint,
                        record_json,
                        record.owner_receipt_ref,
                        record.owner_receipt_fingerprint,
                        record.provider_revision,
                    ),
                )
                self._conn.execute(
                    """INSERT INTO pending_encounter(
                           pending_id, learning_key, record_json
                       ) VALUES(?,?,?)""",
                    (pending_id, record.learning_key, record_json),
                )
                stored = record
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        observed = self._conn.execute(
            "SELECT payload_fingerprint,record_json FROM learning_record WHERE learning_key=?",
            (stored.learning_key,),
        ).fetchone()
        if (
            observed is None
            or observed["payload_fingerprint"] != stored.payload_fingerprint
            or self._record_from_json(observed["record_json"]).semantic_payload()
            != stored.semantic_payload()
        ):
            raise SensingError("A7_LEARNING_READBACK_FAILED")
        return self._receipt_for(stored, replay=replay)

    def learning_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM learning_record").fetchone()[0])

    def pending_count(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM pending_encounter WHERE state='PENDING'"
            ).fetchone()[0]
        )

    def pending(self) -> tuple[PendingEncounter, ...]:
        rows = self._conn.execute(
            """SELECT pending_id,learning_key,record_json
               FROM pending_encounter
               WHERE state='PENDING'
               ORDER BY pending_id"""
        ).fetchall()
        return tuple(
            PendingEncounter(
                pending_id=row["pending_id"],
                learning_key=row["learning_key"],
                record=self._record_from_json(row["record_json"]),
            )
            for row in rows
        )

    def ack_pending(self, pending_id: str) -> None:
        if not is_id(pending_id):
            raise SensingError("A7_PENDING_ID_INVALID")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.execute(
                """UPDATE pending_encounter
                   SET state='ACKED'
                   WHERE pending_id=? AND state='PENDING'""",
                (pending_id,),
            )
            if cur.rowcount != 1:
                raise SensingError("A7_PENDING_NOT_FOUND")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def dispatch_pending(self, consumer: EncounterConsumer) -> int:
        """Explicit later encounter delivery; no watcher/polling is created here."""
        delivered = 0
        for pending in self.pending():
            if consumer.encounter(pending.record, idempotency_key=pending.learning_key):
                self.ack_pending(pending.pending_id)
                delivered += 1
        return delivered
