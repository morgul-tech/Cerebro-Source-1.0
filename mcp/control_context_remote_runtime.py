#!/usr/bin/env python3
"""Provider-neutral production composition for the remote control-context MCP.

This module assembles the already-governed PostgreSQL state port, OAuth service
boundary and official MCP SDK ASGI app.  It does not select an identity provider,
store credentials, apply database migrations, open a listener or deploy itself.
Secrets and provider adapters are constructor-bound runtime dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping


SOURCE_ROOT = Path(__file__).resolve().parents[1]
CONTEXT_TOOLING = SOURCE_ROOT / "tooling" / "context"
if str(CONTEXT_TOOLING) not in sys.path:
    sys.path.insert(0, str(CONTEXT_TOOLING))

from control_context_mcp_sdk import (  # noqa: E402
    DEFAULT_MAX_REQUEST_BODY_SIZE,
    create_streamable_http_app,
    official_mcp_sdk_runtime,
)
from control_context_remote_service import (  # noqa: E402
    HEALTH_PATH,
    MCP_PATH,
    PROTECTED_RESOURCE_PATH,
    ControlContextRemoteMcpService,
    RemoteMcpServiceConfig,
)
from control_context_state_postgres import (  # noqa: E402
    DEFAULT_MANIFEST,
    MIGRATION_MANIFEST_SCHEMA,
    PostgresControlContextStatePort,
    make_psycopg_connection_factory,
)
from control_context_tools import ControlContextMcpTools  # noqa: E402


REMOTE_RUNTIME_SCHEMA = "cerebro-control-context-remote-runtime/v1"
REMOTE_RUNTIME_CONFIG_SCHEMA = "cerebro-control-context-remote-runtime-config/v1"
RUNTIME_BACKEND = "POSTGRESQL"
APPLICATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
REQUIRED_POSTGRES_RELATIONS = (
    "cerebro_schema_migrations",
    "cerebro_project_instances",
    "cerebro_project_basis_revisions",
    "cerebro_principal_project_bindings",
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
)


class ControlContextRemoteRuntimeError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlContextRemoteRuntimeError(message)


def _exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], prefix: str) -> None:
    _require(not required.difference(value), f"{prefix}-required-fields-missing")
    _require(not set(value).difference(allowed), f"{prefix}-unknown-field-prohibited")


def _tuple_of_text(value: Any, *, field_name: str, allow_empty: bool) -> tuple[str, ...]:
    _require(isinstance(value, list), f"{field_name}-array-required")
    _require(allow_empty or bool(value), f"{field_name}-must-not-be-empty")
    result: list[str] = []
    for item in value:
        _require(isinstance(item, str) and item == item.strip() and bool(item), f"{field_name}-item-invalid")
        result.append(item)
    _require(len(result) == len(set(result)), f"{field_name}-duplicate")
    return tuple(result)


@dataclass(frozen=True)
class ControlContextRemoteRuntimeConfig:
    service: RemoteMcpServiceConfig
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...] = ()
    max_request_body_size: int = DEFAULT_MAX_REQUEST_BODY_SIZE
    postgres_connect_timeout_seconds: int = 10
    postgres_application_name: str = "cerebro-control-context-state-service"
    state_backend: str = RUNTIME_BACKEND
    runtime_applies_migrations: bool = False

    def __post_init__(self) -> None:
        _require(isinstance(self.service, RemoteMcpServiceConfig), "runtime-service-config-required")
        _require(isinstance(self.allowed_hosts, tuple) and bool(self.allowed_hosts), "runtime-allowed-hosts-required")
        _require(isinstance(self.allowed_origins, tuple), "runtime-allowed-origins-tuple-required")
        _require(
            isinstance(self.max_request_body_size, int)
            and not isinstance(self.max_request_body_size, bool)
            and 1024 <= self.max_request_body_size <= 4 * 1024 * 1024,
            "runtime-max-request-body-size-invalid",
        )
        _require(
            isinstance(self.postgres_connect_timeout_seconds, int)
            and not isinstance(self.postgres_connect_timeout_seconds, bool)
            and 1 <= self.postgres_connect_timeout_seconds <= 60,
            "runtime-postgres-connect-timeout-invalid",
        )
        _require(
            isinstance(self.postgres_application_name, str)
            and APPLICATION_NAME.fullmatch(self.postgres_application_name) is not None,
            "runtime-postgres-application-name-invalid",
        )
        _require(self.state_backend == RUNTIME_BACKEND, "runtime-state-backend-must-be-PostgreSQL")
        _require(self.runtime_applies_migrations is False, "runtime-role-must-not-apply-migrations")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ControlContextRemoteRuntimeConfig":
        _require(isinstance(value, Mapping), "runtime-config-object-required")
        _exact_keys(
            value,
            {"schema", "service", "transport_security", "state_backend", "postgres"},
            {"schema", "service", "transport_security", "state_backend", "postgres"},
            "runtime-config",
        )
        _require(value.get("schema") == REMOTE_RUNTIME_CONFIG_SCHEMA, "runtime-config-schema-mismatch")

        service_value = value.get("service")
        _require(isinstance(service_value, Mapping), "runtime-service-object-required")
        service_allowed = {
            "schema", "resource", "authorization_servers", "resource_documentation",
            "service_name", "service_version", "identity_claims", "clock_skew_seconds",
            "transport", "paths",
        }
        service_required = service_allowed.difference({"clock_skew_seconds"})
        _exact_keys(service_value, service_allowed, service_required, "runtime-service")
        _require(
            service_value.get("schema") == "cerebro-control-context-remote-mcp-service-config/v1",
            "runtime-service-schema-mismatch",
        )
        _require(service_value.get("transport") == "STREAMABLE_HTTP", "runtime-transport-mismatch")
        paths = service_value.get("paths")
        _require(
            paths
            == {
                "mcp": MCP_PATH,
                "protected_resource_metadata": PROTECTED_RESOURCE_PATH,
                "health": HEALTH_PATH,
            },
            "runtime-service-paths-mismatch",
        )
        claims = service_value.get("identity_claims")
        _require(isinstance(claims, Mapping), "runtime-identity-claims-object-required")
        _exact_keys(claims, {"tenant", "workspace", "principal"}, {"tenant", "workspace", "principal"}, "runtime-identity-claims")
        issuers = _tuple_of_text(
            service_value.get("authorization_servers"),
            field_name="runtime-authorization-servers",
            allow_empty=False,
        )
        service = RemoteMcpServiceConfig(
            resource=service_value.get("resource"),
            authorization_servers=issuers,
            resource_documentation=service_value.get("resource_documentation"),
            service_name=service_value.get("service_name"),
            service_version=service_value.get("service_version"),
            tenant_claim=claims.get("tenant"),
            workspace_claim=claims.get("workspace"),
            principal_claim=claims.get("principal"),
            clock_skew_seconds=service_value.get("clock_skew_seconds", 60),
        )

        security = value.get("transport_security")
        _require(isinstance(security, Mapping), "runtime-transport-security-object-required")
        _exact_keys(
            security,
            {"allowed_hosts", "allowed_origins", "max_request_body_size"},
            {"allowed_hosts", "allowed_origins", "max_request_body_size"},
            "runtime-transport-security",
        )
        postgres = value.get("postgres")
        _require(isinstance(postgres, Mapping), "runtime-postgres-object-required")
        _exact_keys(
            postgres,
            {"connect_timeout_seconds", "application_name", "runtime_applies_migrations"},
            {"connect_timeout_seconds", "application_name", "runtime_applies_migrations"},
            "runtime-postgres",
        )
        return cls(
            service=service,
            allowed_hosts=_tuple_of_text(
                security.get("allowed_hosts"),
                field_name="runtime-allowed-hosts",
                allow_empty=False,
            ),
            allowed_origins=_tuple_of_text(
                security.get("allowed_origins"),
                field_name="runtime-allowed-origins",
                allow_empty=True,
            ),
            max_request_body_size=security.get("max_request_body_size"),
            postgres_connect_timeout_seconds=postgres.get("connect_timeout_seconds"),
            postgres_application_name=postgres.get("application_name"),
            state_backend=value.get("state_backend"),
            runtime_applies_migrations=postgres.get("runtime_applies_migrations"),
        )

    def public_descriptor(self) -> dict[str, Any]:
        return {
            "schema": REMOTE_RUNTIME_CONFIG_SCHEMA,
            "resource": self.service.resource,
            "service_name": self.service.service_name,
            "service_version": self.service.service_version,
            "transport": "STREAMABLE_HTTP",
            "mcp_path": MCP_PATH,
            "state_backend": self.state_backend,
            "runtime_applies_migrations": self.runtime_applies_migrations,
            "allowed_host_count": len(self.allowed_hosts),
            "allowed_origin_count": len(self.allowed_origins),
            "max_request_body_size": self.max_request_body_size,
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _close_safely(value: Any) -> None:
    try:
        value.close()
    except Exception:
        pass


def _rollback_safely(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:
        pass


def _row_value(row: Any, field_name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(field_name)
    if isinstance(row, (tuple, list)) and len(row) > index:
        return row[index]
    return None


class PostgresStateServiceReadinessProbe:
    """Fail-closed schema and migration readiness probe for the runtime role."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        manifest_path: str | Path = DEFAULT_MANIFEST,
    ) -> None:
        _require(callable(connection_factory), "readiness-connection-factory-required")
        self._connection_factory = connection_factory
        self._expected_migrations = self._load_manifest(manifest_path)

    @staticmethod
    def _load_manifest(manifest_path: str | Path) -> tuple[tuple[str, str, str], ...]:
        path = Path(manifest_path).resolve()
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlContextRemoteRuntimeError("readiness-migration-manifest-unreadable") from exc
        _require(manifest.get("schema") == MIGRATION_MANIFEST_SCHEMA, "readiness-migration-manifest-schema-mismatch")
        _require(manifest.get("runtime_role_may_apply_migrations") is False, "readiness-runtime-migration-authority-prohibited")
        migrations = manifest.get("migrations")
        _require(isinstance(migrations, list) and bool(migrations), "readiness-migrations-required")
        expected: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for entry in migrations:
            _require(isinstance(entry, Mapping), "readiness-migration-entry-invalid")
            migration_id = entry.get("migration_id")
            schema_version = entry.get("schema_version")
            relative_path = entry.get("path")
            checksum = entry.get("checksum_sha256")
            _require(isinstance(migration_id, str) and migration_id not in seen, "readiness-migration-id-invalid")
            _require(isinstance(schema_version, str) and bool(schema_version), "readiness-migration-version-invalid")
            _require(isinstance(relative_path, str) and bool(relative_path), "readiness-migration-path-invalid")
            _require(isinstance(checksum, str) and len(checksum) == 64, "readiness-migration-checksum-invalid")
            migration_file = (path.parent / relative_path).resolve()
            _require(migration_file.parent == path.parent, "readiness-migration-path-escape-prohibited")
            try:
                actual_checksum = _file_sha256(migration_file)
            except OSError as exc:
                raise ControlContextRemoteRuntimeError("readiness-migration-source-unreadable") from exc
            _require(actual_checksum == checksum, "readiness-migration-source-drift")
            expected.append((migration_id, schema_version, checksum))
            seen.add(migration_id)
        return tuple(sorted(expected))

    def _probe(self) -> bool:
        connection = None
        cursor = None
        try:
            connection = self._connection_factory()
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT count(*) AS present
                  FROM unnest(%s::text[]) AS required(relation_name)
                 WHERE to_regclass(required.relation_name) IS NOT NULL
                """,
                (list(REQUIRED_POSTGRES_RELATIONS),),
            )
            relation_row = cursor.fetchone()
            if _row_value(relation_row, "present", 0) != len(REQUIRED_POSTGRES_RELATIONS):
                return False
            migration_ids = [entry[0] for entry in self._expected_migrations]
            cursor.execute(
                """
                SELECT migration_id, schema_version, checksum_sha256
                  FROM cerebro_schema_migrations
                 WHERE migration_id = ANY(%s)
                 ORDER BY migration_id
                """,
                (migration_ids,),
            )
            actual = tuple(
                sorted(
                    (
                        _row_value(row, "migration_id", 0),
                        _row_value(row, "schema_version", 1),
                        _row_value(row, "checksum_sha256", 2),
                    )
                    for row in cursor.fetchall()
                )
            )
            return actual == self._expected_migrations
        finally:
            if connection is not None:
                _rollback_safely(connection)
            if cursor is not None:
                _close_safely(cursor)
            if connection is not None:
                _close_safely(connection)

    def __call__(self) -> bool:
        try:
            return self._probe() is True
        except Exception:
            return False


@dataclass(frozen=True)
class ControlContextRemoteRuntime:
    config: ControlContextRemoteRuntimeConfig
    service: ControlContextRemoteMcpService = field(repr=False)
    state_port: PostgresControlContextStatePort = field(repr=False)
    readiness_probe: PostgresStateServiceReadinessProbe = field(repr=False)
    app: Any = field(repr=False)

    def descriptor(self) -> dict[str, Any]:
        sdk = official_mcp_sdk_runtime()
        return {
            "schema": REMOTE_RUNTIME_SCHEMA,
            "status": "ASSEMBLED_NOT_DEPLOYED",
            "service": self.config.service.service_name,
            "version": self.config.service.service_version,
            "resource": self.config.service.resource,
            "transport": "STREAMABLE_HTTP",
            "mcp_path": MCP_PATH,
            "state_backend": RUNTIME_BACKEND,
            "official_mcp_sdk_version": sdk["version"],
            "runtime_applies_migrations": False,
            "repository_credentials": "NONE",
            "identity_provider_selected": False,
            "deployed": False,
        }


def assemble_postgres_control_context_remote_runtime_from_connection_factory(
    *,
    config: ControlContextRemoteRuntimeConfig,
    connection_factory: Callable[[], Any],
    token_verifier: Any,
    resolution_attestation_verifier: Any,
    clock: Callable[[], float] = time.time,
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> ControlContextRemoteRuntime:
    """Assemble the complete ASGI runtime around an injected PostgreSQL pool/factory."""

    _require(isinstance(config, ControlContextRemoteRuntimeConfig), "runtime-config-required")
    _require(callable(connection_factory), "runtime-connection-factory-required")
    _require(callable(getattr(token_verifier, "verify", None)), "runtime-token-verifier-required")
    _require(
        callable(getattr(resolution_attestation_verifier, "verify", None)),
        "runtime-resolution-attestation-verifier-required",
    )
    _require(callable(clock), "runtime-clock-required")
    state_port = PostgresControlContextStatePort(connection_factory)
    readiness_probe = PostgresStateServiceReadinessProbe(connection_factory, manifest_path)
    tools = ControlContextMcpTools(state_port, resolution_attestation_verifier)
    service = ControlContextRemoteMcpService(
        config=config.service,
        tools=tools,
        token_verifier=token_verifier,
        readiness_probe=readiness_probe,
        clock=clock,
    )
    app = create_streamable_http_app(
        service,
        allowed_hosts=config.allowed_hosts,
        allowed_origins=config.allowed_origins,
        max_request_body_size=config.max_request_body_size,
    )
    return ControlContextRemoteRuntime(
        config=config,
        service=service,
        state_port=state_port,
        readiness_probe=readiness_probe,
        app=app,
    )


def assemble_postgres_control_context_remote_runtime(
    *,
    config: ControlContextRemoteRuntimeConfig,
    postgres_dsn: str,
    token_verifier: Any,
    resolution_attestation_verifier: Any,
    clock: Callable[[], float] = time.time,
    manifest_path: str | Path = DEFAULT_MANIFEST,
) -> ControlContextRemoteRuntime:
    """Assemble from an injected DSN without persisting or exposing it."""

    connection_factory = make_psycopg_connection_factory(
        postgres_dsn,
        connect_timeout_seconds=config.postgres_connect_timeout_seconds,
        application_name=config.postgres_application_name,
    )
    return assemble_postgres_control_context_remote_runtime_from_connection_factory(
        config=config,
        connection_factory=connection_factory,
        token_verifier=token_verifier,
        resolution_attestation_verifier=resolution_attestation_verifier,
        clock=clock,
        manifest_path=manifest_path,
    )
