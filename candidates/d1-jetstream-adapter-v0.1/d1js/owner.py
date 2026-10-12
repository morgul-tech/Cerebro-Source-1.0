"""SYNTHETIC Project owner: canonical event + head + outbox + Project-owned D1 closure ledger in ONE sqlite file.

Explicitly synthetic fixture storage -- NOT Cerebro truth, NOT a production owner adapter. It models exactly the
seams the assignment names:
  * owner commit writes the canonical event, the project head and (optionally) the outbox intent in one transaction;
  * the outbox keeps every publication obligation (intent -> PubAck stream/seq/duplicate, UNKNOWN_PENDING, capacity
    rejects, conflicts) so obligations survive broker expiry/retention and process restart;
  * the closure ledger mirrors tooling/owner_state/project_d1_closure_return.py @ a8f1780f: owner-commit proof,
    pre-append head fence (revision AND last event), immutable insert-if-absent, exact compare, then an independent
    read-only readback on a NEW connection. Receipt ref/fingerprint use the same formulas.
The ProjectOwnerEventReader / DurableReturnPort bindings for the gateway live in ``owner_ports.py``.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .faults import Faults

SYNTHETIC_LABEL = "SYNTHETIC_PROJECT_OWNER_NOT_CEREBRO_TRUTH"
OWNER_REF = "PROJECT_ENGINE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  owner_event_key TEXT PRIMARY KEY, tenant_ref TEXT, workspace_ref TEXT, project_ref TEXT, owner_ref TEXT,
  owner_revision INTEGER, event_fingerprint TEXT, material INTEGER, kind TEXT, referent_type TEXT, referent_id TEXT,
  payload TEXT, payload_sha256 TEXT, state_ref TEXT, state_fingerprint TEXT);
CREATE TABLE IF NOT EXISTS event_history(
  owner_event_key TEXT, owner_revision INTEGER, event_fingerprint TEXT, payload_sha256 TEXT,
  PRIMARY KEY(owner_event_key, owner_revision));
CREATE TABLE IF NOT EXISTS project_heads(
  tenant_ref TEXT, workspace_ref TEXT, project_ref TEXT, current_state_ref TEXT, owner_revision INTEGER,
  state_fingerprint TEXT, last_event_ref TEXT, PRIMARY KEY(tenant_ref, workspace_ref, project_ref));
CREATE TABLE IF NOT EXISTS outbox(
  message_id TEXT PRIMARY KEY, owner_event_key TEXT, owner_revision INTEGER, receiver_ref TEXT,
  pointer BLOB, pointer_sha256 TEXT, created_at TEXT, expires_at TEXT, state TEXT, stream TEXT, stream_seq INTEGER,
  duplicate INTEGER, puback_latency_ms REAL, attempts INTEGER DEFAULT 0, last_reason TEXT, updated_at REAL,
  claim_token TEXT, claimed_at TEXT);
CREATE TABLE IF NOT EXISTS d1_closure_ledger(
  tenant_ref TEXT, workspace_ref TEXT, project_ref TEXT, receiver_ref TEXT, closure_id TEXT,
  closure_revision INTEGER, closure_fingerprint TEXT, owner_event_key TEXT, owner_event_fingerprint TEXT,
  receipt_ref TEXT, receipt_fingerprint TEXT, receipt_payload TEXT, inserted_at REAL,
  PRIMARY KEY(tenant_ref, workspace_ref, project_ref, receiver_ref, closure_id));
"""

OUTBOX_INTENDED = "INTENDED"                  # committed with the event, not yet published
OUTBOX_SEND_CLAIMED = "SEND_CLAIMED"          # durably claimed BEFORE any transport effect: may be published
OUTBOX_PUBLISHED = "PUBLISHED"                # PubAck stream/seq recorded
OUTBOX_UNKNOWN = "UNKNOWN_PENDING"            # publish outcome unknown: reconcile before any retry
OUTBOX_CAPACITY = "REJECTED_CAPACITY"         # broker refused (DiscardNew / size): obligation stays open
OUTBOX_CONFLICT = "CONFLICT_HOLD"             # same id, different bytes
OUTBOX_RECONCILE_HOLD = "RECONCILE_HOLD"      # lookup unavailable/incomplete: never a blind second publish


