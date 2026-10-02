#!/usr/bin/env python3
"""Full A2 candidate bridge against an explicitly disposable PostgreSQL DB."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path


def main() -> int:
    dsn = os.environ.get("CEREBRO_A2_TEST_POSTGRES_DSN")
    root = Path(os.environ.get("CEREBRO_A2_CANDIDATE_ROOT", Path(__file__).resolve().parents[2]))
    if not dsn or os.environ.get("CEREBRO_A2_TEST_DISPOSABLE") != "YES":
        print("UNRUN: disposable PostgreSQL DSN and explicit test flag required")
        return 2
    import psycopg
    from psycopg import sql

    sys.path[:0] = [str(root / "mcp"), str(root / "tooling" / "context")]
    from control_context_state_postgres import (  # noqa: E402
        PostgresControlContextStatePort, StateBindingError, StateConflict,
        apply_postgres_migrations,
    )
    from control_context_tools import (  # noqa: E402
        ControlContextMcpTools, HmacControlResolutionAttestor, VerifiedMcpIdentity,
    )
    from project_commissioning_candidate import (  # noqa: E402
        ProjectCommissioningBridge, ProjectCommissioningHold,
    )

    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], text=True).strip()
    print(f"FULL_CHAIN_SOURCE_COMMIT={commit}", flush=True)
    print(f"FULL_CHAIN_SOURCE_TREE={tree}", flush=True)
    schema = "cerebro_a2_full_" + secrets.token_hex(5)
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

        def admin_connection():
            connection = psycopg.connect(dsn)
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            connection.commit()
            return connection

        manifest = root / "tooling" / "context" / "control_context_project_commissioning_migrations.json"
        result = apply_postgres_migrations(admin_connection, manifest)
        expected_ids = [f"000{i}-" for i in range(1, 7)]
        assert len(result["applied"]) == 6 and all(
            name.startswith(prefix) for name, prefix in zip(result["applied"], expected_ids)
        ), result
        replay = apply_postgres_migrations(admin_connection, manifest)
        assert replay["applied"] == [] and replay["already_applied"] == result["applied"]
        print("FULL_CHAIN_MIGRATIONS=0001..0006_APPLIED_AND_REPLAY_VERIFIED", flush=True)

        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)))

        def runtime_connection():
            connection = psycopg.connect(dsn)
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
                cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            connection.commit()
            return connection

        port = PostgresControlContextStatePort(runtime_connection)
        attestor = HmacControlResolutionAttestor(
            key_id="DISPOSABLE_CI", secret=b"disposable-ci-only-synthetic-attestor-secret")
        tools = ControlContextMcpTools(port, attestor)

        class Lineage:
            def authorize(self, *, identity, project_ref, aggregate_id):
                return {"result": "PASS", "project_ref": project_ref,
                        "aggregate_id": aggregate_id, "lineage_ref": "DISPOSABLE-CI-LINEAGE"}

        bridge = ProjectCommissioningBridge(
            state_port=port, tools=tools, issuer=attestor, verifier=attestor,
            lineage_authorizer=Lineage())

        def identity(principal):
            return VerifiedMcpIdentity(
                tenant_ref="T", workspace_ref="W", principal_ref=principal,
                consumer_ref="CI_CONSUMER_" + principal,
                scopes=frozenset({"project_state:read", "project_state:transition"}),
                token_verified=True)

        def args(project):
            return {"project_ref": project, "aggregate_id": "AGG-" + project,
                    "source_revision": "CI-SOURCE", "event_id": "BOOT-" + project,
                    "decision_ref": "CI-DECISION",
                    "root": {"context_id": "ROOT-" + project, "human_label": project,
                             "objective_ref": "CI-OBJECTIVE", "scope_ref": "CI-SCOPE"}}

        p1, p2 = identity("P1"), identity("P2")
        scope = {"tenant_ref": "T", "workspace_ref": "W", "principal_ref": "P1",
                 "scopes": p1.state_scopes}
        port.bootstrap_project(**scope, **args("DEFAULT"), make_default=True)
        before = port.read_principal_default_binding(**scope)
        assert before["status"] == "PRESENT" and before["active_project_ref"] == "DEFAULT"
        first = bridge.start(identity=p1, args=args("PROJECT"))
        assert first["result"] == "PASS" and first["phase"] == "CONTEXT_ONLY"
        assert first["project_basis_initialized"] is False and first["default_binding"] == before
        resumed = bridge.resume(identity=p1, args={
            "project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
            "session_handle": first["session_handle"]})
        assert resumed["result"] == "PASS"
        for caller in (p1, p2):
            try:
                bridge.start(identity=caller, args=args("PROJECT"))
            except ProjectCommissioningHold as exc:
                assert str(exc) == "project-ref-collision", str(exc)
            else:
                raise AssertionError("repeat-or-cross-identity-start-accepted")
        try:
            bridge.resume(identity=p2, args={
                "project_ref": "PROJECT", "aggregate_id": "AGG-PROJECT",
                "session_handle": first["session_handle"]})
        except StateBindingError as exc:
            assert str(exc) == "control-session-not-bound", str(exc)
        else:
            raise AssertionError("foreign-session-resume-accepted")
        assert bridge.start(identity=p2, args=args("OTHER"))["phase"] == "CONTEXT_ONLY"

        # P2 cannot see P1's session row; the actual port must still reject it.
        try:
            port.bind_session(tenant_ref="T", workspace_ref="W", principal_ref="P2",
                              consumer_ref=p2.consumer_ref,
                              session_ref="project-commissioning:" + "B" * 43,
                              session_binding_id="CI-HIDDEN-COLLISION",
                              scopes=p2.state_scopes, project_ref="PROJECT",
                              reject_existing=True,
                              reject_project_commissioning_existing=True)
        except StateConflict as exc:
            assert str(exc) == "commissioning-project-session-conflict", str(exc)
        else:
            raise AssertionError("RLS-hidden-port-bind-accepted")

        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def race(caller):
            barrier.wait(timeout=10)
            try:
                value = bridge.start(identity=caller, args=args("RACE"))
                outcome = ("PASS", value["session_handle"])
            except ProjectCommissioningHold as exc:
                outcome = ("HOLD", str(exc))
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=race, args=(caller,)) for caller in (p1, p2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads), "race-thread-hung"
        assert sorted(item[0] for item in outcomes) == ["HOLD", "PASS"], outcomes
        loser = next(item[1] for item in outcomes if item[0] == "HOLD")
        assert loser in {"project-ref-collision", "context-bootstrap-unknown-no-auto-retry"}, outcomes
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            cursor.execute("SELECT count(*) FROM cerebro_project_instances WHERE project_ref='RACE'")
            assert cursor.fetchone()[0] == 1
            cursor.execute("""SELECT count(*) FROM cerebro_control_session_bindings
                WHERE project_ref='RACE' AND session_ref ~ '^project-commissioning:[A-Za-z0-9_-]{43}$'""")
            assert cursor.fetchone()[0] == 1
        assert port.read_principal_default_binding(**scope) == before
        assert port.read_principal_default_binding(
            tenant_ref="T", workspace_ref="W", principal_ref="P2",
            scopes=p2.state_scopes) == {"status": "ABSENT"}
        print(f"TWO_IDENTITY_RACE=ONE_PASS_ONE_{loser}", flush=True)
        print("PASS: full migration chain, actual bridge start/resume/repeat/two-identity race, "
              "RLS-hidden port conflict, exact default readback", flush=True)
        return 0
    finally:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
            cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
