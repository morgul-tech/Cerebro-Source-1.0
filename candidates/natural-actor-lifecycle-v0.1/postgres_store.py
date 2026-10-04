"""NAL-01 candidate PostgreSQL adapter; default off and never opened by tests.

The caller must supply a connection factory for a separately provisioned
``cerebro_nal_owner`` role. No DSN, credential, migration runner or live port is
embedded here. One actor row is locked before any state decision. State, immutable
terminal/receipt/binding ledgers, and outbox intent commit in one DB transaction.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
from typing import Callable

from psycopg.types.json import Jsonb

from lifecycle import Hold, Result, canonical


class PostgresStore:
    def __init__(self, connect: Callable | None = None, *, enabled: bool = False,
                 expected_owner: str = "cerebro_nal_owner"):
        self.connect = connect
        self.enabled = enabled
        self.expected_owner = expected_owner

    def _connection(self):
        if not self.enabled or self.connect is None:
            raise Hold("PG_ADAPTER_DEFAULT_OFF_OR_UNBOUND")
        return self.connect()

    def _preflight(self, cursor):
        cursor.execute("SELECT current_user")
        if cursor.fetchone()[0] != self.expected_owner:
            raise Hold("NAL_OWNER_ROLE_REQUIRED")
        cursor.execute("SELECT name FROM nal.schema_migration WHERE version = 1")
        if cursor.fetchone() != ("natural-actor-lifecycle-v0.1",):
            raise Hold("NAL_SCHEMA_VERSION_UNAVAILABLE")

    def read(self, actor_ref: str) -> dict | None:
        try:
            with self._connection() as conn, conn.cursor() as cur:
                self._preflight(cur)
                cur.execute("SELECT state_json FROM nal.actor_state WHERE actor_ref = %s", (actor_ref,))
                row = cur.fetchone()
                return copy.deepcopy(row[0]) if row else None
        except Hold:
            raise
        except Exception as exc:
            raise Hold("PG_READ_UNAVAILABLE") from exc

    def read_commit_evidence(self, actor_ref: str, terminal_ref: str) -> dict:
        """Independent persisted readback for one terminal/close/outbox chain."""
        try:
            with self._connection() as conn, conn.cursor() as cur:
                self._preflight(cur)
                cur.execute("SELECT occurrence_json FROM nal.terminal_occurrence "
                            "WHERE actor_ref = %s AND terminal_ref = %s",
                            (actor_ref, terminal_ref))
                occurrence = cur.fetchone()
                cur.execute("SELECT receipt_json FROM nal.close_receipt "
                            "WHERE actor_ref = %s AND terminal_ref = %s",
                            (actor_ref, terminal_ref))
                receipt = cur.fetchone()
                if occurrence is None or receipt is None:
                    raise Hold("PG_COMMIT_EVIDENCE_INCOMPLETE")
                cur.execute("SELECT event_json FROM nal.outbox_intent WHERE receipt_ref = %s",
                            (receipt[0]["receipt_ref"],))
                event = cur.fetchone()
                if event is None or event[0].get("receipt_ref") != receipt[0]["receipt_ref"] or \
                        event[0].get("terminal_ref") != terminal_ref or \
                        receipt[0].get("terminal_digest") != occurrence[0].get("terminal_digest"):
                    raise Hold("PG_COMMIT_EVIDENCE_INCOMPLETE")
                return {"terminal": copy.deepcopy(occurrence[0]),
                        "receipt": copy.deepcopy(receipt[0]), "outbox_event": copy.deepcopy(event[0])}
        except Hold:
            raise
        except Exception as exc:
            raise Hold("PG_READ_UNAVAILABLE") from exc

    @staticmethod
    def _new_entries(old: dict | None, new: dict, key: str) -> dict:
        before = (old or {}).get(key, {})
        after = new.get(key, {})
        if any(k not in after or after[k] != v for k, v in before.items()):
            raise Hold("IMMUTABLE_LEDGER_CHANGED")
        return {k: v for k, v in after.items() if k not in before}

    def _append_ledgers(self, cur, old: dict | None, new: dict):
        actor_ref = new["actor_ref"]
        for ref, value in self._new_entries(old, new, "bindings").items():
            cur.execute("INSERT INTO nal.binding_issue(binding_ref, actor_ref, owner_revision, binding_json) "
                        "VALUES (%s, %s, %s, %s)",
                        (ref, actor_ref, value["owner_revision"], Jsonb(value)))
        for old_ref, value in self._new_entries(old, new, "supersessions").items():
            cur.execute("INSERT INTO nal.binding_supersession"
                        "(old_binding_ref, new_binding_ref, owner_revision, event_json) "
                        "VALUES (%s, %s, %s, %s)",
                        (old_ref, value["new_binding_ref"], value["owner_revision"], Jsonb(value)))
        for terminal_ref, value in self._new_entries(old, new, "terminals").items():
            cur.execute("INSERT INTO nal.terminal_occurrence"
                        "(actor_ref, terminal_ref, terminal_digest, binding_ref, claim_ref, packet_ref, queue_ref, occurrence_json) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (actor_ref, terminal_ref, value["terminal_digest"], value["binding_ref"],
                         value["claim_ref"], value["packet_ref"], value["queue_ref"], Jsonb(value)))
        for terminal_ref, value in self._new_entries(old, new, "receipts").items():
            cur.execute("INSERT INTO nal.close_receipt(receipt_ref, actor_ref, terminal_ref, receipt_json) "
                        "VALUES (%s, %s, %s, %s)",
                        (value["receipt_ref"], actor_ref, terminal_ref, Jsonb(value)))
        old_outbox, new_outbox = (old or {}).get("outbox", {}), new.get("outbox", {})
        for event_ref, prior in old_outbox.items():
            item = new_outbox.get(event_ref)
            if item is None or item["event"] != prior["event"] or \
                    (prior["delivered"] and item != prior):
                raise Hold("IMMUTABLE_OUTBOX_INTENT_CHANGED")
        for event_ref, item in new_outbox.items():
            if event_ref not in old_outbox:
                cur.execute("INSERT INTO nal.outbox_intent(event_ref, receipt_ref, event_json) "
                            "VALUES (%s, %s, %s)",
                            (event_ref, item["event"]["receipt_ref"], Jsonb(item["event"])))
            if item["delivered"] and not old_outbox.get(event_ref, {}).get("delivered", False):
                cur.execute("INSERT INTO nal.outbox_delivery(event_ref, delivery_receipt) VALUES (%s, %s)",
                            (event_ref, item["delivery_receipt"]))

    def apply(self, actor_ref: str, transition: Callable[[dict | None], tuple[dict, Result]],
              *, owner_fence_factory: Callable | None = None) -> Result:
        committed = False
        commit_attempted = False
        try:
            with self._connection() as conn:
                # ExitStack outlives the SQL transaction: the owner fence is
                # acquired after FOR UPDATE and released only after COMMIT.
                with ExitStack() as owner_fence_stack:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            self._preflight(cur)
                            cur.execute("SELECT state_json, aggregate_revision FROM nal.actor_state "
                                        "WHERE actor_ref = %s FOR UPDATE", (actor_ref,))
                            row = cur.fetchone()
                            old, revision = (copy.deepcopy(row[0]), row[1]) if row else (None, 0)
                            commit_guard = None
                            if owner_fence_factory:
                                commit_guard = owner_fence_stack.enter_context(
                                    owner_fence_factory(copy.deepcopy(old), cursor=cur))
                            new, result = transition(copy.deepcopy(old))
                            if result.mutated:
                                if new.get("actor_ref") != actor_ref:
                                    raise Hold("ACTOR_STATE_IDENTITY_MISMATCH")
                                if old is None:
                                    cur.execute("INSERT INTO nal.actor_state"
                                                "(actor_ref, generation_ref, aggregate_revision, state_json) "
                                                "VALUES (%s, %s, 1, %s)",
                                                (actor_ref, new["generation_ref"], Jsonb(new)))
                                else:
                                    if new["generation_ref"] != old["generation_ref"]:
                                        raise Hold("GENERATION_IMMUTABLE")
                                    cur.execute("UPDATE nal.actor_state SET state_json = %s, "
                                                "aggregate_revision = aggregate_revision + 1, updated_at = now() "
                                                "WHERE actor_ref = %s AND aggregate_revision = %s",
                                                (Jsonb(new), actor_ref, revision))
                                    if cur.rowcount != 1:
                                        raise Hold("STALE_AGGREGATE_REVISION")
                                self._append_ledgers(cur, old, new)
                                if commit_guard:
                                    commit_guard()
                                commit_attempted = True
            committed = True
            if result.mutated and canonical(self.read(actor_ref)) != canonical(new):
                raise Hold("PG_COMMIT_READBACK_UNKNOWN")
            if result.mutated and result.receipt is not None and \
                    result.receipt.get("schema") == "cerebro.nal.close-receipt/v1":
                evidence = self.read_commit_evidence(actor_ref, result.receipt["terminal_ref"])
                if evidence["receipt"] != result.receipt:
                    raise Hold("PG_COMMIT_READBACK_UNKNOWN")
            return result
        except Hold as exc:
            # A committed transaction with failed readback is ambiguous to the
            # caller. Never expose it as a retryable read error.
            if committed or commit_attempted:
                raise Hold("PG_COMMIT_OUTCOME_UNKNOWN") from exc
            raise
        except Exception as exc:
            raise Hold("PG_COMMIT_OUTCOME_UNKNOWN" if committed or commit_attempted
                       else "PG_TRANSACTION_UNAVAILABLE") from exc
