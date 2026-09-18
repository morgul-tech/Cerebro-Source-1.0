#!/usr/bin/env python3
"""DEEP contract tests for the candidate PostgreSQL state-port adapter.

These tests intentionally use a scripted DB-API boundary.  They prove query
ordering, exact receipt binding, commit/rollback behavior and migration drift
handling; they are not evidence of a live PostgreSQL deployment.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable


SOURCE_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_ROOT = SOURCE_ROOT / "tooling" / "context"
if str(CONTEXT_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTEXT_ROOT))

from control_context_registry import (  # noqa: E402
    DIRECTIVE_SCHEMA,
    bind_control_session,
    bootstrap_project_state,
)
from control_context_state_port import (  # noqa: E402
    StateAuthorizationError,
    StateConflict,
    StateServiceUnavailable,
)
from control_context_state_postgres import (  # noqa: E402
    DEFAULT_MANIFEST,
    MigrationDriftError,
    PostgresControlContextStatePort,
    apply_postgres_migrations,
    validate_state_commit_receipt,
)


def _normalized(value: str) -> str:
    return " ".join(value.split()).lower()


class ScriptedDatabaseError(RuntimeError):
    def __init__(self, sqlstate: str):
        super().__init__("scripted-database-error")
        self.sqlstate = sqlstate


class ScriptedCursor:
    def __init__(self, steps: list[dict[str, Any]]):
        self.steps = copy.deepcopy(steps)
        self.executed: list[str] = []
        self._rows: list[Any] = []
        self.rowcount = -1
        self.description = None
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> None:
        if not self.steps:
            raise AssertionError("unexpected-scripted-SQL:" + _normalized(sql)[:120])
        step = self.steps.pop(0)
        actual = _normalized(sql)
        expected = _normalized(step["contains"])
        if expected not in actual:
            raise AssertionError(f"scripted-SQL-order-mismatch:expected={expected}:actual={actual[:180]}")
        if "params" in step and tuple(params or ()) != tuple(step["params"]):
            raise AssertionError("scripted-SQL-parameter-binding-mismatch")
        self.executed.append(actual)
        if "error" in step:
            raise step["error"]
        self._rows = copy.deepcopy(step.get("rows", []))
        self.rowcount = int(step.get("rowcount", len(self._rows)))

    def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[Any]:
        rows = self._rows
        self._rows = []
        return rows

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    def __init__(self, steps: list[dict[str, Any]], *, commit_error: BaseException | None = None):
        self.cursor_instance = ScriptedCursor(steps)
        self.commit_error = commit_error
        self.commit_called = False
        self.rollback_called = False
        self.closed = False

    def cursor(self) -> ScriptedCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commit_called = True
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollback_called = True

    def close(self) -> None:
        self.closed = True


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


def _fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    project, _ = bootstrap_project_state(
        aggregate_id="AGG-PG-CONTRACT",
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        project_ref="TOTAL_MCP_REVISION",
        source_revision="contract-fixture",
        event_id="EVENT-BOOTSTRAP",
        decision_ref="MCPD-BOOTSTRAP",
        root={
            "context_id": "CTX-ROOT",
            "human_label": "Fortsett hovedsporet",
            "objective_ref": "OBJ-TOTAL-MCP-REVISION",
            "scope_ref": "SCOPE-TOTAL-MCP-REVISION",
            "basis_refs": ["HANDOFF"],
            "project_basis_ref": "PB-1",
            "quality_trace_ref": "QT-1",
            "completion_criteria_refs": ["DONE"],
        },
    )
    session = bind_control_session(
        project,
        session_binding_id="CSB-PG-CONTRACT",
        principal_ref="PRINCIPAL-1",
        consumer_ref="CONSUMER-1",
        session_ref="SESSION-1",
    )
    binding = {
        "schema": "cerebro-control-context-event-binding/v1",
        "event_id": "EVENT-1",
        "idempotency_key": "IDEMPOTENCY-1",
        "project": copy.deepcopy(project),
        "session": copy.deepcopy(session),
        "expected_project_revision": project["revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"],
        "expected_session_fingerprint": session["fingerprint"],
        "repository_permission_required": False,
        "rehydration_receipt": None,
    }
    directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "EVENT-1",
        "decision_ref": "MCPD-NOOP",
        "expected_project_revision": project["revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "expected_session_revision": session["session_revision"],
        "expected_session_fingerprint": session["fingerprint"],
        "project_operations": [],
        "session_operations": [],
    }
    request = {
        "tenant_ref": "TENANT-1",
        "workspace_ref": "WORKSPACE-1",
        "principal_ref": "PRINCIPAL-1",
        "consumer_ref": "CONSUMER-1",
        "session_ref": "SESSION-1",
        "event_id": "EVENT-1",
        "directive": directive,
        "navigation_options_candidate_fingerprint": None,
    }
    event = {
        "project_ref": project["project_ref"],
        "event_id": "EVENT-1",
        "idempotency_key": "IDEMPOTENCY-1",
        "begin_request_fingerprint": "1" * 64,
        "completion_request_fingerprint": None,
        "completion_fingerprint": None,
        "expected_project_revision": project["revision"],
        "expected_session_revision": session["session_revision"],
        "expected_project_fingerprint": project["fingerprint"],
        "expected_session_fingerprint": session["fingerprint"],
        "event_state": "OPEN",
        "event_payload": {"binding": binding, "completion": None},
    }
    return project, session, binding, request, event


def _project_row(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "aggregate_id": project["aggregate_id"],
        "source_revision": project["source_revision"],
        "project_status": project["project_status"],
        "default_context_ref": project["default_context_ref"],
        "aggregate_revision": project["revision"],
        "aggregate_fingerprint": project["fingerprint"],
        "next_sequence": project["next_sequence"],
    }


def _context_rows(project: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for context in project["contexts"]:
        rows.append(
            {
                "context_id": context["context_id"],
                "parent_context_ref": context["parent_context_ref"],
                "derived_from_context_ref": context["derived_from_context_ref"],
                "lifecycle": context["lifecycle"],
                "control_condition": context["control_condition"],
                "disposition": context["disposition"],
                "sequence": context["sequence"],
                "context_payload": copy.deepcopy(context),
                "context_fingerprint": context["context_fingerprint"],
            }
        )
    return rows


def _session_row(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_binding_id": session["session_binding_id"],
        "project_ref": session["project_ref"],
        "active_context_ref": session["active_context_ref"],
        "project_revision": session["project_revision"],
        "session_revision": session["session_revision"],
        "session_fingerprint": session["fingerprint"],
    }


def _completion_steps(project: dict[str, Any], session: dict[str, Any], event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"contains": "set_config('cerebro.tenant_ref'"},
        {"contains": "set_config('cerebro.workspace_ref'"},
        {"contains": "set_config('cerebro.principal_ref'"},
        {"contains": "SET CONSTRAINTS ALL DEFERRED"},
        {"contains": "FROM cerebro_control_events", "rows": [event]},
        {"contains": "FROM cerebro_project_instances", "rows": [_project_row(project)]},
        {"contains": "FROM cerebro_control_contexts", "rows": _context_rows(project)},
        {"contains": "FROM cerebro_control_session_bindings", "rows": [_session_row(session)]},
        {"contains": "FROM cerebro_continuation_bindings", "rows": []},
        {"contains": "INSERT INTO cerebro_transition_receipts", "rowcount": 1},
        {"contains": "INSERT INTO cerebro_state_commit_receipts", "rowcount": 1},
        {"contains": "UPDATE cerebro_control_events", "rowcount": 1},
    ]


def _schema_shape_valid(receipt: dict[str, Any]) -> bool:
    schema = json.loads((SOURCE_ROOT / "mcp" / "state-service-commit-receipt.schema.json").read_text(encoding="utf-8"))
    required = set(schema["required"])
    properties = set(schema["properties"])
    return (
        not required.difference(receipt)
        and not set(receipt).difference(properties)
        and receipt["schema"] == schema["properties"]["schema"]["const"]
        and receipt["commit_ref"].startswith("SSC-")
        and len(receipt["commit_fingerprint"]) == 64
    )


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    sql_path = CONTEXT_ROOT / "control_context_state_postgres.sql"
    shadow_sql_path = CONTEXT_ROOT / "control_context_state_postgres_0002_actor_claim_shadow.sql"
    adapter_path = CONTEXT_ROOT / "control_context_state_postgres.py"
    sql = sql_path.read_text(encoding="utf-8")
    shadow_sql = shadow_sql_path.read_text(encoding="utf-8")
    adapter_source = adapter_path.read_text(encoding="utf-8")
    required_tables = {
        "cerebro_project_instances",
        "cerebro_control_contexts",
        "cerebro_project_bootstrap_receipts",
        "cerebro_control_session_bindings",
        "cerebro_continuation_bindings",
        "cerebro_control_events",
        "cerebro_transition_receipts",
        "cerebro_state_commit_receipts",
        "cerebro_owner_state_heads",
        "cerebro_owner_state_revisions",
        "cerebro_owner_state_commit_receipts",
    }
    check(
        "postgres-contract-has-separate-bootstrap-and-event-ledgers",
        all(f"CREATE TABLE IF NOT EXISTS {name}" in sql for name in required_tables),
    )
    check(
        "postgres-contract-forces-row-level-security",
        "ENABLE ROW LEVEL SECURITY" in sql
        and "FORCE ROW LEVEL SECURITY" in sql
        and "cerebro.tenant_ref" in sql
        and "cerebro.workspace_ref" in sql
        and "cerebro.principal_ref" in sql,
    )
    check(
        "bootstrap-ledger-does-not-require-session-event-foreign-key",
        "CREATE TABLE IF NOT EXISTS cerebro_project_bootstrap_receipts" in sql
        and "FOREIGN KEY (tenant_ref, workspace_ref, project_ref)" in sql,
    )
    check(
        "normal-completion-has-distinct-state-commit-proof",
        "CREATE TABLE IF NOT EXISTS cerebro_state_commit_receipts" in sql
        and "transition_receipt_fingerprint" in sql
        and "commit_fingerprint" in sql,
    )
    check(
        "owner-revision-and-commit-ledgers-are-immutable",
        "cerebro_reject_immutable_ledger_mutation" in sql
        and "cerebro_owner_state_revisions" in sql
        and "cerebro_owner_state_commit_receipts" in sql
        and "cerebro_mutable_state_no_delete" in sql,
    )
    check(
        "mutable-state-projections-reject-hard-delete",
        all(
            f"'{table}'" in sql
            for table in (
                "cerebro_project_instances",
                "cerebro_principal_project_bindings",
                "cerebro_control_contexts",
                "cerebro_control_session_bindings",
                "cerebro_continuation_bindings",
                "cerebro_control_events",
                "cerebro_owner_state_heads",
            )
        )
        and "CREATE TRIGGER %I BEFORE DELETE ON %I" in sql,
    )
    check(
        "commit-ledgers-bind-same-principal-session-event-as-proven-revision",
        sql.count(
            "tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id\n"
            "    ) REFERENCES"
        ) >= 2,
    )
    check(
        "adapter-sets-transaction-local-verified-identity",
        all(
            marker in adapter_source
            for marker in (
                "set_config('cerebro.tenant_ref'",
                "set_config('cerebro.workspace_ref'",
                "set_config('cerebro.principal_ref'",
            )
        ),
    )
    check(
        "adapter-contains-lock-CAS-and-idempotency-guards",
        "FOR UPDATE" in adapter_source
        and "cursor.rowcount != 1" in adapter_source
        and "ON CONFLICT DO NOTHING" in adapter_source
        and "idempotency-key-reused-with-different-begin-request" in adapter_source,
    )
    forbidden_runtime_surface = ("github", "repo:write", "git push", "personal access token")
    check(
        "state-adapter-exposes-no-source-write-credential-surface",
        not any(term in adapter_source.lower() for term in forbidden_runtime_surface),
    )
    check(
        "B1-shadow-migration-is-additive-non-live-and-workspace-isolated",
        all(marker in shadow_sql for marker in (
            "CREATE TABLE IF NOT EXISTS cerebro_actor_generation_shadow_heads",
            "CREATE TABLE IF NOT EXISTS cerebro_actor_generation_shadow_revisions",
            "CREATE TABLE IF NOT EXISTS cerebro_work_claim_shadow_heads",
            "CREATE TABLE IF NOT EXISTS cerebro_work_claim_shadow_revisions",
            "SHADOW_ONLY", "live_claim", "ENABLE ROW LEVEL SECURITY",
        ))
        and "DROP TABLE" not in shadow_sql.upper(),
    )
    check(
        "B1-adapter-has-CAS-and-immutable-history-for-both-shadow-aggregates",
        all(marker in adapter_source for marker in (
            "write_actor_generation_shadow", "write_work_claim_shadow",
            "actor-generation-shadow-revision-conflict", "work-claim-shadow-revision-conflict",
            "INSERT INTO cerebro_actor_generation_shadow_revisions",
            "INSERT INTO cerebro_work_claim_shadow_revisions",
        )),
    )

    check(
        "K154-allocation-receipt-and-stable-unknown-fit-existing-jsonb-no-sql-migration",
        shadow_sql.count("shadow_payload jsonb NOT NULL") >= 4
        and "provider_allocation_receipt" not in shadow_sql
        and "stable_unknown" not in shadow_sql
        and "%s::jsonb" in adapter_source,
    )
    check(
        "K154-no-new-scalar-column-check-index-or-control-condition-enum",
        "provider_allocation_receipt" not in sql and "provider_allocation_receipt" not in shadow_sql
        and "stable_unknown" not in sql and "stable_unknown" not in shadow_sql,
    )

    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    checksum = hashlib.sha256(sql_path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    shadow_checksum = hashlib.sha256(shadow_sql_path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    check(
        "migration-manifest-checksum-matches-candidate-SQL",
        manifest["migrations"][0]["checksum_sha256"] == checksum,
    )
    check(
        "B1-additive-0002-migration-manifest-checksum-matches-candidate-SQL",
        len(manifest["migrations"]) == 3
        and manifest["migrations"][1]["migration_id"] == "0002-actor-generation-work-claim-shadow"
        and manifest["migrations"][1]["checksum_sha256"] == shadow_checksum,
    )
    check(
        "runtime-role-is-explicitly-barred-from-migrations",
        manifest.get("runtime_role_may_apply_migrations") is False,
    )

    migration_steps = [
        {"contains": "pg_advisory_xact_lock"},
        {"contains": "CREATE TABLE IF NOT EXISTS cerebro_schema_migrations"},
        {"contains": "SELECT schema_version, checksum_sha256", "rows": []},
        {"contains": "CREATE TABLE IF NOT EXISTS cerebro_project_instances"},
        {"contains": "INSERT INTO cerebro_schema_migrations", "rowcount": 1},
        {"contains": "SELECT schema_version, checksum_sha256", "rows": []},
        {"contains": "CREATE TABLE IF NOT EXISTS cerebro_actor_generation_shadow_heads"},
        {"contains": "INSERT INTO cerebro_schema_migrations", "rowcount": 1},
        {"contains": "SELECT schema_version, checksum_sha256", "rows": []},
        {"contains": "CREATE TABLE IF NOT EXISTS cerebro_human_t3_break_glass_heads"},
        {"contains": "INSERT INTO cerebro_schema_migrations", "rowcount": 1},
    ]
    migration_connection = ScriptedConnection(migration_steps)
    migration_result = apply_postgres_migrations(lambda: migration_connection)
    check(
        "migration-runner-applies-under-lock-and-commits",
        migration_result["applied"] == [
            "0001-control-context-state-service",
            "0002-actor-generation-work-claim-shadow",
            "0003-human-t3-break-glass",
        ]
        and migration_connection.commit_called
        and not migration_connection.cursor_instance.steps,
    )
    drift_steps = [
        {"contains": "pg_advisory_xact_lock"},
        {"contains": "CREATE TABLE IF NOT EXISTS cerebro_schema_migrations"},
        {
            "contains": "SELECT schema_version, checksum_sha256",
            "rows": [{"schema_version": "1.0.0-candidate", "checksum_sha256": "0" * 64}],
        },
    ]
    drift_connection = ScriptedConnection(drift_steps)
    check(
        "migration-runner-fails-closed-on-applied-drift",
        _expect_error(lambda: apply_postgres_migrations(lambda: drift_connection), MigrationDriftError)
        and drift_connection.rollback_called
        and not drift_connection.commit_called,
    )

    bootstrap_root = {
        "context_id": "CTX-BOOTSTRAP",
        "human_label": "Fortsett prosjektet",
        "objective_ref": "OBJ-BOOTSTRAP",
        "scope_ref": "SCOPE-BOOTSTRAP",
        "basis_refs": ["BASIS-BOOTSTRAP"],
        "project_basis_ref": "PB-BOOTSTRAP",
        "quality_trace_ref": "QT-BOOTSTRAP",
        "completion_criteria_refs": ["DONE"],
    }
    bootstrap_project, bootstrap_receipt = bootstrap_project_state(
        aggregate_id="AGG-BOOTSTRAP",
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        project_ref="BOOTSTRAP-PROJECT",
        source_revision="fixture",
        event_id="EVENT-BOOTSTRAP",
        decision_ref="MCPD-BOOTSTRAP",
        root=bootstrap_root,
    )
    bootstrap_subject = {
        "operation": "BOOTSTRAP_PROJECT",
        "tenant_ref": "TENANT-1",
        "workspace_ref": "WORKSPACE-1",
        "principal_ref": "PRINCIPAL-1",
        "project_ref": "BOOTSTRAP-PROJECT",
        "aggregate_id": "AGG-BOOTSTRAP",
        "source_revision": "fixture",
        "event_id": "EVENT-BOOTSTRAP",
        "decision_ref": "MCPD-BOOTSTRAP",
        "root": bootstrap_root,
        "make_default": True,
    }
    bootstrap_fingerprint = hashlib.sha256(
        json.dumps(bootstrap_subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    bootstrap_steps = [
        {"contains": "set_config('cerebro.tenant_ref'"},
        {"contains": "set_config('cerebro.workspace_ref'"},
        {"contains": "set_config('cerebro.principal_ref'"},
        {"contains": "SET CONSTRAINTS ALL DEFERRED"},
        {"contains": "FROM cerebro_project_bootstrap_receipts", "rows": []},
        {"contains": "INSERT INTO cerebro_project_instances", "rowcount": 1},
        {"contains": "INSERT INTO cerebro_control_contexts", "rowcount": 1},
        {"contains": "INSERT INTO cerebro_project_bootstrap_receipts", "rowcount": 1},
        {"contains": "FROM cerebro_principal_project_bindings", "rows": []},
        {"contains": "INSERT INTO cerebro_principal_project_bindings", "rowcount": 1},
    ]
    bootstrap_connection = ScriptedConnection(bootstrap_steps)
    bootstrap_args = {
        "tenant_ref": "TENANT-1",
        "workspace_ref": "WORKSPACE-1",
        "principal_ref": "PRINCIPAL-1",
        "project_ref": "BOOTSTRAP-PROJECT",
        "aggregate_id": "AGG-BOOTSTRAP",
        "source_revision": "fixture",
        "event_id": "EVENT-BOOTSTRAP",
        "decision_ref": "MCPD-BOOTSTRAP",
        "root": bootstrap_root,
        "scopes": {"project_state:transition"},
        "make_default": True,
    }
    bootstrap_result = PostgresControlContextStatePort(
        lambda: bootstrap_connection
    ).bootstrap_project(**bootstrap_args)
    check(
        "postgres-project-bootstrap-persists-project-ledger-and-default-atomically",
        bootstrap_result == {"project": bootstrap_project, "receipt": bootstrap_receipt}
        and bootstrap_connection.commit_called
        and not bootstrap_connection.cursor_instance.steps,
    )
    replay_steps = [
        {"contains": "set_config('cerebro.tenant_ref'"},
        {"contains": "set_config('cerebro.workspace_ref'"},
        {"contains": "set_config('cerebro.principal_ref'"},
        {"contains": "SET CONSTRAINTS ALL DEFERRED"},
        {
            "contains": "FROM cerebro_project_bootstrap_receipts",
            "rows": [{
                "request_fingerprint": bootstrap_fingerprint,
                "bootstrap_payload": {
                    "request": bootstrap_subject,
                    "project": bootstrap_project,
                    "receipt": bootstrap_receipt,
                },
                "receipt_fingerprint": bootstrap_receipt["receipt_fingerprint"],
            }],
        },
        {"contains": "FROM cerebro_project_instances", "rows": [_project_row(bootstrap_project)]},
        {"contains": "FROM cerebro_control_contexts", "rows": _context_rows(bootstrap_project)},
    ]
    replay_connection = ScriptedConnection(replay_steps)
    replay_result = PostgresControlContextStatePort(
        lambda: replay_connection
    ).bootstrap_project(**bootstrap_args)
    check(
        "postgres-project-bootstrap-exact-replay-returns-original-result",
        replay_result == bootstrap_result and replay_connection.commit_called,
    )
    conflicting_args = copy.deepcopy(bootstrap_args)
    conflicting_args["root"]["objective_ref"] = "DIFFERENT"
    conflict_connection = ScriptedConnection(replay_steps[:5])
    check(
        "postgres-project-bootstrap-different-replay-conflicts-before-write",
        _expect_error(
            lambda: PostgresControlContextStatePort(
                lambda: conflict_connection
            ).bootstrap_project(**conflicting_args),
            StateConflict,
        )
        and conflict_connection.rollback_called,
    )

    project, session, _, request, event = _fixture()
    connection = ScriptedConnection(_completion_steps(project, session, event))
    completion = PostgresControlContextStatePort(lambda: connection).complete_event(
        request,
        scopes={"project_state:transition"},
    )
    state_commit = completion["state_commit"]
    check(
        "completion-returns-only-after-scripted-database-commit",
        connection.commit_called and connection.closed and completion["result"] == "PASS",
    )
    check(
        "state-commit-receipt-binds-exact-transition-and-after-state",
        validate_state_commit_receipt(
            state_commit,
            directive=request["directive"],
            transition_receipt=completion["receipt"],
            project=completion["project"],
            session=completion["session"],
        )["result"] == "PASS",
    )
    check("state-commit-receipt-matches-governed-schema-shape", _schema_shape_valid(state_commit))
    executed = connection.cursor_instance.executed
    transition_index = next(i for i, query in enumerate(executed) if "insert into cerebro_transition_receipts" in query)
    commit_index = next(i for i, query in enumerate(executed) if "insert into cerebro_state_commit_receipts" in query)
    event_index = next(i for i, query in enumerate(executed) if "update cerebro_control_events" in query)
    check(
        "receipt-ledgers-precede-event-completion-in-one-transaction",
        transition_index < commit_index < event_index,
    )
    check(
        "focus-neutral-noop-does-not-rewrite-project-or-session-projections",
        not any("update cerebro_project_instances" in query for query in executed)
        and not any("update cerebro_control_session_bindings" in query for query in executed),
    )

    failing_connection = ScriptedConnection(
        _completion_steps(project, session, event),
        commit_error=ScriptedDatabaseError("08006"),
    )
    check(
        "failed-database-commit-rolls-back-and-cannot-return-completion",
        _expect_error(
            lambda: PostgresControlContextStatePort(lambda: failing_connection).complete_event(
                request,
                scopes={"project_state:transition"},
            ),
            StateServiceUnavailable,
        )
        and failing_connection.rollback_called,
    )
    unused_connection = ScriptedConnection([])
    check(
        "workspace-read-requires-verified-principal-binding",
        _expect_error(
            lambda: PostgresControlContextStatePort(lambda: unused_connection).read_project(
                tenant_ref="TENANT-1",
                workspace_ref="WORKSPACE-1",
                project_ref="TOTAL_MCP_REVISION",
                scopes={"project_state:read"},
            ),
            StateAuthorizationError,
        )
        and not unused_connection.commit_called,
    )

    tests.extend(human_t3_postgres_regressions())
    result = "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL"
    return {
        "schema": "cerebro-control-context-postgres-contract-selftest/v1",
        "result": result,
        "evidence_class": "SCRIPTED_DBAPI_CONTRACT_NOT_LIVE_POSTGRESQL",
        "live_postgresql_executed": False,
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
    }


def human_t3_postgres_regressions():
    sys.path.insert(0, str(SOURCE_ROOT / "mcp"))
    from control_resolution_host_validation import human_t3_fixture
    from human_t3_break_glass import seal_record
    from control_context_state_port import StateBindingError, StatePortError, validate_human_t3_custody_write
    tests = []
    def check(name, condition):
        tests.append({"name": "HG04-PG-" + name, "result": "PASS" if condition else "FAIL"})
    host, _, _, _, identity, candidate = human_t3_fixture()
    scopes = {"project_state:transition"}
    arm = host.arm(identity=identity, override_id="PG-OVERRIDE", candidate=candidate, scopes=scopes)["record"]
    key = tuple(identity[k] for k in ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref")) + ("PG-OVERRIDE",)
    def scoped():
        return [{"contains": "set_config('cerebro.tenant_ref'", "params": (identity["tenant_ref"],)},
                {"contains": "set_config('cerebro.workspace_ref'", "params": (identity["workspace_ref"],)},
                {"contains": "set_config('cerebro.principal_ref'", "params": (identity["principal_ref"],)},
                {"contains": "SET CONSTRAINTS ALL DEFERRED"}]
    def write_steps(record, prior=None, rowcount=1):
        return scoped() + [{"contains": "pg_advisory_xact_lock"},
                           {"contains": "FROM cerebro_human_t3_break_glass_heads", "params": key, "rows": [{"arm_payload": prior}] if prior else []},
                           {"contains": "UPDATE cerebro_human_t3_break_glass_heads" if prior else "INSERT INTO cerebro_human_t3_break_glass_heads", "rowcount": rowcount},
                           {"contains": "INSERT INTO cerebro_human_t3_break_glass_revisions", "rowcount": 1},
                           {"contains": "INSERT INTO cerebro_human_t3_break_glass_receipts", "rowcount": 1}]
    connection = ScriptedConnection(write_steps(arm))
    receipt = PostgresControlContextStatePort(lambda: connection).commit_human_t3_arm(record=arm, expected_revision=0, scopes=scopes)
    check("ARM-CAS-ledgers-commit-before-receipt", receipt == validate_human_t3_custody_write(arm, None, 0)
          and connection.commit_called and not connection.rollback_called and not connection.cursor_instance.steps)
    read_connection = ScriptedConnection(scoped() + [{"contains": "SELECT arm_payload", "params": key, "rows": [{"arm_payload": arm}]}])
    observed = PostgresControlContextStatePort(lambda: read_connection).read_human_t3_arm(**identity, override_id="PG-OVERRIDE", scopes=scopes)
    check("independent-durable-readback-exact-scope", observed == arm and read_connection.commit_called and read_connection.closed)
    consumed = copy.deepcopy(arm)
    consumed.update(revision=2, state="OVERRIDE_CONSUMED", override_consumed=True, confirm_event_id="CONFIRM-EVENT", effect_attempt_id="T3-ATTEMPT", effect_truth="EFFECT_UNKNOWN")
    consumed = seal_record(consumed)
    connection = ScriptedConnection(write_steps(consumed, arm))
    receipt = PostgresControlContextStatePort(lambda: connection).commit_human_t3_arm(record=consumed, expected_revision=1, scopes=scopes)
    check("one-consume-CAS-and-immutable-receipt", receipt["previous_revision"] == 1 and receipt["revision"] == 2 and connection.commit_called and not connection.cursor_instance.steps)
    connection = ScriptedConnection(write_steps(consumed, arm, rowcount=0)[:-2])
    check("lost-CAS-no-ledger-or-receipt", _expect_error(lambda: PostgresControlContextStatePort(lambda: connection).commit_human_t3_arm(record=consumed, expected_revision=1, scopes=scopes), StateConflict)
          and connection.rollback_called and not connection.commit_called and not connection.cursor_instance.steps)
    connection = ScriptedConnection(scoped() + [{"contains": "pg_advisory_xact_lock"}, {"contains": "FROM cerebro_human_t3_break_glass_heads", "rows": [{"arm_payload": consumed}]}])
    check("duplicate-consume-conflicts-before-update", _expect_error(lambda: PostgresControlContextStatePort(lambda: connection).commit_human_t3_arm(record=consumed, expected_revision=1, scopes=scopes), StateConflict)
          and connection.rollback_called and not connection.commit_called)
    connection = ScriptedConnection(write_steps(arm), commit_error=RuntimeError("fixture-commit-uncertain"))
    check("failed-commit-no-receipt", _expect_error(lambda: PostgresControlContextStatePort(lambda: connection).commit_human_t3_arm(record=arm, expected_revision=0, scopes=scopes), StatePortError)
          and connection.rollback_called)
    for label, changed in (("fingerprint", dict(arm, fingerprint="0"*64)), ("principal", seal_record({**arm, "identity": {**identity, "principal_ref": "OTHER"}}))):
        connection = ScriptedConnection(scoped() + [{"contains": "SELECT arm_payload", "rows": [{"arm_payload": changed}]}])
        check(label + "-readback-rejected", _expect_error(lambda: PostgresControlContextStatePort(lambda: connection).read_human_t3_arm(**identity, override_id="PG-OVERRIDE", scopes=scopes), StateBindingError)
              and connection.rollback_called and not connection.commit_called)
    connection = ScriptedConnection([])
    check("read-and-write-transition-scope-before-DB", _expect_error(lambda: PostgresControlContextStatePort(lambda: connection).read_human_t3_arm(**identity, override_id="PG-OVERRIDE", scopes={"project_state:read"}), StateAuthorizationError)
          and _expect_error(lambda: PostgresControlContextStatePort(lambda: connection).commit_human_t3_arm(record=arm, expected_revision=0, scopes={"project_state:read"}), StateAuthorizationError) and not connection.commit_called)
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    entry = manifest["migrations"][-1]
    sql_bytes = (CONTEXT_ROOT / entry["path"]).read_bytes().replace(b"\r\n", b"\n")
    sql = sql_bytes.decode("utf-8")
    check("0003-additive-canonical-checksum", entry["migration_id"] == "0003-human-t3-break-glass" and entry["checksum_sha256"] == hashlib.sha256(sql_bytes).hexdigest())
    check("0003-RLS-no-delete-immutable-revisions-receipts", all(marker in sql for marker in ("ENABLE ROW LEVEL SECURITY", "FORCE ROW LEVEL SECURITY", "BEFORE DELETE", "BEFORE UPDATE OR DELETE", "cerebro_reject_immutable_ledger_mutation", "cerebro.principal_ref", "NONAUTHORIZING_CUSTODY", "CUSTODY_ONLY"))
          and all("CREATE TABLE IF NOT EXISTS cerebro_human_t3_break_glass_" + suffix in sql for suffix in ("heads", "revisions", "receipts")) and "DROP TABLE" not in sql)
    return tests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["selftest"])
    parser.parse_args()
    result = selftest()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