def _canonical_text(value: Any) -> str:          # == control_context_state_postgres._canonical_text
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:                  # == control_context_state_postgres._sha256
    return hashlib.sha256(_canonical_text(value).encode("utf-8")).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


class SyntheticProjectOwner:
    SYNTHETIC_TEST_ONLY = True
    label = SYNTHETIC_LABEL

    def __init__(self, path: Path | str, *, faults: Faults | None = None) -> None:
        self.path = Path(path)
        self.faults = faults or Faults()
        with closing(_connect(self.path)) as con:
            con.executescript(_SCHEMA)

    # ------------------------------------------------------------------ owner commit (+ outbox intent)
    def commit_event(self, *, tenant_ref: str, workspace_ref: str, project_ref: str, owner_event_key: str,
                     material: bool, kind: str, referent_type: str, referent_id: str, payload: dict[str, Any],
                     outbox: list[tuple[str, str, bytes, str, str]] | None = None) -> dict[str, Any]:
        """Commit (or amend) one canonical event: head revision N -> N+1 and last_event_ref = this key.

        ``outbox`` is a list of (message_id, receiver_ref, pointer_bytes, created_at, expires_at) intents written in
        the SAME transaction (the owner-side seam). None models a producer WITHOUT an outbox.
        Returns the committed event facts (revision, fingerprint, payload hash).
        """
        con = _connect(self.path)
        try:
            con.execute("BEGIN IMMEDIATE")
            head = con.execute("SELECT * FROM project_heads WHERE tenant_ref=? AND workspace_ref=? AND project_ref=?",
                               (tenant_ref, workspace_ref, project_ref)).fetchone()
            revision = (head["owner_revision"] if head else 0) + 1
            payload_text = _canonical_text(payload)
            payload_sha = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
            fingerprint = _sha256({"owner_event_key": owner_event_key, "owner_revision": revision,
                                   "payload_sha256": payload_sha, "material": bool(material), "kind": kind})
            state_ref = f"state:{project_ref}:{revision}"
            state_fp = _sha256({"state_ref": state_ref, "event": owner_event_key, "fingerprint": fingerprint})
            con.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (owner_event_key, tenant_ref, workspace_ref, project_ref, OWNER_REF, revision, fingerprint,
                         int(bool(material)), kind, referent_type, referent_id, payload_text, payload_sha, state_ref,
                         state_fp))
            con.execute("INSERT INTO event_history VALUES (?,?,?,?)", (owner_event_key, revision, fingerprint,
                                                                       payload_sha))
            con.execute("INSERT OR REPLACE INTO project_heads VALUES (?,?,?,?,?,?,?)",
                        (tenant_ref, workspace_ref, project_ref, state_ref, revision, state_fp, owner_event_key))
            facts = {"owner_event_key": owner_event_key, "owner_revision": revision, "event_fingerprint": fingerprint,
                     "payload_sha256": payload_sha}
            for message_id, receiver_ref, pointer, created_at, expires_at in (outbox or []):
                con.execute("INSERT INTO outbox(message_id, owner_event_key, owner_revision, receiver_ref, pointer, "
                            "pointer_sha256, created_at, expires_at, state, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (message_id, owner_event_key, revision, receiver_ref, pointer,
                             hashlib.sha256(pointer).hexdigest(), created_at, expires_at, OUTBOX_INTENDED, time.time()))
            con.execute("COMMIT")
            return facts
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def peek_next_revision(self, tenant_ref: str, workspace_ref: str, project_ref: str) -> int:
        with closing(_connect(self.path)) as con:
            head = con.execute("SELECT owner_revision FROM project_heads WHERE tenant_ref=? AND workspace_ref=? "
                               "AND project_ref=?", (tenant_ref, workspace_ref, project_ref)).fetchone()
            return (head[0] if head else 0) + 1

    def fingerprint_for(self, *, owner_event_key: str, owner_revision: int, payload: dict[str, Any], material: bool,
                        kind: str) -> str:
        payload_sha = hashlib.sha256(_canonical_text(payload).encode("utf-8")).hexdigest()
        return _sha256({"owner_event_key": owner_event_key, "owner_revision": owner_revision,
                        "payload_sha256": payload_sha, "material": bool(material), "kind": kind})

    def touch_head(self, tenant_ref: str, workspace_ref: str, project_ref: str, *, same_revision: bool) -> None:
        """Fault helper: move the project head between owner read and return append.
        same_revision=False => revision N -> N+1; True => same revision, different last_event_ref."""
        con = _connect(self.path)
        try:
            con.execute("BEGIN IMMEDIATE")
            head = con.execute("SELECT * FROM project_heads WHERE tenant_ref=? AND workspace_ref=? AND project_ref=?",
                               (tenant_ref, workspace_ref, project_ref)).fetchone()
            if same_revision:
                con.execute("UPDATE project_heads SET last_event_ref=? WHERE tenant_ref=? AND workspace_ref=? AND "
                            "project_ref=?", ("other-event-same-revision", tenant_ref, workspace_ref, project_ref))
            else:
                con.execute("UPDATE project_heads SET owner_revision=?, current_state_ref=? WHERE tenant_ref=? AND "
                            "workspace_ref=? AND project_ref=?", (head["owner_revision"] + 1,
                                                                  f"state:{project_ref}:{head['owner_revision'] + 1}",
                                                                  tenant_ref, workspace_ref, project_ref))
            con.execute("COMMIT")
        finally:
            con.close()

    # ------------------------------------------------------------------ reads (fresh connection each time)
    def event(self, owner_event_key: str) -> dict[str, Any] | None:
        with closing(_connect(self.path)) as con:
            row = con.execute("SELECT * FROM events WHERE owner_event_key=?", (owner_event_key,)).fetchone()
            return dict(row) if row else None

    def head(self, tenant_ref: str, workspace_ref: str, project_ref: str) -> dict[str, Any] | None:
        with closing(_connect(self.path)) as con:
            row = con.execute("SELECT * FROM project_heads WHERE tenant_ref=? AND workspace_ref=? AND project_ref=?",
                              (tenant_ref, workspace_ref, project_ref)).fetchone()
            return dict(row) if row else None

    def read_committed_owner_event(self, *, tenant_ref: str, workspace_ref: str, project_ref: str,
                                   owner_event_key: str) -> dict[str, Any]:
        """Same shape as the reference ProjectOwnerCommitReader (synthetic flags, labelled)."""
        self.faults.hit("owner.commit_reader")
        ev = self.event(owner_event_key)
        head = self.head(tenant_ref, workspace_ref, project_ref)
        if ev is None or head is None or (ev["tenant_ref"], ev["workspace_ref"], ev["project_ref"]) != (
                tenant_ref, workspace_ref, project_ref):
            raise LookupError("owner-event-not-found")
        current = head["last_event_ref"] == owner_event_key and head["owner_revision"] == ev["owner_revision"]
        return {"tenant_ref": tenant_ref, "workspace_ref": workspace_ref, "project_ref": project_ref,
                "owner": "project", "event_state": "OWNER_EFFECT_COMMITTED", "owner_event_key": owner_event_key,
                "owner_event_fingerprint": ev["event_fingerprint"], "provider_readback_verified": True,
                "authenticated_same_owner_readback": True, "currentness": "CURRENT" if current else "STALE",
                "owner_revision": ev["owner_revision"], "owner_state_ref": ev["state_ref"],
                "owner_state_fingerprint": ev["state_fingerprint"], "synthetic": SYNTHETIC_LABEL}

    # ------------------------------------------------------------------ outbox
    def outbox(self, message_id: str | None = None) -> list[dict[str, Any]]:
        with closing(_connect(self.path)) as con:
            if message_id is None:
                rows = con.execute("SELECT * FROM outbox ORDER BY created_at, message_id").fetchall()
            else:
                rows = con.execute("SELECT * FROM outbox WHERE message_id=?", (message_id,)).fetchall()
            return [dict(r) for r in rows]

    def outbox_update(self, message_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with closing(_connect(self.path)) as con:
            con.execute(f"UPDATE outbox SET {cols} WHERE message_id=?", (*fields.values(), message_id))

    def outbox_transition(self, message_id: str, *, from_state: str, from_token: str | None,
                          **fields: Any) -> bool:
        """Atomic compare-and-set on (state, claim_token). Exactly one caller can move a given (state, token).
        Returns False (nothing written) when the row moved; raises when the durable write itself fails."""
        self.faults.hit("owner.outbox_transition")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        con = _connect(self.path)
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(f"UPDATE outbox SET {cols} WHERE message_id=? AND state=? AND claim_token IS ?",
                              (*fields.values(), message_id, from_state, from_token))
            con.execute("COMMIT")
            return cur.rowcount == 1
        finally:
            con.close()

    def outbox_add(self, message_id: str, owner_event_key: str, owner_revision: int, receiver_ref: str,
                   pointer: bytes, created_at: str, expires_at: str) -> str:
        """Record a new intent; same id + same bytes => existing; same id + different bytes => CONFLICT_HOLD."""
        digest = hashlib.sha256(pointer).hexdigest()
        con = _connect(self.path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT pointer_sha256 FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            if row is not None:
                con.execute("COMMIT")
                return "SAME" if row[0] == digest else "CONFLICT"
            con.execute("INSERT INTO outbox(message_id, owner_event_key, owner_revision, receiver_ref, pointer, "
                        "pointer_sha256, created_at, expires_at, state, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (message_id, owner_event_key, owner_revision, receiver_ref, pointer, digest, created_at,
                         expires_at, OUTBOX_INTENDED, time.time()))
            con.execute("COMMIT")
            return "NEW"
        finally:
            con.close()

    # ------------------------------------------------------------------ Project-owned D1 closure ledger
    def append_and_confirm(self, *, tenant_ref: str, workspace_ref: str, project_ref: str, receiver_ref: str,
                           closure_id: str, closure_fingerprint: str, owner_event_key: str,
                           owner_event_fingerprint: str, critical: Any = None) -> dict[str, Any]:
        """Mirror of PostgresProjectD1ClosureReturnPort.append_and_confirm (semantics, not SQL).
        ``critical`` (composition only): a context manager that must hold for the fenced insert, e.g. the
        safe-boundary lease; if it cannot be entered nothing is inserted."""
        def hold(reason: str, mutation: bool | str = False) -> dict[str, Any]:
            return {"result": "HOLD", "closure_state": "NOT_CLOSED", "mutated": mutation, "reason": reason}

        identity = (tenant_ref, workspace_ref, project_ref, receiver_ref, closure_id)
        try:
            owner = self.read_committed_owner_event(tenant_ref=tenant_ref, workspace_ref=workspace_ref,
                                                    project_ref=project_ref, owner_event_key=owner_event_key)
        except Exception:
            return hold("project-owner-commit-readback-unavailable")
        if (owner["owner_event_fingerprint"] != owner_event_fingerprint or owner["currentness"] != "CURRENT"
                or owner["authenticated_same_owner_readback"] is not True):
            return hold("project-owner-commit-not-proven")
        expected_head = {"current_state_ref": owner["owner_state_ref"], "owner_revision": owner["owner_revision"],
                         "state_fingerprint": owner["owner_state_fingerprint"], "last_event_ref": owner_event_key}
        receipt = {"schema": "cerebro-project-d1-closure-receipt/v1", "tenant_ref": tenant_ref,
                   "workspace_ref": workspace_ref, "project_ref": project_ref, "receiver_ref": receiver_ref,
                   "closure_id": closure_id, "closure_revision": 1, "closure_fingerprint": closure_fingerprint,
                   "owner_event_key": owner_event_key, "owner_event_fingerprint": owner_event_fingerprint}
        receipt_ref = "D1C-" + _sha256(list(identity))[:32].upper()
        receipt_fingerprint = _sha256({**receipt, "receipt_ref": receipt_ref})
        expected_row = {"closure_revision": 1, "closure_fingerprint": closure_fingerprint,
                        "owner_event_key": owner_event_key, "owner_event_fingerprint": owner_event_fingerprint,
                        "receipt_ref": receipt_ref, "receipt_fingerprint": receipt_fingerprint,
                        "receipt_payload": _canonical_text(receipt)}
        self.faults.hit("owner.between_read_and_append")        # e.g. call:owner_moves_head
        from contextlib import nullcontext
        con = _connect(self.path)
        inserted = False
        try:
            gate = critical("return.insert") if critical is not None else nullcontext()
            gate.__enter__()
        except Exception:
            con.close()
            return hold("safe-boundary-lease-lost-before-insert")
        try:
            con.execute("BEGIN IMMEDIATE")                          # write lock == FOR SHARE head + insert, atomically
            head = con.execute("SELECT current_state_ref, owner_revision, state_fingerprint, last_event_ref FROM "
                               "project_heads WHERE tenant_ref=? AND workspace_ref=? AND project_ref=?",
                               (tenant_ref, workspace_ref, project_ref)).fetchone()
            if head is None or dict(head) != expected_head:
                con.execute("ROLLBACK")
                return hold("project-owner-head-revision-or-event-changed")
            cur = con.execute("INSERT OR IGNORE INTO d1_closure_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (*identity, 1, closure_fingerprint, owner_event_key, owner_event_fingerprint,
                               receipt_ref, receipt_fingerprint, expected_row["receipt_payload"], time.time()))
            inserted = cur.rowcount == 1
            row = self._ledger_row(con, identity)
            if row is None:
                con.execute("ROLLBACK")
                return hold("closure-collision-visibility-unproven")
            if row != expected_row:
                con.execute("ROLLBACK")
                return {"result": "CONFLICT_HOLD", "closure_state": "NOT_CLOSED", "mutated": False,
                        "reason": "closure-key-fingerprint-conflict"}
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            return hold("closure-write-commit-unproven", "UNKNOWN")
        finally:
            con.close()
            gate.__exit__(None, None, None)
        self.faults.hit("return.after_commit_before_readback")    # crash/raise here = committed, unconfirmed
        try:
            self.faults.hit("return.readback")
            head2, row2 = self._independent_read(identity, tenant_ref, workspace_ref, project_ref)
        except Exception:
            return hold("closure-provider-readback-unavailable", inserted)
        if row2 != expected_row or head2 != expected_head:
            return hold("closure-exact-provider-readback-mismatch", inserted)
        return {"result": "PASS", "closure_state": "CLOSED", "mutated": inserted, "receipt_ref": receipt_ref,
                "receipt_fingerprint": receipt_fingerprint, "closure_revision": 1, "receipt": receipt}

    @staticmethod
    def _ledger_row(con: sqlite3.Connection, identity: tuple[str, ...]) -> dict[str, Any] | None:
        row = con.execute("SELECT closure_revision, closure_fingerprint, owner_event_key, owner_event_fingerprint, "
                          "receipt_ref, receipt_fingerprint, receipt_payload FROM d1_closure_ledger WHERE "
                          "tenant_ref=? AND workspace_ref=? AND project_ref=? AND receiver_ref=? AND closure_id=?",
                          identity).fetchone()
        return dict(row) if row else None

    def _independent_read(self, identity, tenant_ref, workspace_ref, project_ref):
        con = _connect(self.path)                                 # a NEW connection: independent readback
        try:
            con.execute("BEGIN DEFERRED")
            head = con.execute("SELECT current_state_ref, owner_revision, state_fingerprint, last_event_ref FROM "
                               "project_heads WHERE tenant_ref=? AND workspace_ref=? AND project_ref=?",
                               (tenant_ref, workspace_ref, project_ref)).fetchone()
            row = self._ledger_row(con, identity)
            con.execute("COMMIT")
            return (dict(head) if head else None), row
        finally:
            con.close()

    def read_closure(self, *, tenant_ref: str, workspace_ref: str, project_ref: str, receiver_ref: str,
                     closure_id: str) -> dict[str, Any] | None:
        """Independent exact readback by key (new connection)."""
        self.faults.hit("return.read_exact")
        with closing(_connect(self.path)) as con:
            return self._ledger_row(con, (tenant_ref, workspace_ref, project_ref, receiver_ref, closure_id))

    def closure_by_receipt(self, receipt_ref: str) -> dict[str, Any] | None:
        with closing(_connect(self.path)) as con:
            row = con.execute("SELECT * FROM d1_closure_ledger WHERE receipt_ref=?", (receipt_ref,)).fetchone()
            return dict(row) if row else None

    def revision_of(self, owner_event_key: str, event_fingerprint: str) -> int | None:
        """Owner-held history: which committed revision carried this exact fingerprint (independent of the caller)."""
        with closing(_connect(self.path)) as con:
            row = con.execute("SELECT owner_revision FROM event_history WHERE owner_event_key=? AND "
                              "event_fingerprint=?", (owner_event_key, event_fingerprint)).fetchone()
            return row[0] if row else None

    def materializations(self) -> int:
        with closing(_connect(self.path)) as con:
            return con.execute("SELECT COUNT(*) FROM d1_closure_ledger").fetchone()[0]
