"""A7-01 local durable learning bridge candidate.

Flow:
  trusted owner receipt -> existing OwnerEvent -> local relevance/applicability
  -> Context-owned durable learning append+readback -> pending encounter outbox.

The SQLite sink is a disposable local proof adapter.  It models the required
Context-owned durability/readback contract; it is not a production provider,
truth store, scheduler, watcher, or authority source.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .a7_owner_receipt import TrustedOwnerCommitReader, read_trusted_owner_event
from .applicability import ApplicabilityPolicy
from .model import (
    APPLICABILITY_HOLD,
    APPLICABILITY_NOT_APPLICABLE,
    APPLIES,
    SensingError,
    canonical,
    is_id,
    sha256_hex,
)

LEARNING_COMMITTED = "LEARNING_COMMITTED"
NOT_APPLICABLE = "NOT_APPLICABLE"
HOLD_APPLICABILITY = "HOLD_APPLICABILITY"
OUTCOMES = frozenset({"POSITIVE", "NEGATIVE", "UNKNOWN"})
ORIGINS = frozenset({"HUMAN", "MACHINE", "PROVIDER", "SYSTEM"})


class LearningConflict(SensingError):
    pass


@dataclass(frozen=True)
class LearningRecord:
    learning_key: str
    event_id: str
    owner_ref: str
    referent_type: str
    referent_id: str
    revision_after: str
    expected_sha256: str
    classifier_revision: str
    basis_fingerprint: str
    payload_fingerprint: str
    outcome: str
    origin: str
    evidence_refs: tuple[str, ...]
    way_home: tuple[str, ...]
    owner_receipt_ref: str
    owner_receipt_fingerprint: str
    provider_revision: int

    def semantic_payload(self) -> dict[str, Any]:
        """Fields that define learning identity/content.

        Receipt/provider evidence is intentionally excluded so an equivalent
        reread/receipt does not manufacture a second learning event.
        """
        return {
            "event_id": self.event_id,
            "owner_ref": self.owner_ref,
            "referent_type": self.referent_type,
            "referent_id": self.referent_id,
            "revision_after": self.revision_after,
            "expected_sha256": self.expected_sha256,
            "classifier_revision": self.classifier_revision,
            "outcome": self.outcome,
            "origin": self.origin,
            "evidence_refs": list(self.evidence_refs),
            "way_home": list(self.way_home),
        }

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = list(self.evidence_refs)
        value["way_home"] = list(self.way_home)
        return value


@dataclass(frozen=True)
class LearningReceipt:
    learning_key: str
    record_fingerprint: str
    provider_revision: int
    readback_verified: bool
    idempotent_replay: bool
    pending_id: str


@dataclass(frozen=True)
class A7ProcessResult:
    disposition: str
    event_id: str
    applicability: str
    learning_receipt: LearningReceipt | None = None
    reason: str = ""


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
        """Atomically persist one learning record plus one encounter obligation.

        Same key + same semantic payload returns the original stored record.
        Same key + changed semantic payload fails closed.
        """
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
                    "INSERT OR IGNORE INTO pending_encounter(pending_id, learning_key, record_json) VALUES(?,?,?)",
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
                    "INSERT INTO pending_encounter(pending_id, learning_key, record_json) VALUES(?,?,?)",
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


class A7LearningBridge:
    def __init__(
        self,
        *,
        receipt_reader: TrustedOwnerCommitReader,
        interests: ApplicabilityPolicy,
        sink: SqliteContextLearningSink,
        classifier_revision: str,
    ) -> None:
        if not is_id(classifier_revision):
            raise SensingError("A7_CLASSIFIER_REVISION_INVALID")
        self._reader = receipt_reader
        self._interests = interests
        self._sink = sink
        self._classifier_revision = classifier_revision

    def process(
        self,
        *,
        receipt_ref: str,
        expected_owner_ref: str,
        outcome: str,
        origin: str,
        evidence_refs: Iterable[str],
    ) -> A7ProcessResult:
        trusted = read_trusted_owner_event(
            self._reader,
            receipt_ref=receipt_ref,
            expected_owner_ref=expected_owner_ref,
        )
        event = trusted.event
        applicability = self._interests.evaluate(
            event.owner_ref, event.referent_type, event.referent_id
        )
        if applicability == APPLICABILITY_NOT_APPLICABLE:
            return A7ProcessResult(
                NOT_APPLICABLE,
                event.event_id,
                APPLICABILITY_NOT_APPLICABLE,
                reason="LOCAL_NOT_APPLICABLE",
            )
        if applicability != APPLIES:
            return A7ProcessResult(
                HOLD_APPLICABILITY,
                event.event_id,
                APPLICABILITY_HOLD,
                reason="LOCAL_APPLICABILITY_UNDECIDABLE",
            )
        if outcome not in OUTCOMES:
            raise SensingError("A7_OUTCOME_INVALID")
        if origin not in ORIGINS:
            raise SensingError("A7_ORIGIN_INVALID")
        evidence = tuple(evidence_refs)
        if not evidence or not all(is_id(ref) for ref in evidence):
            raise SensingError("A7_EVIDENCE_REFS_INVALID")

        basis = {
            "event_id": event.event_id,
            "owner_ref": event.owner_ref,
            "referent_type": event.referent_type,
            "referent_id": event.referent_id,
            "revision_after": event.revision_after,
            "expected_sha256": event.expected_sha256,
            "classifier_revision": self._classifier_revision,
        }
        basis_fp = sha256_hex(canonical(basis))
        semantic = {
            **basis,
            "outcome": outcome,
            "origin": origin,
            "evidence_refs": list(evidence),
            "way_home": list(event.way_home),
        }
        payload_fp = sha256_hex(canonical(semantic))
        learning_key = sha256_hex(
            canonical(
                {
                    "event_id": event.event_id,
                    "owner_ref": event.owner_ref,
                    "revision_after": event.revision_after,
                    "classifier_revision": self._classifier_revision,
                }
            )
        )
        record = LearningRecord(
            learning_key=learning_key,
            event_id=event.event_id,
            owner_ref=event.owner_ref,
            referent_type=event.referent_type,
            referent_id=event.referent_id,
            revision_after=event.revision_after,
            expected_sha256=event.expected_sha256,
            classifier_revision=self._classifier_revision,
            basis_fingerprint=basis_fp,
            payload_fingerprint=payload_fp,
            outcome=outcome,
            origin=origin,
            evidence_refs=evidence,
            way_home=event.way_home,
            owner_receipt_ref=trusted.receipt_ref,
            owner_receipt_fingerprint=trusted.receipt_fingerprint,
            provider_revision=trusted.provider_revision,
        )
        receipt = self._sink.append_and_readback(record)
        return A7ProcessResult(
            LEARNING_COMMITTED,
            event.event_id,
            APPLIES,
            learning_receipt=receipt,
            reason="CONTEXT_LEARNING_APPENDED_AND_READ_BACK",
        )
