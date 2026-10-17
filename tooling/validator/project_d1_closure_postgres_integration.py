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
                str(ROOT / "tooling" / "context"),
                str(ROOT / "tooling" / "validator")]


def local_cleanup_regressions() -> list[dict[str, str]]:
    """Scripted DB-API cleanup canaries; no DSN, database, or natural owner event."""
    import copy
    import json

    from control_context_postgres_validation import ScriptedConnection, ScriptedCursor
    from control_context_state_postgres import _sha256
    from project_d1_closure_return import PostgresProjectD1ClosureReturnPort

    tests: list[dict[str, str]] = []

    def check(name, condition):
        tests.append({"name": "D1-SCRIPTED-CLEANUP-" + name,
                      "result": "PASS" if condition else "FAIL"})

    class CleanupCursor(ScriptedCursor):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.close()
            return False

    class CleanupConnection(ScriptedConnection):
        def __init__(self, steps, *, name, events, faults=()):
            super().__init__(steps, commit_error=(
                RuntimeError("scripted-commit-error") if "commit" in faults else None))
            self.cursor_instance = CleanupCursor(steps)
            self.expected_sql_count = len(steps)
            self.name, self.events, self.faults = name, events, faults
            self.close_calls = 0

        def commit(self):
            self.events.append(self.name + ":commit")
            super().commit()

        def rollback(self):
            self.events.append(self.name + ":rollback")
            super().rollback()
            if "rollback" in self.faults:
                raise RuntimeError("scripted-rollback-error")

        def close(self):
            self.events.append(self.name + ":close")
            self.close_calls += 1
            if "close" in self.faults:
                raise RuntimeError("scripted-close-error")
            super().close()

    args = {"tenant_ref": "SCRIPTED-T", "workspace_ref": "SCRIPTED-W",
            "project_ref": "SCRIPTED-P", "receiver_ref": "SCRIPTED-R",
            "closure_id": "SCRIPTED-D1-CLEANUP", "closure_fingerprint": "a" * 64,
            "owner_event_key": "SCRIPTED-OWNER-EVENT",
            "owner_event_fingerprint": "e" * 64}
    identity = tuple(args[key] for key in
                     ("tenant_ref", "workspace_ref", "project_ref", "receiver_ref", "closure_id"))
    head_key = identity[:3] + ("project", args["project_ref"])
    head = {"current_state_ref": "SCRIPTED-OWNER-STATE", "owner_revision": 1,
            "state_fingerprint": "a" * 64, "last_event_ref": args["owner_event_key"]}
    receipt = {"schema": "cerebro-project-d1-closure-receipt/v1", **args,
               "closure_revision": 1}
    receipt_ref = "D1C-" + _sha256(identity)[:32].upper()
    receipt_fingerprint = _sha256({**receipt, "receipt_ref": receipt_ref})
    row = {"closure_revision": 1, "closure_fingerprint": args["closure_fingerprint"],
           "owner_event_key": args["owner_event_key"],
           "owner_event_fingerprint": args["owner_event_fingerprint"],
           "receipt_ref": receipt_ref, "receipt_fingerprint": receipt_fingerprint,
           "receipt_payload": receipt}

    class ScriptedOwnerReader:
        """Synthetic owner assertion only; no authenticated provider is exercised."""

        def read_committed_owner_event(self, **kwargs):
            return {**kwargs, "owner": "project", "event_state": "OWNER_EFFECT_COMMITTED",
                    "owner_event_fingerprint": args["owner_event_fingerprint"],
                    "provider_readback_verified": True,
                    "authenticated_same_owner_readback": True, "currentness": "CURRENT",
                    "owner_revision": head["owner_revision"],
                    "owner_state_ref": head["current_state_ref"],
                    "owner_state_fingerprint": head["state_fingerprint"]}

    def scope_steps():
        return [{"contains": "SELECT set_config", "params": ("cerebro." + key, args[key])}
                for key in ("tenant_ref", "workspace_ref", "project_ref")]

    def exercise(*, write_mode="INSERT", read_mode="EXACT", write_faults=(), read_faults=()):
        write_head = {**head, "owner_revision": 2} if write_mode == "STALE_HEAD" else head
        write_steps = scope_steps() + [
            {"contains": "FROM cerebro_owner_state_heads", "params": head_key,
             "rows": [write_head]}]
        if write_mode != "STALE_HEAD":
            write_steps += [
                {"contains": "INSERT INTO cerebro_project_d1_closure_ledger",
                 "params": identity + (args["closure_fingerprint"], args["owner_event_key"],
                                        args["owner_event_fingerprint"], receipt_ref,
                                        receipt_fingerprint,
                                        json.dumps(receipt, sort_keys=True, separators=(",", ":"))),
                 "rows": [{"closure_revision": 1}] if write_mode == "INSERT" else []},
                {"contains": "FROM cerebro_project_d1_closure_ledger", "params": identity,
                 "rows": [{**row, "closure_fingerprint": "b" * 64}
                          if write_mode == "CONFLICT" else row]}]
        read_steps = [{"contains": "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"}]
        read_steps += scope_steps() + [
            {"contains": "FROM cerebro_owner_state_heads", "params": head_key, "rows": [head]},
            {"contains": "FROM cerebro_project_d1_closure_ledger", "params": identity,
             "rows": [{**row, "closure_fingerprint": "b" * 64}
                      if read_mode == "MISMATCH" else row]}]
        if read_mode == "SQL_ERROR":
            read_steps[-1]["error"] = RuntimeError("scripted-readback-error")
        events = []
        write = CleanupConnection(write_steps, name="write", events=events, faults=write_faults)
        read = CleanupConnection(read_steps, name="read", events=events, faults=read_faults)
        connections = iter((write, write if read_mode == "REUSE_WRITE" else read))

        def connect():
            connection = next(connections)
            events.append("connect:" + connection.name)
            return connection

        result = PostgresProjectD1ClosureReturnPort(
            connection_factory=connect, owner_commit_reader=ScriptedOwnerReader()
        ).append_and_confirm(**copy.deepcopy(args))
        return result, write, read, events

    def completed(connection):
        cursor = connection.cursor_instance
        return (not cursor.steps and len(cursor.executed) == connection.expected_sql_count
                and cursor.closed)

    def untouched(connection):
        return (not connection.cursor_instance.executed and not connection.commit_called
                and not connection.rollback_called and connection.close_calls == 0)

    def closed(result, mutation):
        return (result == {"result": "PASS", "closure_state": "CLOSED", "mutated": mutation,
                           "receipt_ref": receipt_ref, "receipt_fingerprint": receipt_fingerprint,
                           "closure_revision": 1, "receipt": receipt}
                and result["mutated"] is mutation)

    def held(result, reason, mutation, result_kind="HOLD"):
        return (result == {"result": result_kind, "closure_state": "NOT_CLOSED",
                           "mutated": mutation, "reason": reason}
                and (result["mutated"] is mutation if isinstance(mutation, bool)
                     else result["mutated"] == mutation))

    normal_events = ["connect:write", "write:commit", "write:close",
                     "connect:read", "read:commit", "read:close"]
    result, write, read, events = exercise()
    check("write-then-independent-exact-readback",
          closed(result, True) and write is not read and completed(write) and completed(read)
          and events == normal_events)

    result, write, read, events = exercise(write_faults=("close",))
    check("write-close-error-still-runs-independent-exact-readback",
          closed(result, True) and completed(write) and completed(read)
          and write.close_calls == 1 and read.close_calls == 1 and events == normal_events)

    result, write, read, events = exercise(read_mode="REUSE_WRITE", write_faults=("close",))
    check("write-close-error-cannot-substitute-same-connection-readback",
          held(result, "closure-independent-readback-required", True)
          and completed(write) and untouched(read) and write.close_calls == 2
          and events == ["connect:write", "write:commit", "write:close",
                         "connect:write", "write:close"])

    result, write, read, events = exercise(write_faults=("commit", "rollback", "close"))
    check("commit-rollback-close-errors-preserve-unknown-without-readback",
          held(result, "closure-write-commit-unproven", "UNKNOWN")
          and completed(write) and untouched(read) and write.rollback_called
          and events == ["connect:write", "write:commit", "write:rollback", "write:close"])

    for name, read_mode, faults, reason, read_events in (
        ("readback-sql-error-and-cleanup-errors-remain-not-closed", "SQL_ERROR",
         ("rollback", "close"), "closure-provider-readback-unavailable",
         ["read:rollback", "read:close"]),
        ("readback-mismatch-and-close-error-remain-not-closed", "MISMATCH",
         ("close",), "closure-exact-provider-readback-mismatch",
         ["read:commit", "read:close"]),
        ("readback-commit-and-cleanup-errors-remain-not-closed", "EXACT",
         ("commit", "rollback", "close"), "closure-provider-readback-unavailable",
         ["read:commit", "read:rollback", "read:close"]),
    ):
        result, write, read, events = exercise(read_mode=read_mode, read_faults=faults)
        check(name, held(result, reason, True) and completed(write) and completed(read)
              and read.close_calls == 1 and events == normal_events[:4] + read_events)

    result, write, read, events = exercise(write_mode="REPLAY", write_faults=("close",),
                                         read_faults=("close",))
    check("exact-replay-with-close-errors-stays-unmutated-after-independent-readback",
          closed(result, False) and completed(write) and completed(read)
          and events == normal_events)

    result, write, read, events = exercise(write_mode="CONFLICT", write_faults=("close",))
    check("conflict-and-close-error-preserve-unmutated-hold",
          held(result, "closure-key-fingerprint-conflict", False, "CONFLICT_HOLD")
          and completed(write) and untouched(read) and not write.commit_called
          and events == ["connect:write", "write:rollback", "write:close"])

    result, write, read, events = exercise(write_mode="STALE_HEAD", write_faults=("close",))
    check("stale-head-and-close-error-preserve-unmutated-hold",
          held(result, "project-owner-head-revision-or-event-changed", False)
          and completed(write) and untouched(read) and not write.commit_called
          and events == ["connect:write", "write:rollback", "write:close"])
    return tests


