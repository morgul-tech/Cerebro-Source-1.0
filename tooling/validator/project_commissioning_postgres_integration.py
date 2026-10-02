#!/usr/bin/env python3
"""Actual PostgreSQL/RLS A2 uniqueness test in an explicitly disposable DB.

Requires CEREBRO_A2_TEST_POSTGRES_DSN and CEREBRO_A2_TEST_DISPOSABLE=YES.
The DSN must name a disposable database and connect as a role able to create
and drop a test role. This script never runs against the deployed Context DB.
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
from pathlib import Path


def main() -> int:
    dsn = os.environ.get("CEREBRO_A2_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("CEREBRO_A2_TEST_DISPOSABLE") != "YES":
        print("UNRUN: disposable PostgreSQL DSN and explicit test flag required")
        return 2
    import psycopg
    from psycopg import sql

    schema = "cerebro_a2_it_" + secrets.token_hex(5)
    role = schema + "_role"
    migration = (Path(__file__).resolve().parents[1] / "context" /
                 "control_context_state_postgres_0006_project_commissioning_session.sql").read_text(
                     encoding="utf-8")
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute("SELECT current_database(), rolsuper FROM pg_roles WHERE rolname=current_user")
            database, superuser = cursor.fetchone()
            if not database.startswith("test_") or superuser is not True:
                raise RuntimeError("disposable-test-database-and-superuser-required")
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOBYPASSRLS").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            cursor.execute("""CREATE TABLE cerebro_control_session_bindings (
                tenant_ref text NOT NULL, workspace_ref text NOT NULL,
                principal_ref text NOT NULL, consumer_ref text NOT NULL,
                project_ref text NOT NULL, session_ref text NOT NULL,
                PRIMARY KEY (tenant_ref,workspace_ref,principal_ref,consumer_ref,session_ref))""")
            cursor.execute("ALTER TABLE cerebro_control_session_bindings ENABLE ROW LEVEL SECURITY")
            cursor.execute("ALTER TABLE cerebro_control_session_bindings FORCE ROW LEVEL SECURITY")
            cursor.execute("""CREATE POLICY principal_scope ON cerebro_control_session_bindings
                USING (tenant_ref=current_setting('cerebro.tenant_ref',true)
                   AND workspace_ref=current_setting('cerebro.workspace_ref',true)
                   AND principal_ref=current_setting('cerebro.principal_ref',true))
                WITH CHECK (tenant_ref=current_setting('cerebro.tenant_ref',true)
                   AND workspace_ref=current_setting('cerebro.workspace_ref',true)
                   AND principal_ref=current_setting('cerebro.principal_ref',true))""")
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT,INSERT ON {}.cerebro_control_session_bindings TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute("""INSERT INTO cerebro_control_session_bindings
                (tenant_ref,workspace_ref,principal_ref,consumer_ref,project_ref,session_ref)
                VALUES ('T','W','P1','C1','PREFLIGHT',%s),
                       ('T','W','P2','C2','PREFLIGHT',%s)""",
                ("project-commissioning:" + "X" * 43,
                 "project-commissioning:" + "Y" * 43))
            try:
                cursor.execute(migration)
            except psycopg.errors.UniqueViolation:
                pass
            else:
                raise AssertionError("duplicate-preflight-did-not-hold")
            cursor.execute("DELETE FROM cerebro_control_session_bindings WHERE principal_ref='P2'")
            cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            try:
                cursor.execute(migration)
            except psycopg.errors.InsufficientPrivilege:
                pass
            else:
                raise AssertionError("RLS-visibility-preflight-did-not-hold")
            finally:
                cursor.execute("RESET ROLE")
            cursor.execute(migration)

        def connect(principal: str):
            connection = psycopg.connect(dsn)
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                cursor.execute("SELECT set_config('cerebro.tenant_ref','T',false)")
                cursor.execute("SELECT set_config('cerebro.workspace_ref','W',false)")
                cursor.execute("SELECT set_config('cerebro.principal_ref',%s,false)", (principal,))
            connection.commit()
            return connection

        def insert(connection, *, principal, consumer, project, handle):
            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO cerebro_control_session_bindings
                    (tenant_ref,workspace_ref,principal_ref,consumer_ref,project_ref,session_ref)
                    VALUES ('T','W',%s,%s,%s,%s)""",
                    (principal, consumer, project, "project-commissioning:" + handle))
            connection.commit()

        first = connect("P1")
        second = connect("P2")
        try:
            insert(first, principal="P1", consumer="C1", project="PROJECT", handle="A" * 43)
            with second.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM cerebro_control_session_bindings WHERE project_ref='PROJECT'")
                assert cursor.fetchone()[0] == 0, "RLS-hidden-row-test-invalid"
            try:
                insert(second, principal="P2", consumer="C2", project="PROJECT", handle="B" * 43)
            except psycopg.errors.UniqueViolation as exc:
                assert exc.diag.constraint_name == "cerebro_one_project_commissioning_session"
                second.rollback()
            else:
                raise AssertionError("second-identity-project-bind-was-accepted")
            insert(second, principal="P2", consumer="C2", project="OTHER", handle="C" * 43)
            with second.cursor() as cursor:
                cursor.execute("""INSERT INTO cerebro_control_session_bindings
                    (tenant_ref,workspace_ref,principal_ref,consumer_ref,project_ref,session_ref)
                    VALUES ('T','W','P2','C2','PROJECT','ordinary-session')""")
            second.commit()
            with first.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT count(*) FROM cerebro_control_session_bindings WHERE session_ref=%s",
                               ("project-commissioning:" + "A" * 43,))
                assert cursor.fetchone()[0] == 1
            first.rollback()
            with second.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT count(*) FROM cerebro_control_session_bindings WHERE session_ref=%s",
                               ("project-commissioning:" + "A" * 43,))
                assert cursor.fetchone()[0] == 0
            second.rollback()
        finally:
            first.close()
            second.close()

        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()
        def race(principal: str, consumer: str, handle: str):
            connection = connect(principal)
            try:
                barrier.wait(timeout=10)
                try:
                    insert(connection, principal=principal, consumer=consumer,
                           project="RACE", handle=handle)
                    outcome = "PASS"
                except psycopg.errors.UniqueViolation as exc:
                    assert exc.diag.constraint_name == "cerebro_one_project_commissioning_session"
                    connection.rollback()
                    outcome = "CONFLICT"
                with lock:
                    outcomes.append(outcome)
            finally:
                connection.close()
        a = threading.Thread(target=race, args=("P1", "C1", "D" * 43))
        b = threading.Thread(target=race, args=("P2", "C2", "E" * 43))
        a.start(); b.start(); a.join(timeout=20); b.join(timeout=20)
        assert not a.is_alive() and not b.is_alive()
        assert sorted(outcomes) == ["CONFLICT", "PASS"], outcomes
        print("PASS: RLS-hidden sequential and concurrent cross-identity unique index, other project, ordinary session, read-only resume")
        return 0
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
