#!/usr/bin/env python3
"""Disposable PostgreSQL proof for the Project-owned D1 return ledger."""

from __future__ import annotations

import os
import secrets
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tooling" / "owner_state"),
                str(ROOT / "tooling" / "context")]


def main() -> int:
    dsn = os.environ.get("CEREBRO_D1_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("CEREBRO_D1_TEST_DISPOSABLE") != "YES":
        print("UNRUN: disposable PostgreSQL DSN and explicit test flag required")
        return 2
    import psycopg
    from psycopg import sql
    from project_d1_closure_return import PostgresProjectD1ClosureReturnPort

    schema = "cerebro_d1_test_" + secrets.token_hex(5)
    role = schema + "_role"
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT current_database(), rolsuper FROM pg_roles WHERE rolname=current_user")
            database, superuser = cursor.fetchone()
            if not database.startswith("test_") or superuser is not True:
                raise RuntimeError("disposable-test-database-and-superuser-required")
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOBYPASSRLS").format(sql.Identifier(role)))

        base = (ROOT / "tooling" / "context" / "control_context_state_postgres.sql").read_text(encoding="utf-8")
        candidate = (ROOT / "tooling" / "owner_state" / "project_d1_closure_ledger_candidate.sql").read_text(encoding="utf-8")
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            cursor.execute(base)
            cursor.execute(candidate)
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT, INSERT ON {}.cerebro_project_d1_closure_ledger TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))

        def connect():
            connection = psycopg.connect(dsn)
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            connection.commit()
            return connection

        class SyntheticOwnerReader:
            """No natural Project event is consumed or created in this test."""

            def read_committed_owner_event(self, *, tenant_ref, workspace_ref,
                                           project_ref, owner_event_key):
                return {"tenant_ref": tenant_ref, "workspace_ref": workspace_ref,
                        "project_ref": project_ref, "owner": "project",
                        "event_state": "OWNER_EFFECT_COMMITTED",
                        "owner_event_key": owner_event_key,
                        "owner_event_fingerprint": "e" * 64,
                        "provider_readback_verified": True}

        reader = SyntheticOwnerReader()
        port = PostgresProjectD1ClosureReturnPort(
            connection_factory=connect, owner_commit_reader=reader)

        def args(closure_id, fingerprint="a" * 64):
            return {"tenant_ref": "T", "workspace_ref": "W",
                    "project_ref": "PROJECT", "receiver_ref": "RECEIVER",
                    "closure_id": closure_id, "closure_fingerprint": fingerprint,
                    "owner_event_key": "SYNTHETIC-FIXTURE-OWNER-EVENT",
                    "owner_event_fingerprint": "e" * 64}

        first = port.append_and_confirm(**args("SEQUENTIAL"))
        assert first["result"] == "PASS" and first["closure_state"] == "CLOSED"
        assert first["mutated"] is True and first["closure_revision"] == 1
        restarted = PostgresProjectD1ClosureReturnPort(
            connection_factory=connect, owner_commit_reader=reader)
        replay = restarted.append_and_confirm(**args("SEQUENTIAL"))
        assert replay["result"] == "PASS" and replay["mutated"] is False
        assert replay["receipt"] == first["receipt"]
        assert replay["receipt_ref"] == first["receipt_ref"]
        assert replay["receipt_fingerprint"] == first["receipt_fingerprint"]
        conflict = restarted.append_and_confirm(**args("SEQUENTIAL", "b" * 64))
        assert conflict["result"] == "CONFLICT_HOLD" and conflict["mutated"] is False
        assert conflict["closure_state"] == "NOT_CLOSED"

        class DenyOwner:
            def read_committed_owner_event(self, **kwargs):
                return {"owner": "project", "event_state": "UNCOMMITTED"}

        denied = PostgresProjectD1ClosureReturnPort(
            connection_factory=connect, owner_commit_reader=DenyOwner())
        assert denied.append_and_confirm(**args("DENIED"))["closure_state"] == "NOT_CLOSED"

        def race(closure_id, fingerprints):
            barrier = threading.Barrier(2)
            results = []
            lock = threading.Lock()

            def run(fingerprint):
                local = PostgresProjectD1ClosureReturnPort(
                    connection_factory=connect, owner_commit_reader=reader)
                barrier.wait(timeout=10)
                result = local.append_and_confirm(**args(closure_id, fingerprint))
                with lock:
                    results.append(result)

            threads = [threading.Thread(target=run, args=(fingerprint,))
                       for fingerprint in fingerprints]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            assert all(not thread.is_alive() for thread in threads), "race-hung"
            return results

        for index in range(4):
            same = race(f"SAME-RACE-{index}", ("c" * 64, "c" * 64))
            assert len(same) == 2 and all(value["result"] == "PASS" for value in same), same
            assert sorted(value["mutated"] for value in same) == [False, True]
            assert same[0]["receipt_ref"] == same[1]["receipt_ref"]
            changed = race(f"CHANGED-RACE-{index}", ("d" * 64, "f" * 64))
            assert sorted(value["result"] for value in changed) == ["CONFLICT_HOLD", "PASS"], changed
            assert next(value for value in changed if value["result"] == "CONFLICT_HOLD")["mutated"] is False

        class FailSecondConnection:
            calls = 0
            def __call__(self):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("synthetic-readback-failure")
                return connect()

        failing = PostgresProjectD1ClosureReturnPort(
            connection_factory=FailSecondConnection(), owner_commit_reader=reader)
        held = failing.append_and_confirm(**args("READBACK-FAIL"))
        assert held == {"result": "HOLD", "closure_state": "NOT_CLOSED",
                        "mutated": True, "reason": "closure-provider-readback-unavailable"}, held
        recovered = restarted.append_and_confirm(**args("READBACK-FAIL"))
        assert recovered["result"] == "PASS" and recovered["mutated"] is False
        assert recovered["closure_state"] == "CLOSED"

        with admin.cursor() as cursor:
            cursor.execute("SELECT closure_id, count(*) FROM cerebro_project_d1_closure_ledger GROUP BY closure_id")
            assert dict(cursor.fetchall()) == {
                **{"SEQUENTIAL": 1, "READBACK-FAIL": 1},
                **{f"SAME-RACE-{index}": 1 for index in range(4)},
                **{f"CHANGED-RACE-{index}": 1 for index in range(4)}}
            try:
                cursor.execute("UPDATE cerebro_project_d1_closure_ledger SET closure_revision=1")
            except psycopg.errors.ObjectNotInPrerequisiteState:
                pass
            else:
                raise AssertionError("append-only-trigger-not-enforced")
        print("PASS: disposable Project owner-state sibling ledger; exact replay/conflict, "
              "four same/changed concurrent race pairs, restart/redelivery, readback HOLD then recovery, "
              "append-only/RLS role; no natural event", flush=True)
        return 0
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