def main() -> int:
    dsn = os.environ.get("CEREBRO_D1_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("CEREBRO_D1_TEST_DISPOSABLE") != "YES":
        print("UNRUN: disposable PostgreSQL DSN and explicit test flag required")
        return 2
    cleanup_tests = local_cleanup_regressions()
    assert all(test["result"] == "PASS" for test in cleanup_tests), cleanup_tests
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
            cursor.execute(sql.SQL("GRANT SELECT, UPDATE ON {}.cerebro_owner_state_heads TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute("""INSERT INTO cerebro_project_instances
                (tenant_ref,workspace_ref,project_ref,aggregate_id,source_revision,
                 project_status,aggregate_revision,aggregate_fingerprint)
                VALUES ('T','W','PROJECT','SYNTHETIC-AGGREGATE','TEST','ACTIVE',1,%s)""",
                ("a" * 64,))
            cursor.execute("""INSERT INTO cerebro_owner_state_heads
                (tenant_ref,workspace_ref,project_ref,owner,aggregate_ref,
                 current_state_ref,owner_revision,state_schema,state_payload,
                 state_fingerprint,last_event_ref)
                VALUES ('T','W','PROJECT','project','PROJECT',%s,1,%s,'{}'::jsonb,%s,%s)""",
                ("SYNTHETIC-STATE-N", "synthetic-fixture/v1", "a" * 64,
                 "SYNTHETIC-FIXTURE-OWNER-EVENT"))

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
                        "provider_readback_verified": True,
                        "authenticated_same_owner_readback": True,
                        "currentness": "CURRENT", "owner_revision": 1,
                        "owner_state_ref": "SYNTHETIC-STATE-N",
                        "owner_state_fingerprint": "a" * 64}

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

        class CountingConnection:
            calls = 0
            def __call__(self):
                self.calls += 1
                raise AssertionError("invalid-owner-proof-reached-connection-factory")

        class AlteredOwner:
            def __init__(self, change):
                self.change = change
            def read_committed_owner_event(self, **kwargs):
                proof = reader.read_committed_owner_event(**kwargs)
                proof.update(self.change)
                return proof

        for index, change in enumerate((
            {"currentness": "STALE"},
            {"authenticated_same_owner_readback": False},
            {"owner_revision": None},
            {"owner_state_fingerprint": "f" * 64, "currentness": "UNKNOWN"},
            {"owner": "quality"},
            {"provider_readback_verified": False},
        )):
            no_connection = CountingConnection()
            invalid = PostgresProjectD1ClosureReturnPort(
                connection_factory=no_connection, owner_commit_reader=AlteredOwner(change))
            rejected = invalid.append_and_confirm(**args(f"INVALID-{index}"))
            assert rejected == {"result": "HOLD", "closure_state": "NOT_CLOSED",
                                "mutated": False, "reason": "project-owner-commit-not-proven"}
            assert no_connection.calls == 0

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

        # Reader captured N; the authoritative owner head moved to N+1 before append.
        with admin.cursor() as cursor:
            cursor.execute("""UPDATE cerebro_owner_state_heads
                SET owner_revision=2,current_state_ref='SYNTHETIC-STATE-N1',
                    state_fingerprint=%s,last_event_ref='SYNTHETIC-REASSIGN-EVENT'
                WHERE tenant_ref='T' AND workspace_ref='W' AND project_ref='PROJECT'
                  AND owner='project' AND aggregate_ref='PROJECT'""", ("b" * 64,))
        stale = restarted.append_and_confirm(**args("REASSIGNED-BEFORE-APPEND"))
        assert stale == {"result": "HOLD", "closure_state": "NOT_CLOSED",
                         "mutated": False, "reason": "project-owner-head-revision-or-event-changed"}, stale
        with admin.cursor() as cursor:
            cursor.execute("""SELECT count(*) FROM cerebro_project_d1_closure_ledger
                WHERE closure_id='REASSIGNED-BEFORE-APPEND'""")
            assert cursor.fetchone()[0] == 0, "stale-owner-must-not-write"

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
        with connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('cerebro.tenant_ref','T',true)")
                cursor.execute("SELECT set_config('cerebro.workspace_ref','W',true)")
                cursor.execute("SELECT set_config('cerebro.project_ref','OTHER',true)")
                cursor.execute("SELECT count(*) FROM cerebro_project_d1_closure_ledger")
                assert cursor.fetchone()[0] == 0, "cross-project-RLS-visibility"
        print("PASS: disposable Project owner-state sibling ledger; exact replay/conflict, "
              "four same/changed concurrent race pairs, restart/redelivery, readback HOLD then recovery, "
              "append-only and cross-project RLS; stale/unauthenticated owner proof and "
              "N-to-N+1 pre-append fence; no natural event", flush=True)
        return 0
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
