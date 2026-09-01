#!/usr/bin/env python3
"""PostgreSQL state-port adapter for hierarchical project control.

The adapter owns persistence mechanics only.  Project-control semantics remain
in ``control_context_registry`` and authorization identity must already have
been verified by the MCP boundary.  A returned completion means that the DB-API
``commit()`` call succeeded; no completion is returned from a failed commit.
"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:
    from .control_context_registry import (
        DIRECTIVE_SCHEMA,
        ControlContextError,
        apply_transition,
        bind_control_session,
        bootstrap_project_state,
        validate_actor_generation_shadow,
        validate_work_claim_shadow,
        validate_project_state,
        validate_session_state,
        validate_transition_receipt,
    )
    from .control_context_state_port import (
        BEGIN_RESULT_SCHEMA,
        BEGIN_SCHEMA,
        COMPLETE_RESULT_SCHEMA,
        StateAuthorizationError,
        StateBindingError,
        StateConflict,
        StatePortError,
        StateServiceUnavailable,
        rehydrate_control_session,
        validate_context_owner_candidate_binding,
    )
except ImportError:
    from control_context_registry import (
        DIRECTIVE_SCHEMA,
        ControlContextError,
        apply_transition,
        bind_control_session,
        bootstrap_project_state,
        validate_actor_generation_shadow,
        validate_work_claim_shadow,
        validate_project_state,
        validate_session_state,
        validate_transition_receipt,
    )
    from control_context_state_port import (
        BEGIN_RESULT_SCHEMA,
        BEGIN_SCHEMA,
        COMPLETE_RESULT_SCHEMA,
        StateAuthorizationError,
        StateBindingError,
        StateConflict,
        StatePortError,
        StateServiceUnavailable,
        rehydrate_control_session,
        validate_context_owner_candidate_binding,
    )


STATE_COMMIT_SCHEMA = "cerebro-state-service-commit-receipt/v1"
MIGRATION_MANIFEST_SCHEMA = "cerebro-control-context-postgres-migrations/v1"
DEFAULT_MANIFEST = Path(__file__).with_name("control_context_postgres_migrations.json")


class MigrationDriftError(StatePortError):
    """An applied migration no longer matches its governed checksum."""


def _require(condition: bool, message: str, error: type[StatePortError] = StatePortError) -> None:
    if not condition:
        raise error(message)


def _canonical_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_text(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    # Governed migration checksums bind canonical Git text, independent of a
    # Windows checkout's CRLF presentation.
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _json_value(value: Any, *, field: str) -> Any:
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise StatePortError(f"persisted-{field}-json-invalid") from exc
    raise StatePortError(f"persisted-{field}-json-type-invalid")


def _row_mapping(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = getattr(cursor, "description", None)
    if not description:
        raise StatePortError("database-row-description-required")
    names: list[str] = []
    for item in description:
        name = getattr(item, "name", None)
        names.append(name if isinstance(name, str) else item[0])
    if len(names) != len(row):
        raise StatePortError("database-row-width-mismatch")
    return dict(zip(names, row))


def _fetchone(cursor: Any) -> dict[str, Any] | None:
    return _row_mapping(cursor, cursor.fetchone())


def _fetchall(cursor: Any) -> list[dict[str, Any]]:
    return [_row_mapping(cursor, row) or {} for row in cursor.fetchall()]


def _sqlstate(exc: BaseException) -> str | None:
    value = getattr(exc, "sqlstate", None)
    if isinstance(value, str):
        return value
    diagnostic = getattr(exc, "diag", None)
    value = getattr(diagnostic, "sqlstate", None)
    return value if isinstance(value, str) else None


def _mapped_database_error(exc: BaseException) -> StatePortError:
    code = _sqlstate(exc)
    if code is not None and code.startswith("08"):
        return StateServiceUnavailable("control-context-state-service-unavailable")
    if code in {"40001", "40P01", "23503", "23505", "23514", "23P01"}:
        return StateConflict("state-service-transaction-conflict")
    if code in {"42501", "28000", "28P01"}:
        return StateAuthorizationError("state-service-database-authorization-failed")
    return StatePortError("state-service-database-operation-failed")


def _safe_rollback(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:
        pass


def _safe_close(value: Any) -> None:
    try:
        value.close()
    except Exception:
        pass


def make_psycopg_connection_factory(
    dsn: str,
    *,
    connect_timeout_seconds: int = 10,
    application_name: str = "cerebro-control-context-state-service",
) -> Callable[[], Any]:
    """Build a production connection factory without storing connection data."""

    _require(isinstance(dsn, str) and bool(dsn.strip()), "postgres-dsn-required")
    _require(
        isinstance(connect_timeout_seconds, int) and connect_timeout_seconds >= 1,
        "postgres-connect-timeout-invalid",
    )

    def connect() -> Any:
        try:
            import psycopg  # type: ignore
            from psycopg.rows import dict_row  # type: ignore
        except ImportError as exc:
            raise StateServiceUnavailable("psycopg-runtime-dependency-unavailable") from exc
        try:
            return psycopg.connect(
                dsn.strip(),
                connect_timeout=connect_timeout_seconds,
                application_name=application_name,
                row_factory=dict_row,
            )
        except Exception as exc:
            raise _mapped_database_error(exc) from exc

    return connect


def build_state_commit_receipt(
    *,
    tenant_ref: str,
    workspace_ref: str,
    principal_ref: str,
    consumer_ref: str,
    session_ref: str,
    project_ref: str,
    event_id: str,
    directive: dict[str, Any],
    owner_effect_candidate: dict[str, Any] | None,
    transition_receipt: dict[str, Any],
    project: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    """Seal the exact transition and after-state accepted by one transaction."""

    if owner_effect_candidate is not None:
        _require(owner_effect_candidate.get("schema") == "cerebro-owner-effect-receipt/v1", "state-commit-owner-candidate-schema-mismatch")
        _require(owner_effect_candidate.get("result") == "CANDIDATE" and owner_effect_candidate.get("current") is False, "state-commit-owner-candidate-must-be-precommit")
        _require(owner_effect_candidate.get("control_decision_ref") == directive.get("decision_ref"), "state-commit-owner-candidate-decision-mismatch")
        owner_candidate_ref = owner_effect_candidate.get("receipt_ref")
        owner_candidate_fingerprint = owner_effect_candidate.get("receipt_fingerprint")
        _require(isinstance(owner_candidate_ref, str) and owner_candidate_ref.startswith("OER-"), "state-commit-owner-candidate-ref-invalid")
        _require(isinstance(owner_candidate_fingerprint, str) and len(owner_candidate_fingerprint) == 64, "state-commit-owner-candidate-fingerprint-invalid")
    else:
        owner_candidate_ref = None
        owner_candidate_fingerprint = None
    receipt: dict[str, Any] = {
        "schema": STATE_COMMIT_SCHEMA,
        "message_kind": "STATE_SERVICE_COMMIT_RECEIPT",
        "backend": "POSTGRESQL",
        "commit_protocol": "RETURN_ONLY_AFTER_DATABASE_COMMIT",
        "durability": "DATABASE_COMMIT_ACKNOWLEDGED_ON_RETURN",
        "tenant_ref": tenant_ref,
        "workspace_ref": workspace_ref,
        "principal_ref": principal_ref,
        "consumer_ref": consumer_ref,
        "session_ref": session_ref,
        "project_ref": project_ref,
        "event_id": event_id,
        "transition_receipt_ref": transition_receipt["receipt_id"],
        "transition_receipt_fingerprint": transition_receipt["receipt_fingerprint"],
        "transition_directive_fingerprint": _sha256(directive),
        "owner_effect_candidate_ref": owner_candidate_ref,
        "owner_effect_candidate_fingerprint": owner_candidate_fingerprint,
        "project_revision_after": project["revision"],
        "session_revision_after": session["session_revision"],
        "project_fingerprint_after": project["fingerprint"],
        "session_fingerprint_after": session["fingerprint"],
        "commit_fingerprint": "",
        "commit_ref": "",
    }
    subject = copy.deepcopy(receipt)
    subject.pop("commit_fingerprint")
    subject.pop("commit_ref")
    receipt["commit_fingerprint"] = _sha256(subject)
    receipt["commit_ref"] = "SSC-" + receipt["commit_fingerprint"][:24].upper()
    validate_state_commit_receipt(
        receipt,
        directive=directive,
        owner_effect_candidate=owner_effect_candidate,
        transition_receipt=transition_receipt,
        project=project,
        session=session,
    )
    return receipt


def validate_state_commit_receipt(
    receipt: dict[str, Any],
    *,
    directive: dict[str, Any] | None = None,
    owner_effect_candidate: dict[str, Any] | None = None,
    transition_receipt: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = {
        "schema", "message_kind", "backend", "commit_protocol", "durability",
        "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref",
        "project_ref", "event_id", "transition_receipt_ref",
        "transition_receipt_fingerprint", "transition_directive_fingerprint",
        "owner_effect_candidate_ref", "owner_effect_candidate_fingerprint",
        "project_revision_after", "session_revision_after",
        "project_fingerprint_after", "session_fingerprint_after", "commit_fingerprint", "commit_ref",
    }
    _require(isinstance(receipt, dict) and not required.difference(receipt), "state-commit-receipt-fields-missing")
    _require(receipt.get("schema") == STATE_COMMIT_SCHEMA, "state-commit-receipt-schema-mismatch")
    _require(receipt.get("message_kind") == "STATE_SERVICE_COMMIT_RECEIPT", "state-commit-message-kind-mismatch")
    _require(receipt.get("backend") == "POSTGRESQL", "state-commit-backend-mismatch")
    _require(receipt.get("commit_protocol") == "RETURN_ONLY_AFTER_DATABASE_COMMIT", "state-commit-protocol-mismatch")
    _require(receipt.get("durability") == "DATABASE_COMMIT_ACKNOWLEDGED_ON_RETURN", "state-commit-durability-mismatch")
    for field in (
        "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref",
        "project_ref", "event_id", "transition_receipt_ref",
    ):
        _require(isinstance(receipt.get(field), str) and bool(receipt[field].strip()), f"state-commit-{field}-required")
    for field in (
        "transition_receipt_fingerprint", "transition_directive_fingerprint", "project_fingerprint_after",
        "session_fingerprint_after", "commit_fingerprint",
    ):
        value = receipt.get(field)
        _require(isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value), f"state-commit-{field}-invalid")
    for field in ("project_revision_after", "session_revision_after"):
        _require(isinstance(receipt.get(field), int) and receipt[field] >= 1, f"state-commit-{field}-invalid")
    owner_candidate_ref = receipt.get("owner_effect_candidate_ref")
    owner_candidate_fingerprint = receipt.get("owner_effect_candidate_fingerprint")
    _require(
        (owner_candidate_ref is None and owner_candidate_fingerprint is None)
        or (
            isinstance(owner_candidate_ref, str)
            and owner_candidate_ref.startswith("OER-")
            and isinstance(owner_candidate_fingerprint, str)
            and len(owner_candidate_fingerprint) == 64
            and all(c in "0123456789abcdef" for c in owner_candidate_fingerprint)
        ),
        "state-commit-owner-effect-candidate-binding-invalid",
    )
    subject = copy.deepcopy(receipt)
    subject.pop("commit_fingerprint", None)
    subject.pop("commit_ref", None)
    expected_fingerprint = _sha256(subject)
    _require(receipt.get("commit_fingerprint") == expected_fingerprint, "state-commit-fingerprint-mismatch")
    _require(receipt.get("commit_ref") == "SSC-" + expected_fingerprint[:24].upper(), "state-commit-ref-mismatch")
    if transition_receipt is not None:
        validate_transition_receipt(transition_receipt)
        _require(receipt["event_id"] == transition_receipt["event_id"], "state-commit-event-mismatch")
        _require(receipt["transition_receipt_ref"] == transition_receipt["receipt_id"], "state-commit-transition-ref-mismatch")
        _require(receipt["transition_receipt_fingerprint"] == transition_receipt["receipt_fingerprint"], "state-commit-transition-fingerprint-mismatch")
    if directive is not None:
        _require(receipt["event_id"] == directive.get("event_id"), "state-commit-directive-event-mismatch")
        _require(receipt["transition_directive_fingerprint"] == _sha256(directive), "state-commit-directive-fingerprint-mismatch")
    if owner_effect_candidate is not None:
        _require(receipt["owner_effect_candidate_ref"] == owner_effect_candidate.get("receipt_ref"), "state-commit-owner-candidate-ref-mismatch")
        _require(receipt["owner_effect_candidate_fingerprint"] == owner_effect_candidate.get("receipt_fingerprint"), "state-commit-owner-candidate-fingerprint-mismatch")
    if project is not None:
        validate_project_state(project)
        _require(receipt["project_ref"] == project["project_ref"], "state-commit-project-ref-mismatch")
        _require(receipt["project_revision_after"] == project["revision"], "state-commit-project-revision-mismatch")
        _require(receipt["project_fingerprint_after"] == project["fingerprint"], "state-commit-project-fingerprint-mismatch")
    if session is not None:
        if project is not None:
            validate_session_state(session, project)
        _require(receipt["session_ref"] == session["session_ref"], "state-commit-session-ref-mismatch")
        _require(receipt["session_revision_after"] == session["session_revision"], "state-commit-session-revision-mismatch")
        _require(receipt["session_fingerprint_after"] == session["fingerprint"], "state-commit-session-fingerprint-mismatch")
    return {
        "result": "PASS",
        "commit_ref": receipt["commit_ref"],
        "event_id": receipt["event_id"],
        "project_revision_after": receipt["project_revision_after"],
        "session_revision_after": receipt["session_revision_after"],
    }


def apply_postgres_migrations(
    connection_factory: Callable[[], Any],
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Apply checksum-locked migrations with a migration-role connection.

    The supplied connection must use a deployment role distinct from the
    runtime service role.  Migration drift fails closed.
    """

    path = Path(manifest_path).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatePortError("postgres-migration-manifest-unreadable") from exc
    _require(manifest.get("schema") == MIGRATION_MANIFEST_SCHEMA, "postgres-migration-manifest-schema-mismatch")
    migrations = manifest.get("migrations")
    _require(isinstance(migrations, list) and bool(migrations), "postgres-migrations-required")
    connection = None
    cursor = None
    applied: list[str] = []
    already_applied: list[str] = []
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        cursor.execute("SELECT pg_advisory_xact_lock(48329716411208371)")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cerebro_schema_migrations (
                migration_id text PRIMARY KEY,
                schema_version text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now(),
                checksum_sha256 text NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
            )
            """
        )
        seen: set[str] = set()
        for entry in migrations:
            _require(isinstance(entry, dict), "postgres-migration-entry-invalid")
            migration_id = entry.get("migration_id")
            schema_version = entry.get("schema_version")
            relative_path = entry.get("path")
            checksum = entry.get("checksum_sha256")
            _require(isinstance(migration_id, str) and migration_id and migration_id not in seen, "postgres-migration-id-invalid")
            _require(isinstance(schema_version, str) and schema_version, "postgres-migration-schema-version-invalid")
            _require(isinstance(relative_path, str) and relative_path, "postgres-migration-path-invalid")
            _require(isinstance(checksum, str) and len(checksum) == 64, "postgres-migration-checksum-invalid")
            seen.add(migration_id)
            migration_path = (path.parent / relative_path).resolve()
            _require(migration_path.parent == path.parent, "postgres-migration-path-escape-prohibited")
            try:
                actual_checksum = _file_sha256(migration_path)
                sql = migration_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise StatePortError("postgres-migration-file-unreadable") from exc
            if actual_checksum != checksum:
                raise MigrationDriftError(f"postgres-migration-source-drift:{migration_id}")
            cursor.execute(
                "SELECT schema_version, checksum_sha256 FROM cerebro_schema_migrations WHERE migration_id = %s",
                (migration_id,),
            )
            existing = _fetchone(cursor)
            if existing is not None:
                if existing.get("schema_version") != schema_version or existing.get("checksum_sha256") != checksum:
                    raise MigrationDriftError(f"postgres-migration-applied-drift:{migration_id}")
                already_applied.append(migration_id)
                continue
            cursor.execute(sql)
            cursor.execute(
                "INSERT INTO cerebro_schema_migrations (migration_id, schema_version, checksum_sha256) VALUES (%s, %s, %s)",
                (migration_id, schema_version, checksum),
            )
            applied.append(migration_id)
        connection.commit()
    except StatePortError:
        if connection is not None:
            _safe_rollback(connection)
        raise
    except Exception as exc:
        if connection is not None:
            _safe_rollback(connection)
        raise _mapped_database_error(exc) from exc
    finally:
        if cursor is not None:
            _safe_close(cursor)
        if connection is not None:
            _safe_close(connection)
    return {
        "schema": "cerebro-control-context-postgres-migration-result/v1",
        "result": "PASS",
        "applied": applied,
        "already_applied": already_applied,
    }


class PostgresControlContextStatePort:
    """DB-API PostgreSQL implementation of the control-context state port."""

    def __init__(self, connection_factory: Callable[[], Any]):
        _require(callable(connection_factory), "postgres-connection-factory-required")
        self._connection_factory = connection_factory

    @staticmethod
    def _require_scope(scopes: set[str], required: str) -> None:
        _require(required in scopes, f"required-scope-missing:{required}", StateAuthorizationError)

    @contextmanager
    def _transaction(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
    ) -> Iterator[Any]:
        connection = None
        cursor = None
        try:
            try:
                connection = self._connection_factory()
            except StatePortError:
                raise
            except Exception as exc:
                raise StateServiceUnavailable("control-context-state-service-unavailable") from exc
            cursor = connection.cursor()
            cursor.execute("SELECT set_config('cerebro.tenant_ref', %s, true)", (tenant_ref,))
            cursor.execute("SELECT set_config('cerebro.workspace_ref', %s, true)", (workspace_ref,))
            cursor.execute("SELECT set_config('cerebro.principal_ref', %s, true)", (principal_ref,))
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            yield cursor
            connection.commit()
        except StatePortError:
            if connection is not None:
                _safe_rollback(connection)
            raise
        except Exception as exc:
            if connection is not None:
                _safe_rollback(connection)
            raise _mapped_database_error(exc) from exc
        finally:
            if cursor is not None:
                _safe_close(cursor)
            if connection is not None:
                _safe_close(connection)

    @staticmethod
    def _validate_text_fields(value: dict[str, Any], prefix: str, fields: tuple[str, ...]) -> None:
        for field in fields:
            _require(
                isinstance(value.get(field), str) and bool(value[field].strip()),
                f"{prefix}-{field}-required",
            )

    @staticmethod
    def _project_row(project: dict[str, Any]) -> tuple[Any, ...]:
        return (
            project["tenant_ref"],
            project["workspace_ref"],
            project["project_ref"],
            project["aggregate_id"],
            project["source_revision"],
            project["project_status"],
            project["default_context_ref"],
            project["revision"],
            project["fingerprint"],
            project["next_sequence"],
        )

    def _load_project(
        self,
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        project_ref: str,
        for_update: bool = False,
        required: bool = True,
    ) -> dict[str, Any] | None:
        lock = " FOR UPDATE" if for_update else ""
        cursor.execute(
            """
            SELECT aggregate_id, source_revision, project_status, default_context_ref,
                   aggregate_revision, aggregate_fingerprint, next_sequence
              FROM cerebro_project_instances
             WHERE tenant_ref = %s AND workspace_ref = %s AND project_ref = %s
            """ + lock,
            (tenant_ref, workspace_ref, project_ref),
        )
        row = _fetchone(cursor)
        if row is None:
            if required:
                raise StateBindingError("project-instance-not-found")
            return None
        cursor.execute(
            """
            SELECT context_id, parent_context_ref, derived_from_context_ref, lifecycle,
                   control_condition, disposition, sequence, context_payload, context_fingerprint
              FROM cerebro_control_contexts
             WHERE tenant_ref = %s AND workspace_ref = %s AND project_ref = %s
             ORDER BY sequence, context_id
            """,
            (tenant_ref, workspace_ref, project_ref),
        )
        contexts: list[dict[str, Any]] = []
        for context_row in _fetchall(cursor):
            payload = _json_value(context_row.get("context_payload"), field="context-payload")
            if not isinstance(payload, dict):
                raise StatePortError("persisted-context-payload-object-required")
            for payload_field, scalar_field in (
                ("context_id", "context_id"),
                ("parent_context_ref", "parent_context_ref"),
                ("derived_from_context_ref", "derived_from_context_ref"),
                ("lifecycle", "lifecycle"),
                ("control_condition", "control_condition"),
                ("disposition", "disposition"),
                ("sequence", "sequence"),
                ("context_fingerprint", "context_fingerprint"),
            ):
                if payload.get(payload_field) != context_row.get(scalar_field):
                    raise StatePortError(f"persisted-context-{payload_field}-projection-mismatch")
            contexts.append(payload)
        project = {
            "schema": "cerebro-control-context-project-state/v1",
            "aggregate_id": row["aggregate_id"],
            "tenant_ref": tenant_ref,
            "workspace_ref": workspace_ref,
            "project_ref": project_ref,
            "source_revision": row["source_revision"],
            "project_status": row["project_status"],
            "revision": row["aggregate_revision"],
            "default_context_ref": row["default_context_ref"],
            "contexts": contexts,
            "next_sequence": row["next_sequence"],
            "fingerprint": row["aggregate_fingerprint"],
        }
        try:
            validate_project_state(project)
        except ControlContextError as exc:
            raise StatePortError("persisted-project-state-invalid") from exc
        return project

    def _load_session(
        self,
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        project: dict[str, Any] | None = None,
        for_update: bool = False,
        required: bool = True,
    ) -> dict[str, Any] | None:
        lock = " FOR UPDATE" if for_update else ""
        cursor.execute(
            """
            SELECT session_binding_id, project_ref, active_context_ref, project_revision,
                   session_revision, session_fingerprint
              FROM cerebro_control_session_bindings
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
               AND consumer_ref = %s AND session_ref = %s
            """ + lock,
            (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref),
        )
        row = _fetchone(cursor)
        if row is None:
            if required:
                raise StateBindingError("control-session-not-bound")
            return None
        cursor.execute(
            """
            SELECT binding_id, binding_revision, context_ref, basis_project_revision,
                   basis_session_revision, binding_payload, binding_fingerprint
              FROM cerebro_continuation_bindings
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
               AND consumer_ref = %s AND session_ref = %s AND active = true
            """,
            (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref),
        )
        binding_rows = _fetchall(cursor)
        if len(binding_rows) > 1:
            raise StatePortError("persisted-multiple-active-continuation-bindings")
        binding: dict[str, Any] | None = None
        if binding_rows:
            binding_row = binding_rows[0]
            value = _json_value(binding_row.get("binding_payload"), field="binding-payload")
            if not isinstance(value, dict):
                raise StatePortError("persisted-binding-payload-object-required")
            for payload_field, scalar_field in (
                ("binding_id", "binding_id"),
                ("binding_revision", "binding_revision"),
                ("context_ref", "context_ref"),
                ("basis_project_revision", "basis_project_revision"),
                ("basis_session_revision", "basis_session_revision"),
                ("binding_fingerprint", "binding_fingerprint"),
            ):
                if value.get(payload_field) != binding_row.get(scalar_field):
                    raise StatePortError(f"persisted-binding-{payload_field}-projection-mismatch")
            binding = value
        session = {
            "schema": "cerebro-control-session-state/v1",
            "session_binding_id": row["session_binding_id"],
            "tenant_ref": tenant_ref,
            "workspace_ref": workspace_ref,
            "principal_ref": principal_ref,
            "consumer_ref": consumer_ref,
            "session_ref": session_ref,
            "project_ref": row["project_ref"],
            "project_revision": row["project_revision"],
            "session_revision": row["session_revision"],
            "active_context_ref": row["active_context_ref"],
            "active_continuation_binding": binding,
            "fingerprint": row["session_fingerprint"],
        }
        if project is not None:
            try:
                validate_session_state(session, project)
            except ControlContextError as exc:
                raise StatePortError("persisted-control-session-state-invalid") from exc
        return session

    @staticmethod
    def _insert_contexts(cursor: Any, project: dict[str, Any]) -> None:
        for context in project["contexts"]:
            cursor.execute(
                """
                INSERT INTO cerebro_control_contexts (
                    tenant_ref, workspace_ref, project_ref, context_id,
                    parent_context_ref, derived_from_context_ref, lifecycle,
                    control_condition, disposition, sequence, context_payload,
                    context_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (tenant_ref, workspace_ref, project_ref, context_id)
                DO UPDATE SET
                    parent_context_ref = EXCLUDED.parent_context_ref,
                    derived_from_context_ref = EXCLUDED.derived_from_context_ref,
                    lifecycle = EXCLUDED.lifecycle,
                    control_condition = EXCLUDED.control_condition,
                    disposition = EXCLUDED.disposition,
                    sequence = EXCLUDED.sequence,
                    context_payload = EXCLUDED.context_payload,
                    context_fingerprint = EXCLUDED.context_fingerprint,
                    updated_at = now()
                """,
                (
                    project["tenant_ref"], project["workspace_ref"], project["project_ref"],
                    context["context_id"], context["parent_context_ref"],
                    context["derived_from_context_ref"], context["lifecycle"],
                    context["control_condition"], context["disposition"], context["sequence"],
                    _canonical_text(context), context["context_fingerprint"],
                ),
            )

    def _insert_project(self, cursor: Any, project: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO cerebro_project_instances (
                tenant_ref, workspace_ref, project_ref, aggregate_id, source_revision,
                project_status, default_context_ref, aggregate_revision,
                aggregate_fingerprint, next_sequence
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            self._project_row(project),
        )
        self._insert_contexts(cursor, project)

    def _write_project_cas(
        self,
        cursor: Any,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        if before == after:
            return
        cursor.execute(
            """
            UPDATE cerebro_project_instances
               SET aggregate_id = %s, source_revision = %s, project_status = %s,
                   default_context_ref = %s, aggregate_revision = %s,
                   aggregate_fingerprint = %s, next_sequence = %s, updated_at = now()
             WHERE tenant_ref = %s AND workspace_ref = %s AND project_ref = %s
               AND aggregate_revision = %s AND aggregate_fingerprint = %s
            """,
            (
                after["aggregate_id"], after["source_revision"], after["project_status"],
                after["default_context_ref"], after["revision"], after["fingerprint"],
                after["next_sequence"], before["tenant_ref"], before["workspace_ref"],
                before["project_ref"], before["revision"], before["fingerprint"],
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflict("project-state-conflict-reload-and-reresolve")
        self._insert_contexts(cursor, after)

    @staticmethod
    def _insert_session(cursor: Any, session: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO cerebro_control_session_bindings (
                tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                session_binding_id, project_ref, active_context_ref, project_revision,
                session_revision, session_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref)
            DO NOTHING
            """,
            (
                session["tenant_ref"], session["workspace_ref"], session["principal_ref"],
                session["consumer_ref"], session["session_ref"], session["session_binding_id"],
                session["project_ref"], session["active_context_ref"], session["project_revision"],
                session["session_revision"], session["fingerprint"],
            ),
        )

    @staticmethod
    def _sync_continuation_binding(cursor: Any, session: dict[str, Any]) -> None:
        cursor.execute(
            """
            UPDATE cerebro_continuation_bindings
               SET active = false, superseded_at = now()
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
               AND consumer_ref = %s AND session_ref = %s AND active = true
            """,
            (
                session["tenant_ref"], session["workspace_ref"], session["principal_ref"],
                session["consumer_ref"], session["session_ref"],
            ),
        )
        binding = session.get("active_continuation_binding")
        if binding is None:
            return
        cursor.execute(
            """
            INSERT INTO cerebro_continuation_bindings (
                tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                project_ref, binding_id, binding_revision, context_ref,
                basis_project_revision, basis_session_revision, active,
                binding_payload, binding_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s::jsonb, %s)
            """,
            (
                session["tenant_ref"], session["workspace_ref"], session["principal_ref"],
                session["consumer_ref"], session["session_ref"], session["project_ref"],
                binding["binding_id"], binding["binding_revision"], binding["context_ref"],
                binding["basis_project_revision"], binding["basis_session_revision"],
                _canonical_text(binding), binding["binding_fingerprint"],
            ),
        )

    def _write_session_cas(
        self,
        cursor: Any,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        if before == after:
            return
        cursor.execute(
            """
            UPDATE cerebro_control_session_bindings
               SET project_ref = %s, active_context_ref = %s, project_revision = %s,
                   session_revision = %s, session_fingerprint = %s, updated_at = now()
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
               AND consumer_ref = %s AND session_ref = %s
               AND session_revision = %s AND session_fingerprint = %s
            """,
            (
                after["project_ref"], after["active_context_ref"], after["project_revision"],
                after["session_revision"], after["fingerprint"], before["tenant_ref"],
                before["workspace_ref"], before["principal_ref"], before["consumer_ref"],
                before["session_ref"], before["session_revision"], before["fingerprint"],
            ),
        )
        if cursor.rowcount != 1:
            raise StateConflict("session-state-conflict-reload-and-reresolve")
        self._sync_continuation_binding(cursor, after)

    @staticmethod
    def _binding_fingerprint(
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        project_ref: str,
        revision: int,
    ) -> str:
        return _sha256(
            {
                "schema": "cerebro-principal-project-binding/v1",
                "tenant_ref": tenant_ref,
                "workspace_ref": workspace_ref,
                "principal_ref": principal_ref,
                "active_project_ref": project_ref,
                "binding_revision": revision,
            }
        )

    def _set_default_project(
        self,
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        project_ref: str,
    ) -> None:
        cursor.execute(
            """
            SELECT active_project_ref, binding_revision
              FROM cerebro_principal_project_bindings
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
             FOR UPDATE
            """,
            (tenant_ref, workspace_ref, principal_ref),
        )
        existing = _fetchone(cursor)
        if existing is not None and existing.get("active_project_ref") == project_ref:
            return
        revision = 1 if existing is None else int(existing["binding_revision"]) + 1
        fingerprint = self._binding_fingerprint(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
            project_ref=project_ref,
            revision=revision,
        )
        if existing is None:
            cursor.execute(
                """
                INSERT INTO cerebro_principal_project_bindings (
                    tenant_ref, workspace_ref, principal_ref, active_project_ref,
                    binding_revision, binding_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (tenant_ref, workspace_ref, principal_ref, project_ref, revision, fingerprint),
            )
        else:
            cursor.execute(
                """
                UPDATE cerebro_principal_project_bindings
                   SET active_project_ref = %s, binding_revision = %s,
                       binding_fingerprint = %s, updated_at = now()
                 WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
                   AND binding_revision = %s
                """,
                (
                    project_ref, revision, fingerprint, tenant_ref, workspace_ref,
                    principal_ref, existing["binding_revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflict("default-project-binding-conflict")

    def bootstrap_project(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        project_ref: str,
        aggregate_id: str,
        source_revision: str,
        event_id: str,
        decision_ref: str,
        root: dict[str, Any],
        scopes: set[str],
        make_default: bool = True,
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:transition")
        project, receipt = bootstrap_project_state(
            aggregate_id=aggregate_id,
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            project_ref=project_ref,
            source_revision=source_revision,
            event_id=event_id,
            decision_ref=decision_ref,
            root=root,
        )
        validate_transition_receipt(receipt)
        request_subject = {
            "operation": "BOOTSTRAP_PROJECT",
            "tenant_ref": tenant_ref,
            "workspace_ref": workspace_ref,
            "principal_ref": principal_ref,
            "project_ref": project_ref,
            "aggregate_id": aggregate_id,
            "source_revision": source_revision,
            "event_id": event_id,
            "decision_ref": decision_ref,
            "root": copy.deepcopy(root),
            "make_default": make_default,
        }
        request_fingerprint = _sha256(request_subject)
        result: dict[str, Any] | None = None
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            cursor.execute(
                """
                SELECT request_fingerprint, bootstrap_payload, receipt_fingerprint
                  FROM cerebro_project_bootstrap_receipts
                 WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
                   AND project_ref = %s AND event_id = %s
                 FOR UPDATE
                """,
                (tenant_ref, workspace_ref, principal_ref, project_ref, event_id),
            )
            existing = _fetchone(cursor)
            if existing is not None:
                if existing.get("request_fingerprint") != request_fingerprint:
                    raise StateConflict("project-bootstrap-replayed-with-different-request")
                bundle = _json_value(existing.get("bootstrap_payload"), field="bootstrap-payload")
                if not isinstance(bundle, dict) or bundle.get("request") != request_subject:
                    raise StatePortError("persisted-project-bootstrap-request-mismatch")
                stored_receipt = bundle.get("receipt")
                stored_project_snapshot = bundle.get("project")
                if not isinstance(stored_receipt, dict) or not isinstance(stored_project_snapshot, dict):
                    raise StatePortError("persisted-project-bootstrap-result-required")
                validate_transition_receipt(stored_receipt)
                try:
                    validate_project_state(stored_project_snapshot)
                except ControlContextError as exc:
                    raise StatePortError("persisted-project-bootstrap-project-invalid") from exc
                if stored_receipt.get("receipt_fingerprint") != existing.get("receipt_fingerprint"):
                    raise StatePortError("persisted-project-bootstrap-receipt-fingerprint-mismatch")
                self._load_project(
                    cursor,
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    project_ref=project_ref,
                    for_update=True,
                )
                if (
                    stored_receipt.get("project_fingerprint_after") != stored_project_snapshot.get("fingerprint")
                    or stored_receipt.get("project_revision_after") != stored_project_snapshot.get("revision")
                ):
                    raise StatePortError("persisted-project-bootstrap-state-mismatch")
                result = {"project": stored_project_snapshot, "receipt": stored_receipt}
            else:
                self._insert_project(cursor, project)
                cursor.execute(
                    """
                    INSERT INTO cerebro_project_bootstrap_receipts (
                        tenant_ref, workspace_ref, principal_ref, project_ref, event_id,
                        receipt_id, decision_ref, request_fingerprint, project_revision_after,
                        project_fingerprint_after, bootstrap_payload, receipt_fingerprint
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        tenant_ref, workspace_ref, principal_ref, project_ref, event_id,
                        receipt["receipt_id"], decision_ref, request_fingerprint,
                        project["revision"], project["fingerprint"],
                        _canonical_text({
                            "request": request_subject,
                            "project": project,
                            "receipt": receipt,
                        }),
                        receipt["receipt_fingerprint"],
                    ),
                )
                if make_default:
                    self._set_default_project(
                        cursor,
                        tenant_ref=tenant_ref,
                        workspace_ref=workspace_ref,
                        principal_ref=principal_ref,
                        project_ref=project_ref,
                    )
                result = {"project": copy.deepcopy(project), "receipt": copy.deepcopy(receipt)}
        _require(result is not None, "project-bootstrap-result-missing")
        return result

    def set_default_project(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        project_ref: str,
        scopes: set[str],
    ) -> None:
        self._require_scope(scopes, "project_state:transition")
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            self._load_project(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                project_ref=project_ref,
                for_update=True,
            )
            self._set_default_project(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                principal_ref=principal_ref,
                project_ref=project_ref,
            )

    def read_project(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        project_ref: str,
        scopes: set[str],
        principal_ref: str | None = None,
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:read")
        _require(isinstance(principal_ref, str) and bool(principal_ref.strip()), "verified-principal-ref-required", StateAuthorizationError)
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            project = self._load_project(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                project_ref=project_ref,
            )
        _require(project is not None, "project-read-result-missing")
        return project

    def bind_session(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        session_binding_id: str,
        scopes: set[str],
        project_ref: str | None = None,
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:transition")
        self._validate_text_fields(
            {
                "tenant_ref": tenant_ref,
                "workspace_ref": workspace_ref,
                "principal_ref": principal_ref,
                "consumer_ref": consumer_ref,
                "session_ref": session_ref,
                "session_binding_id": session_binding_id,
            },
            "bind",
            (
                "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref",
                "session_ref", "session_binding_id",
            ),
        )
        result: dict[str, Any] | None = None
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            existing = self._load_session(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                principal_ref=principal_ref,
                consumer_ref=consumer_ref,
                session_ref=session_ref,
                for_update=True,
                required=False,
            )
            if existing is not None:
                if project_ref is not None and existing["project_ref"] != project_ref:
                    raise StateBindingError("control-session-already-bound-to-different-project")
                result = existing
            else:
                resolved_project = project_ref
                if resolved_project is None:
                    cursor.execute(
                        """
                        SELECT active_project_ref
                          FROM cerebro_principal_project_bindings
                         WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
                         FOR SHARE
                        """,
                        (tenant_ref, workspace_ref, principal_ref),
                    )
                    binding_row = _fetchone(cursor)
                    if binding_row is not None:
                        resolved_project = binding_row.get("active_project_ref")
                _require(
                    isinstance(resolved_project, str) and bool(resolved_project),
                    "active-project-binding-required",
                    StateBindingError,
                )
                project = self._load_project(
                    cursor,
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    project_ref=resolved_project,
                    for_update=True,
                )
                _require(project is not None, "session-bind-project-missing", StateBindingError)
                candidate = bind_control_session(
                    project,
                    session_binding_id=session_binding_id,
                    principal_ref=principal_ref,
                    consumer_ref=consumer_ref,
                    session_ref=session_ref,
                )
                self._insert_session(cursor, candidate)
                actual = self._load_session(
                    cursor,
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    principal_ref=principal_ref,
                    consumer_ref=consumer_ref,
                    session_ref=session_ref,
                    project=project,
                    for_update=True,
                )
                _require(actual is not None, "session-bind-collision-missing", StateConflict)
                if actual["project_ref"] != resolved_project:
                    raise StateBindingError("control-session-already-bound-to-different-project")
                result = actual
        _require(result is not None, "session-bind-result-missing")
        return result

    def read_session(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:read")
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            session = self._load_session(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                principal_ref=principal_ref,
                consumer_ref=consumer_ref,
                session_ref=session_ref,
            )
            _require(session is not None, "session-read-result-missing", StateBindingError)
            project = self._load_project(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                project_ref=session["project_ref"],
            )
            _require(project is not None, "session-read-project-missing", StateBindingError)
            if session["project_revision"] != project["revision"]:
                raise StateBindingError("control-session-stale-requires-begin-event-rehydrate")
            try:
                validate_session_state(session, project)
            except ControlContextError as exc:
                raise StatePortError("persisted-control-session-state-invalid") from exc
        return session

    def _load_event_by_idempotency(
        self,
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        idempotency_key: str,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        lock = " FOR UPDATE" if for_update else ""
        cursor.execute(
            """
            SELECT project_ref, event_id, idempotency_key, begin_request_fingerprint,
                   completion_request_fingerprint, completion_fingerprint,
                   expected_project_revision, expected_session_revision,
                   expected_project_fingerprint, expected_session_fingerprint,
                   event_state, event_payload
              FROM cerebro_control_events
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
               AND consumer_ref = %s AND session_ref = %s AND idempotency_key = %s
            """ + lock,
            (
                tenant_ref, workspace_ref, principal_ref, consumer_ref,
                session_ref, idempotency_key,
            ),
        )
        return self._decode_event_row(_fetchone(cursor))

    def _load_event_by_id(
        self,
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        event_id: str,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        lock = " FOR UPDATE" if for_update else ""
        cursor.execute(
            """
            SELECT project_ref, event_id, idempotency_key, begin_request_fingerprint,
                   completion_request_fingerprint, completion_fingerprint,
                   expected_project_revision, expected_session_revision,
                   expected_project_fingerprint, expected_session_fingerprint,
                   event_state, event_payload
              FROM cerebro_control_events
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
               AND consumer_ref = %s AND session_ref = %s AND event_id = %s
            """ + lock,
            (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, event_id),
        )
        return self._decode_event_row(_fetchone(cursor))

    @staticmethod
    def _decode_event_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = _json_value(row.get("event_payload"), field="event-payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("binding"), dict):
            raise StatePortError("persisted-event-payload-invalid")
        binding = payload["binding"]
        for row_field, binding_field in (
            ("event_id", "event_id"),
            ("idempotency_key", "idempotency_key"),
            ("expected_project_revision", "expected_project_revision"),
            ("expected_session_revision", "expected_session_revision"),
            ("expected_project_fingerprint", "expected_project_fingerprint"),
            ("expected_session_fingerprint", "expected_session_fingerprint"),
        ):
            if row.get(row_field) != binding.get(binding_field):
                raise StatePortError(f"persisted-event-{row_field}-projection-mismatch")
        project = binding.get("project")
        if not isinstance(project, dict) or project.get("project_ref") != row.get("project_ref"):
            raise StatePortError("persisted-event-project-projection-mismatch")
        value = dict(row)
        value["payload"] = payload
        return value

    @staticmethod
    def _event_replay(event: dict[str, Any], request_fingerprint: str) -> dict[str, Any]:
        if event.get("begin_request_fingerprint") != request_fingerprint:
            raise StateConflict("idempotency-key-reused-with-different-begin-request")
        payload = event["payload"]
        if event.get("event_state") == "COMPLETED":
            completion = payload.get("completion")
            if not isinstance(completion, dict):
                raise StatePortError("persisted-completed-event-missing-completion")
            if event.get("completion_fingerprint") != _sha256(completion):
                raise StatePortError("persisted-event-completion-fingerprint-mismatch")
            return copy.deepcopy(completion)
        if event.get("event_state") != "OPEN":
            raise StateBindingError("event-not-open")
        return copy.deepcopy(payload["binding"])

    def begin_event(self, request: dict[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:transition")
        _require(request.get("schema") == BEGIN_SCHEMA, "begin-event-schema-mismatch")
        fields = (
            "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref",
            "event_id", "idempotency_key",
        )
        self._validate_text_fields(request, "begin", fields)
        begin_fingerprint = _sha256(request)
        result: dict[str, Any] | None = None
        with self._transaction(
            tenant_ref=request["tenant_ref"],
            workspace_ref=request["workspace_ref"],
            principal_ref=request["principal_ref"],
        ) as cursor:
            existing = self._load_event_by_idempotency(
                cursor,
                tenant_ref=request["tenant_ref"],
                workspace_ref=request["workspace_ref"],
                principal_ref=request["principal_ref"],
                consumer_ref=request["consumer_ref"],
                session_ref=request["session_ref"],
                idempotency_key=request["idempotency_key"],
                for_update=True,
            )
            if existing is not None:
                result = self._event_replay(existing, begin_fingerprint)
            else:
                session_before = self._load_session(
                    cursor,
                    tenant_ref=request["tenant_ref"],
                    workspace_ref=request["workspace_ref"],
                    principal_ref=request["principal_ref"],
                    consumer_ref=request["consumer_ref"],
                    session_ref=request["session_ref"],
                    for_update=True,
                )
                _require(session_before is not None, "begin-event-session-missing", StateBindingError)
                project = self._load_project(
                    cursor,
                    tenant_ref=request["tenant_ref"],
                    workspace_ref=request["workspace_ref"],
                    project_ref=session_before["project_ref"],
                    for_update=True,
                )
                _require(project is not None, "begin-event-project-missing", StateBindingError)
                try:
                    session = rehydrate_control_session(copy.deepcopy(session_before), project)
                except (ControlContextError, StatePortError) as exc:
                    raise StateBindingError("control-session-rehydration-failed") from exc
                self._write_session_cas(cursor, session_before, session)
                rehydration_receipt = None
                if session != session_before:
                    rehydration_receipt = {
                        "schema": "cerebro-control-session-rehydration-receipt/v1",
                        "project_ref": project["project_ref"],
                        "project_revision": project["revision"],
                        "session_revision_before": session_before["session_revision"],
                        "session_revision_after": session["session_revision"],
                        "session_fingerprint_before": session_before["fingerprint"],
                        "session_fingerprint_after": session["fingerprint"],
                        "active_context_before": session_before["active_context_ref"],
                        "active_context_after": session["active_context_ref"],
                        "continuation_preserved": (
                            session_before.get("active_continuation_binding") is not None
                            and session.get("active_continuation_binding") is not None
                        ),
                    }
                    rehydration_receipt["receipt_id"] = "CSR-" + _sha256(rehydration_receipt)[:24].upper()
                binding = {
                    "schema": BEGIN_RESULT_SCHEMA,
                    "event_id": request["event_id"],
                    "idempotency_key": request["idempotency_key"],
                    "project": copy.deepcopy(project),
                    "session": copy.deepcopy(session),
                    "expected_project_revision": project["revision"],
                    "expected_project_fingerprint": project["fingerprint"],
                    "expected_session_revision": session["session_revision"],
                    "expected_session_fingerprint": session["fingerprint"],
                    "repository_permission_required": False,
                    "rehydration_receipt": rehydration_receipt,
                }
                cursor.execute(
                    """
                    INSERT INTO cerebro_control_events (
                        tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                        project_ref, event_id, idempotency_key, begin_request_fingerprint,
                        expected_project_revision, expected_session_revision,
                        expected_project_fingerprint, expected_session_fingerprint,
                        event_state, event_payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        request["tenant_ref"], request["workspace_ref"], request["principal_ref"],
                        request["consumer_ref"], request["session_ref"], project["project_ref"],
                        request["event_id"], request["idempotency_key"], begin_fingerprint,
                        project["revision"], session["session_revision"], project["fingerprint"],
                        session["fingerprint"], _canonical_text({"binding": binding, "completion": None}),
                    ),
                )
                if cursor.rowcount == 1:
                    result = binding
                else:
                    collision = self._load_event_by_idempotency(
                        cursor,
                        tenant_ref=request["tenant_ref"],
                        workspace_ref=request["workspace_ref"],
                        principal_ref=request["principal_ref"],
                        consumer_ref=request["consumer_ref"],
                        session_ref=request["session_ref"],
                        idempotency_key=request["idempotency_key"],
                        for_update=True,
                    )
                    if collision is None:
                        raise StateConflict("event-id-already-used")
                    result = self._event_replay(collision, begin_fingerprint)
        _require(result is not None, "begin-event-result-missing")
        return result

    @staticmethod
    def _assert_no_other_session_invalidated(
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        project_ref: str,
        project_operations: list[dict[str, Any]],
    ) -> None:
        ending_targets = sorted(
            {
                operation.get("context_ref")
                for operation in project_operations
                if operation.get("operation") in {"RETURN_CONTEXT", "CANCEL_CONTEXT"}
                and isinstance(operation.get("context_ref"), str)
            }
        )
        if not ending_targets:
            return
        cursor.execute(
            """
            SELECT session_ref
              FROM cerebro_control_session_bindings
             WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
               AND project_ref = %s AND active_context_ref = ANY(%s)
               AND NOT (consumer_ref = %s AND session_ref = %s)
             LIMIT 1
             FOR SHARE
            """,
            (
                tenant_ref, workspace_ref, principal_ref, project_ref,
                ending_targets, consumer_ref, session_ref,
            ),
        )
        if _fetchone(cursor) is not None:
            raise StateConflict("context-active-in-another-control-session")

    @staticmethod
    def _insert_transition_receipt(
        cursor: Any,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        receipt: dict[str, Any],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO cerebro_transition_receipts (
                tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                event_id, receipt_id, result, mutated, project_revision_before,
                project_revision_after, session_revision_before, session_revision_after,
                project_fingerprint_before, project_fingerprint_after,
                session_fingerprint_before, session_fingerprint_after, decision_ref,
                active_context_ref_after, transition_payload, receipt_fingerprint
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            """,
            (
                tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                receipt["event_id"], receipt["receipt_id"], receipt["result"], receipt["mutated"],
                receipt["project_revision_before"], receipt["project_revision_after"],
                receipt["session_revision_before"], receipt["session_revision_after"],
                receipt["project_fingerprint_before"], receipt["project_fingerprint_after"],
                receipt["session_fingerprint_before"], receipt["session_fingerprint_after"],
                receipt["decision_ref"], receipt["active_context_ref_after"],
                _canonical_text(receipt), receipt["receipt_fingerprint"],
            ),
        )

    @staticmethod
    def _insert_state_commit_receipt(
        cursor: Any,
        *,
        receipt: dict[str, Any],
    ) -> None:
        cursor.execute(
            """
            INSERT INTO cerebro_state_commit_receipts (
                tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref,
                project_ref, event_id, commit_ref, transition_receipt_id,
                transition_receipt_fingerprint, transition_directive_fingerprint,
                owner_effect_candidate_ref, owner_effect_candidate_fingerprint,
                project_revision_after,
                session_revision_after, project_fingerprint_after,
                session_fingerprint_after, commit_payload, commit_fingerprint
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
            )
            """,
            (
                receipt["tenant_ref"], receipt["workspace_ref"], receipt["principal_ref"],
                receipt["consumer_ref"], receipt["session_ref"], receipt["project_ref"],
                receipt["event_id"], receipt["commit_ref"], receipt["transition_receipt_ref"],
                receipt["transition_receipt_fingerprint"], receipt["transition_directive_fingerprint"],
                receipt["owner_effect_candidate_ref"], receipt["owner_effect_candidate_fingerprint"],
                receipt["project_revision_after"],
                receipt["session_revision_after"], receipt["project_fingerprint_after"],
                receipt["session_fingerprint_after"], _canonical_text(receipt),
                receipt["commit_fingerprint"],
            ),
        )

    def complete_event(self, request: dict[str, Any], *, scopes: set[str]) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:transition")
        fields = (
            "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref", "event_id",
        )
        self._validate_text_fields(request, "complete", fields)
        directive = request.get("directive")
        _require(
            isinstance(directive, dict) and directive.get("schema") == DIRECTIVE_SCHEMA,
            "complete-directive-required",
        )
        _require(directive.get("event_id") == request["event_id"], "complete-event-id-mismatch")
        request_fingerprint = _sha256(request)
        result: dict[str, Any] | None = None
        with self._transaction(
            tenant_ref=request["tenant_ref"],
            workspace_ref=request["workspace_ref"],
            principal_ref=request["principal_ref"],
        ) as cursor:
            event = self._load_event_by_id(
                cursor,
                tenant_ref=request["tenant_ref"],
                workspace_ref=request["workspace_ref"],
                principal_ref=request["principal_ref"],
                consumer_ref=request["consumer_ref"],
                session_ref=request["session_ref"],
                event_id=request["event_id"],
                for_update=True,
            )
            if event is None:
                raise StateBindingError("event-not-open")
            if event["event_state"] == "COMPLETED":
                if event.get("completion_request_fingerprint") != request_fingerprint:
                    raise StateConflict("completed-event-replayed-with-different-directive")
                completion = event["payload"].get("completion")
                if not isinstance(completion, dict):
                    raise StatePortError("persisted-completed-event-missing-completion")
                if event.get("completion_fingerprint") != _sha256(completion):
                    raise StatePortError("persisted-event-completion-fingerprint-mismatch")
                state_commit = completion.get("state_commit")
                if not isinstance(state_commit, dict):
                    raise StatePortError("persisted-completion-state-commit-required")
                validate_state_commit_receipt(state_commit)
                result = copy.deepcopy(completion)
            else:
                if event["event_state"] != "OPEN":
                    raise StateBindingError("event-not-open")
                begin = event["payload"]["binding"]
                project = self._load_project(
                    cursor,
                    tenant_ref=request["tenant_ref"],
                    workspace_ref=request["workspace_ref"],
                    project_ref=event["project_ref"],
                    for_update=True,
                )
                session = self._load_session(
                    cursor,
                    tenant_ref=request["tenant_ref"],
                    workspace_ref=request["workspace_ref"],
                    principal_ref=request["principal_ref"],
                    consumer_ref=request["consumer_ref"],
                    session_ref=request["session_ref"],
                    for_update=True,
                )
                _require(project is not None, "complete-event-project-missing", StateBindingError)
                _require(session is not None, "complete-event-session-missing", StateBindingError)
                if (
                    project["revision"] != begin["expected_project_revision"]
                    or project["fingerprint"] != begin["expected_project_fingerprint"]
                ):
                    raise StateConflict("project-state-conflict-reload-and-reresolve")
                if (
                    session["session_revision"] != begin["expected_session_revision"]
                    or session["fingerprint"] != begin["expected_session_fingerprint"]
                ):
                    raise StateConflict("session-state-conflict-reload-and-reresolve")
                try:
                    validate_session_state(session, project)
                except ControlContextError as exc:
                    raise StatePortError("persisted-control-session-state-invalid") from exc
                project_operations = directive.get("project_operations")
                _require(isinstance(project_operations, list), "project-operations-array-required")
                self._assert_no_other_session_invalidated(
                    cursor,
                    tenant_ref=request["tenant_ref"],
                    workspace_ref=request["workspace_ref"],
                    principal_ref=request["principal_ref"],
                    consumer_ref=request["consumer_ref"],
                    session_ref=request["session_ref"],
                    project_ref=project["project_ref"],
                    project_operations=project_operations,
                )
                try:
                    project_after, session_after, transition_receipt = apply_transition(
                        project,
                        session,
                        directive,
                    )
                except ControlContextError as exc:
                    raise StateConflict(str(exc)) from exc
                validate_transition_receipt(transition_receipt)
                owner_effect_candidate = request.get("owner_effect_candidate")
                if owner_effect_candidate is not None:
                    validate_context_owner_candidate_binding(
                        owner_effect_candidate,
                        directive=directive,
                        project_before=project,
                        project_after=project_after,
                        transition_receipt=transition_receipt,
                    )
                self._write_project_cas(cursor, project, project_after)
                self._write_session_cas(cursor, session, session_after)
                self._insert_transition_receipt(
                    cursor,
                    tenant_ref=request["tenant_ref"],
                    workspace_ref=request["workspace_ref"],
                    principal_ref=request["principal_ref"],
                    consumer_ref=request["consumer_ref"],
                    session_ref=request["session_ref"],
                    receipt=transition_receipt,
                )
                state_commit = build_state_commit_receipt(
                    tenant_ref=request["tenant_ref"],
                    workspace_ref=request["workspace_ref"],
                    principal_ref=request["principal_ref"],
                    consumer_ref=request["consumer_ref"],
                    session_ref=request["session_ref"],
                    project_ref=project_after["project_ref"],
                    event_id=request["event_id"],
                    directive=directive,
                    owner_effect_candidate=owner_effect_candidate,
                    transition_receipt=transition_receipt,
                    project=project_after,
                    session=session_after,
                )
                self._insert_state_commit_receipt(cursor, receipt=state_commit)
                completion = {
                    "schema": COMPLETE_RESULT_SCHEMA,
                    "event_id": request["event_id"],
                    "result": "PASS",
                    "receipt": copy.deepcopy(transition_receipt),
                    "state_commit": copy.deepcopy(state_commit),
                    "project": copy.deepcopy(project_after),
                    "session": copy.deepcopy(session_after),
                    "repository_permission_required": False,
                }
                completion_fingerprint = _sha256(completion)
                cursor.execute(
                    """
                    UPDATE cerebro_control_events
                       SET event_state = 'COMPLETED', completion_request_fingerprint = %s,
                           completion_fingerprint = %s, event_payload = %s::jsonb,
                           completed_at = now()
                     WHERE tenant_ref = %s AND workspace_ref = %s AND principal_ref = %s
                       AND consumer_ref = %s AND session_ref = %s AND event_id = %s
                       AND event_state = 'OPEN' AND completion_request_fingerprint IS NULL
                    """,
                    (
                        request_fingerprint, completion_fingerprint,
                        _canonical_text({
                            "binding": begin,
                            "completion_request": {
                                "directive": copy.deepcopy(directive),
                                "navigation_options_candidate_fingerprint": request.get(
                                    "navigation_options_candidate_fingerprint"
                                ),
                                "owner_effect_candidate": copy.deepcopy(
                                    request.get("owner_effect_candidate")
                                ),
                            },
                            "completion": completion,
                        }),
                        request["tenant_ref"], request["workspace_ref"], request["principal_ref"],
                        request["consumer_ref"], request["session_ref"], request["event_id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateConflict("event-completion-conflict")
                result = completion
        _require(result is not None, "complete-event-result-missing")
        return result

    def write_actor_generation_shadow(
        self,
        state: dict[str, Any],
        *,
        expected_revision: int,
        principal_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Persist a typed actor-generation shadow with CAS and immutable history."""

        self._require_scope(scopes, "project_state:transition")
        validate_actor_generation_shadow(state)
        _require(state["revision"] == expected_revision + 1, "actor-generation-shadow-revision-step-invalid", StateConflict)
        payload = _canonical_text(state)
        with self._transaction(
            tenant_ref=state["tenant_ref"], workspace_ref=state["workspace_ref"], principal_ref=principal_ref,
        ) as cursor:
            if expected_revision == 0:
                cursor.execute(
                    """
                    INSERT INTO cerebro_actor_generation_shadow_heads (
                        tenant_ref, workspace_ref, actor_ref, actor_role, generation_ref,
                        lifecycle, source_revision, aggregate_revision, aggregate_fingerprint, shadow_payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (state["tenant_ref"], state["workspace_ref"], state["actor_ref"], state["role"],
                     state["generation_ref"], state["lifecycle"], state["source_revision"], state["revision"],
                     state["fingerprint"], payload),
                )
            else:
                cursor.execute(
                    """
                    UPDATE cerebro_actor_generation_shadow_heads
                       SET lifecycle = %s, source_revision = %s, aggregate_revision = %s,
                           aggregate_fingerprint = %s, shadow_payload = %s::jsonb, updated_at = now()
                     WHERE tenant_ref = %s AND workspace_ref = %s AND actor_role = %s
                       AND generation_ref = %s AND aggregate_revision = %s AND actor_ref = %s
                    """,
                    (state["lifecycle"], state["source_revision"], state["revision"], state["fingerprint"], payload,
                     state["tenant_ref"], state["workspace_ref"], state["role"], state["generation_ref"],
                     expected_revision, state["actor_ref"]),
                )
            if cursor.rowcount != 1:
                raise StateConflict("actor-generation-shadow-revision-conflict")
            cursor.execute(
                """
                INSERT INTO cerebro_actor_generation_shadow_revisions (
                    tenant_ref, workspace_ref, actor_role, generation_ref,
                    aggregate_revision, aggregate_fingerprint, shadow_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (state["tenant_ref"], state["workspace_ref"], state["role"], state["generation_ref"],
                 state["revision"], state["fingerprint"], payload),
            )
        return copy.deepcopy(state)

    def read_actor_generation_shadow(
        self, *, tenant_ref: str, workspace_ref: str, role: str, generation_ref: str,
        principal_ref: str, scopes: set[str],
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:read")
        state: dict[str, Any] | None = None
        with self._transaction(tenant_ref=tenant_ref, workspace_ref=workspace_ref, principal_ref=principal_ref) as cursor:
            cursor.execute(
                """
                SELECT shadow_payload FROM cerebro_actor_generation_shadow_heads
                 WHERE tenant_ref = %s AND workspace_ref = %s AND actor_role = %s AND generation_ref = %s
                 FOR SHARE
                """,
                (tenant_ref, workspace_ref, role, generation_ref),
            )
            row = _fetchone(cursor)
            if row is None:
                raise StateBindingError("actor-generation-shadow-not-found")
            value = _json_value(row.get("shadow_payload"), field="actor-generation-shadow-payload")
            _require(isinstance(value, dict), "actor-generation-shadow-payload-invalid")
            state = value
            validate_actor_generation_shadow(state)
        _require(state is not None, "actor-generation-shadow-not-found", StateBindingError)
        return copy.deepcopy(state)

    def write_work_claim_shadow(
        self,
        state: dict[str, Any],
        *,
        expected_revision: int,
        principal_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Persist a non-live work-claim shadow with CAS and immutable history."""

        self._require_scope(scopes, "project_state:transition")
        validate_work_claim_shadow(state)
        _require(state["revision"] == expected_revision + 1, "work-claim-shadow-revision-step-invalid", StateConflict)
        payload = _canonical_text(state)
        with self._transaction(
            tenant_ref=state["tenant_ref"], workspace_ref=state["workspace_ref"], principal_ref=principal_ref,
        ) as cursor:
            cursor.execute(
                """
                SELECT shadow_payload FROM cerebro_actor_generation_shadow_heads
                 WHERE tenant_ref = %s AND workspace_ref = %s AND actor_role = %s
                   AND generation_ref = %s FOR SHARE
                """,
                (state["tenant_ref"], state["workspace_ref"], state["actor_role"], state["actor_generation_ref"]),
            )
            actor_row = _fetchone(cursor)
            if actor_row is None:
                raise StateBindingError("work-claim-shadow-actor-generation-not-found")
            actor_state = _json_value(actor_row.get("shadow_payload"), field="actor-generation-shadow-payload")
            _require(isinstance(actor_state, dict), "actor-generation-shadow-payload-invalid")
            validate_work_claim_shadow(state, actor_state)
            if expected_revision == 0:
                cursor.execute(
                    """
                    INSERT INTO cerebro_work_claim_shadow_heads (
                        tenant_ref, workspace_ref, claim_ref, project_ref, actor_ref, actor_role,
                        actor_generation_ref, scope_ref, claim_mode, lifecycle, source_revision,
                        aggregate_revision, aggregate_fingerprint, shadow_payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    """,
                    (state["tenant_ref"], state["workspace_ref"], state["claim_ref"], state["project_ref"],
                     state["actor_ref"], state["actor_role"], state["actor_generation_ref"], state["scope_ref"],
                     state["mode"], state["lifecycle"], state["source_revision"], state["revision"],
                     state["fingerprint"], payload),
                )
            else:
                cursor.execute(
                    """
                    UPDATE cerebro_work_claim_shadow_heads
                       SET lifecycle = %s, source_revision = %s, aggregate_revision = %s,
                           aggregate_fingerprint = %s, shadow_payload = %s::jsonb, updated_at = now()
                     WHERE tenant_ref = %s AND workspace_ref = %s AND claim_ref = %s
                       AND aggregate_revision = %s AND project_ref = %s AND actor_ref = %s
                       AND actor_role = %s AND actor_generation_ref = %s AND scope_ref = %s AND claim_mode = %s
                    """,
                    (state["lifecycle"], state["source_revision"], state["revision"], state["fingerprint"], payload,
                     state["tenant_ref"], state["workspace_ref"], state["claim_ref"], expected_revision,
                     state["project_ref"], state["actor_ref"], state["actor_role"], state["actor_generation_ref"],
                     state["scope_ref"], state["mode"]),
                )
            if cursor.rowcount != 1:
                raise StateConflict("work-claim-shadow-revision-conflict")
            cursor.execute(
                """
                INSERT INTO cerebro_work_claim_shadow_revisions (
                    tenant_ref, workspace_ref, claim_ref, aggregate_revision, aggregate_fingerprint, shadow_payload
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """,
                (state["tenant_ref"], state["workspace_ref"], state["claim_ref"],
                 state["revision"], state["fingerprint"], payload),
            )
        return copy.deepcopy(state)

    def read_work_claim_shadow(
        self, *, tenant_ref: str, workspace_ref: str, claim_ref: str,
        principal_ref: str, scopes: set[str],
    ) -> dict[str, Any]:
        self._require_scope(scopes, "project_state:read")
        state: dict[str, Any] | None = None
        with self._transaction(tenant_ref=tenant_ref, workspace_ref=workspace_ref, principal_ref=principal_ref) as cursor:
            cursor.execute(
                """
                SELECT shadow_payload FROM cerebro_work_claim_shadow_heads
                 WHERE tenant_ref = %s AND workspace_ref = %s AND claim_ref = %s FOR SHARE
                """,
                (tenant_ref, workspace_ref, claim_ref),
            )
            row = _fetchone(cursor)
            if row is None:
                raise StateBindingError("work-claim-shadow-not-found")
            value = _json_value(row.get("shadow_payload"), field="work-claim-shadow-payload")
            _require(isinstance(value, dict), "work-claim-shadow-payload-invalid")
            state = value
            validate_work_claim_shadow(state)
        _require(state is not None, "work-claim-shadow-not-found", StateBindingError)
        return copy.deepcopy(state)

    def read_state_commit_evidence(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        consumer_ref: str,
        session_ref: str,
        commit_ref: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Read and internally cross-check one durable commit evidence bundle.

        This is an internal verifier port, not an exposed MCP tool.  Every scope
        coordinate is constructor-bound by the trusted verifier in normal use.
        """

        self._require_scope(scopes, "project_state:read")
        self._validate_text_fields(
            {
                "tenant_ref": tenant_ref,
                "workspace_ref": workspace_ref,
                "principal_ref": principal_ref,
                "consumer_ref": consumer_ref,
                "session_ref": session_ref,
                "commit_ref": commit_ref,
            },
            "state-commit-read",
            (
                "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref",
                "session_ref", "commit_ref",
            ),
        )
        bundle: dict[str, Any] | None = None
        with self._transaction(
            tenant_ref=tenant_ref,
            workspace_ref=workspace_ref,
            principal_ref=principal_ref,
        ) as cursor:
            cursor.execute(
                """
                SELECT s.project_ref, s.event_id, s.commit_payload, s.commit_fingerprint,
                       s.transition_receipt_id, s.transition_receipt_fingerprint,
                       s.transition_directive_fingerprint,
                       s.owner_effect_candidate_ref, s.owner_effect_candidate_fingerprint,
                       t.transition_payload, t.receipt_fingerprint,
                       e.event_payload, e.completion_fingerprint, e.event_state
                  FROM cerebro_state_commit_receipts AS s
                  JOIN cerebro_transition_receipts AS t
                    ON t.tenant_ref = s.tenant_ref
                   AND t.workspace_ref = s.workspace_ref
                   AND t.principal_ref = s.principal_ref
                   AND t.consumer_ref = s.consumer_ref
                   AND t.session_ref = s.session_ref
                   AND t.event_id = s.event_id
                   AND t.receipt_id = s.transition_receipt_id
                  JOIN cerebro_control_events AS e
                    ON e.tenant_ref = s.tenant_ref
                   AND e.workspace_ref = s.workspace_ref
                   AND e.principal_ref = s.principal_ref
                   AND e.consumer_ref = s.consumer_ref
                   AND e.session_ref = s.session_ref
                   AND e.event_id = s.event_id
                 WHERE s.tenant_ref = %s AND s.workspace_ref = %s AND s.principal_ref = %s
                   AND s.consumer_ref = %s AND s.session_ref = %s AND s.commit_ref = %s
                 FOR SHARE OF s, t, e
                """,
                (tenant_ref, workspace_ref, principal_ref, consumer_ref, session_ref, commit_ref),
            )
            row = _fetchone(cursor)
            if row is None:
                raise StateBindingError("state-commit-evidence-not-found")
            commit = _json_value(row.get("commit_payload"), field="state-commit-payload")
            transition = _json_value(row.get("transition_payload"), field="transition-payload")
            event_payload = _json_value(row.get("event_payload"), field="event-payload")
            if not all(isinstance(value, dict) for value in (commit, transition, event_payload)):
                raise StatePortError("state-commit-evidence-payload-invalid")
            completion_request = event_payload.get("completion_request")
            completion = event_payload.get("completion")
            if not isinstance(completion_request, dict) or not isinstance(completion, dict):
                raise StatePortError("state-commit-event-completion-evidence-invalid")
            directive = completion_request.get("directive")
            owner_effect_candidate = completion_request.get("owner_effect_candidate")
            project_after = completion.get("project")
            session_after = completion.get("session")
            if not all(isinstance(value, dict) for value in (directive, project_after, session_after)):
                raise StatePortError("state-commit-completion-state-evidence-invalid")
            if row.get("event_state") != "COMPLETED":
                raise StatePortError("state-commit-event-not-completed")
            if row.get("completion_fingerprint") != _sha256(completion):
                raise StatePortError("state-commit-completion-fingerprint-mismatch")
            for payload_field, row_field in (
                ("commit_fingerprint", "commit_fingerprint"),
                ("transition_receipt_ref", "transition_receipt_id"),
                ("transition_receipt_fingerprint", "transition_receipt_fingerprint"),
                ("transition_directive_fingerprint", "transition_directive_fingerprint"),
                ("owner_effect_candidate_ref", "owner_effect_candidate_ref"),
                ("owner_effect_candidate_fingerprint", "owner_effect_candidate_fingerprint"),
            ):
                if commit.get(payload_field) != row.get(row_field):
                    raise StatePortError(f"state-commit-{payload_field}-projection-mismatch")
            if transition.get("receipt_fingerprint") != row.get("receipt_fingerprint"):
                raise StatePortError("state-commit-transition-ledger-fingerprint-mismatch")
            if completion.get("state_commit") != commit or completion.get("receipt") != transition:
                raise StatePortError("state-commit-completion-bundle-mismatch")
            if commit.get("owner_effect_candidate_ref") is None:
                if owner_effect_candidate is not None:
                    raise StatePortError("state-commit-unbound-owner-candidate-present")
            elif not isinstance(owner_effect_candidate, dict):
                raise StatePortError("state-commit-bound-owner-candidate-missing")
            validate_state_commit_receipt(
                commit,
                directive=directive,
                owner_effect_candidate=(
                    owner_effect_candidate if isinstance(owner_effect_candidate, dict) else None
                ),
                transition_receipt=transition,
                project=project_after,
                session=session_after,
            )
            current_project = self._load_project(
                cursor,
                tenant_ref=tenant_ref,
                workspace_ref=workspace_ref,
                project_ref=row["project_ref"],
                for_update=True,
            )
            _require(current_project is not None, "state-commit-current-project-missing", StateBindingError)
            bundle = {
                "schema": "cerebro-state-service-commit-evidence-bundle/v1",
                "commit": copy.deepcopy(commit),
                "transition_receipt": copy.deepcopy(transition),
                "directive": copy.deepcopy(directive),
                "owner_effect_candidate": copy.deepcopy(owner_effect_candidate),
                "completion": copy.deepcopy(completion),
                "current_project": copy.deepcopy(current_project),
            }
        _require(bundle is not None, "state-commit-evidence-bundle-missing")
        return bundle
