#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable

import yaml


SOURCE_ROOT = Path(__file__).resolve().parents[2]
for path in (SOURCE_ROOT / "mcp", SOURCE_ROOT / "tooling" / "context"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_context_remote_runtime import (  # noqa: E402
    REQUIRED_POSTGRES_RELATIONS,
    ControlContextRemoteRuntimeConfig,
    ControlContextRemoteRuntimeError,
    PostgresStateServiceReadinessProbe,
    assemble_postgres_control_context_remote_runtime_from_connection_factory,
)
from control_context_remote_service import VerifiedBearerToken  # noqa: E402
from control_context_tools import HmacControlResolutionAttestor  # noqa: E402


NOW = 2_000_000_000.0
RESOURCE = "https://mcp.cerebro.invalid"
ISSUER = "https://auth.cerebro.invalid"
MIGRATION = (
    "0001-control-context-state-service",
    "1.0.0-candidate",
    "6312ab6d5fae58cfa52bfb1d13b1d361ebe73e3a4b37bb34f4875e480afc420d",
)


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


def _config_mapping() -> dict[str, Any]:
    return {
        "schema": "cerebro-control-context-remote-runtime-config/v1",
        "service": {
            "schema": "cerebro-control-context-remote-mcp-service-config/v1",
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "resource_documentation": "https://docs.cerebro.invalid/project-control",
            "service_name": "cerebro-project-control",
            "service_version": "1.0.0-runtime-local-proof",
            "identity_claims": {
                "tenant": "cerebro_tenant",
                "workspace": "cerebro_workspace",
                "principal": "sub",
            },
            "clock_skew_seconds": 30,
            "transport": "STREAMABLE_HTTP",
            "paths": {
                "mcp": "/mcp",
                "protected_resource_metadata": "/.well-known/oauth-protected-resource",
                "health": "/healthz",
            },
        },
        "transport_security": {
            "allowed_hosts": ["testserver"],
            "allowed_origins": [],
            "max_request_body_size": 1048576,
        },
        "state_backend": "POSTGRESQL",
        "postgres": {
            "connect_timeout_seconds": 10,
            "application_name": "cerebro-control-context-state-service",
            "runtime_applies_migrations": False,
        },
    }


class StaticTokenVerifier:
    def verify(self, token: str) -> VerifiedBearerToken:
        claims = {
            "iss": ISSUER,
            "aud": RESOURCE,
            "exp": NOW + 3600,
            "scope": "project_state:read project_state:transition",
            "sub": "OAUTH-PRINCIPAL-1",
            "cerebro_tenant": "TENANT-1",
            "cerebro_workspace": "WORKSPACE-1",
        }
        return VerifiedBearerToken(claims=claims, signature_verified=token == "runtime-token")


class ProbeCursor:
    def __init__(
        self,
        *,
        relation_count: int = len(REQUIRED_POSTGRES_RELATIONS),
        migrations: list[Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.relation_count = relation_count
        self.migrations = copy.deepcopy(migrations if migrations is not None else [MIGRATION])
        self.error = error
        self.statement_count = 0
        self.closed = False
        self._mode = ""

    def execute(self, sql: str, params: Any = None) -> None:
        del params
        if self.error is not None:
            raise self.error
        normalized = " ".join(sql.split()).lower()
        self.statement_count += 1
        if "to_regclass" in normalized:
            self._mode = "relations"
        elif "from cerebro_schema_migrations" in normalized:
            self._mode = "migrations"
        else:
            raise AssertionError("unexpected-readiness-query")

    def fetchone(self) -> Any:
        if self._mode != "relations":
            raise AssertionError("unexpected-readiness-fetchone")
        return {"present": self.relation_count}

    def fetchall(self) -> list[Any]:
        if self._mode != "migrations":
            raise AssertionError("unexpected-readiness-fetchall")
        return copy.deepcopy(self.migrations)

    def close(self) -> None:
        self.closed = True


class ProbeConnection:
    def __init__(self, cursor: ProbeCursor) -> None:
        self.cursor_instance = cursor
        self.rollback_called = False
        self.closed = False

    def cursor(self) -> ProbeCursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rollback_called = True

    def close(self) -> None:
        self.closed = True


class ProbeFactory:
    def __init__(
        self,
        *,
        relation_count: int = len(REQUIRED_POSTGRES_RELATIONS),
        migrations: list[Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.relation_count = relation_count
        self.migrations = migrations
        self.error = error
        self.connections: list[ProbeConnection] = []

    def __call__(self) -> ProbeConnection:
        connection = ProbeConnection(
            ProbeCursor(
                relation_count=self.relation_count,
                migrations=self.migrations,
                error=self.error,
            )
        )
        self.connections.append(connection)
        return connection


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    mapping = _config_mapping()
    config = ControlContextRemoteRuntimeConfig.from_mapping(mapping)
    descriptor = config.public_descriptor()
    check(
        "strict-public-runtime-config-builds-provider-neutral-PostgreSQL-composition",
        descriptor["state_backend"] == "POSTGRESQL"
        and descriptor["transport"] == "STREAMABLE_HTTP"
        and descriptor["runtime_applies_migrations"] is False
        and descriptor["allowed_host_count"] == 1,
    )
    schema = json.loads(
        (SOURCE_ROOT / "mcp/control-context-remote-runtime-config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    serialized_schema = json.dumps(schema, sort_keys=True).lower()
    check(
        "runtime-config-schema-contains-no-credential-or-repository-field",
        all(
            prohibited not in serialized_schema
            for prohibited in ("dsn", "password", "client_secret", "private_key", "repository_credential")
        )
        and schema["properties"]["postgres"]["properties"]["runtime_applies_migrations"]
        == {"const": False},
    )
    with_secret = _config_mapping()
    with_secret["postgres"]["dsn"] = "postgresql://must-not-be-configured-here"
    migration_authority = _config_mapping()
    migration_authority["postgres"]["runtime_applies_migrations"] = True
    wrong_path = _config_mapping()
    wrong_path["service"]["paths"]["mcp"] = "/other"
    check(
        "strict-runtime-config-rejects-secret-fields-migration-authority-and-path-drift",
        _expect_error(
            lambda: ControlContextRemoteRuntimeConfig.from_mapping(with_secret),
            ControlContextRemoteRuntimeError,
        )
        and _expect_error(
            lambda: ControlContextRemoteRuntimeConfig.from_mapping(migration_authority),
            ControlContextRemoteRuntimeError,
        )
        and _expect_error(
            lambda: ControlContextRemoteRuntimeConfig.from_mapping(wrong_path),
            ControlContextRemoteRuntimeError,
        ),
    )

    ready_factory = ProbeFactory()
    probe = PostgresStateServiceReadinessProbe(ready_factory)
    check(
        "readiness-requires-all-relations-and-exact-applied-migration",
        probe() is True
        and len(ready_factory.connections) == 1
        and ready_factory.connections[0].cursor_instance.statement_count == 2,
    )
    check(
        "readiness-query-is-read-only-and-always-rolls-back-and-closes",
        ready_factory.connections[0].rollback_called is True
        and ready_factory.connections[0].cursor_instance.closed is True
        and ready_factory.connections[0].closed is True,
    )
    check(
        "readiness-fails-closed-on-missing-schema-relation",
        PostgresStateServiceReadinessProbe(
            ProbeFactory(relation_count=len(REQUIRED_POSTGRES_RELATIONS) - 1)
        )()
        is False,
    )
    check(
        "readiness-fails-closed-on-migration-ledger-drift",
        PostgresStateServiceReadinessProbe(
            ProbeFactory(migrations=[(MIGRATION[0], MIGRATION[1], "0" * 64)])
        )()
        is False,
    )
    check(
        "readiness-fails-closed-without-leaking-database-errors",
        PostgresStateServiceReadinessProbe(
            ProbeFactory(error=RuntimeError("SECRET-DATABASE-DETAIL"))
        )()
        is False,
    )

    try:
        from starlette.testclient import TestClient
    except Exception:
        return {
            "schema": "cerebro-control-context-remote-runtime-selftest/v1",
            "result": "BLOCK",
            "test_count": len(tests),
            "failures": [item for item in tests if item["result"] != "PASS"],
            "tests": tests,
            "blocker": "official-MCP-SDK-test-runtime-unavailable",
            "evidence_class": "LOCAL_RUNTIME_CORE_PROOF_INCOMPLETE_OFFICIAL_SDK_COMPOSITION",
        }

    runtime_factory = ProbeFactory()
    attestor = HmacControlResolutionAttestor(
        key_id="REMOTE-RUNTIME-SELFTEST",
        secret=b"remote-runtime-selftest-attestation-0001",
    )
    runtime = assemble_postgres_control_context_remote_runtime_from_connection_factory(
        config=config,
        connection_factory=runtime_factory,
        token_verifier=StaticTokenVerifier(),
        resolution_attestation_verifier=attestor,
        clock=lambda: NOW,
    )
    runtime_descriptor = runtime.descriptor()
    serialized_descriptor = json.dumps(runtime_descriptor, sort_keys=True).lower()
    check(
        "complete-runtime-is-assembled-with-official-SDK-and-explicitly-not-deployed",
        runtime_descriptor["status"] == "ASSEMBLED_NOT_DEPLOYED"
        and runtime_descriptor["state_backend"] == "POSTGRESQL"
        and runtime_descriptor["runtime_applies_migrations"] is False
        and runtime_descriptor["deployed"] is False
        and runtime_descriptor["identity_provider_selected"] is False,
    )
    check(
        "runtime-descriptor-cannot-disclose-credentials-or-grant-repository-authority",
        all(
            prohibited not in serialized_descriptor
            for prohibited in ("postgresql://", "runtime-token", "secret", "password", "dsn")
        )
        and runtime_descriptor["repository_credentials"] == "NONE",
    )
    check(
        "runtime-constructor-requires-external-token-and-attestation-verifiers",
        _expect_error(
            lambda: assemble_postgres_control_context_remote_runtime_from_connection_factory(
                config=config,
                connection_factory=runtime_factory,
                token_verifier=object(),
                resolution_attestation_verifier=attestor,
                clock=lambda: NOW,
            ),
            ControlContextRemoteRuntimeError,
        )
        and _expect_error(
            lambda: assemble_postgres_control_context_remote_runtime_from_connection_factory(
                config=config,
                connection_factory=runtime_factory,
                token_verifier=StaticTokenVerifier(),
                resolution_attestation_verifier=object(),
                clock=lambda: NOW,
            ),
            ControlContextRemoteRuntimeError,
        ),
    )

    with TestClient(runtime.app) as client:
        health = client.get("/healthz")
        metadata = client.get("/.well-known/oauth-protected-resource")
        check(
            "assembled-runtime-exposes-ready-health-only-after-schema-and-migration-proof",
            health.status_code == 200
            and health.json()["status"] == "READY"
            and runtime_factory.connections[-1].rollback_called is True,
        )
        check(
            "assembled-runtime-exposes-provider-neutral-protected-resource-metadata",
            metadata.status_code == 200
            and metadata.json()["resource"] == RESOURCE
            and metadata.json()["authorization_servers"] == [ISSUER],
        )

    source_text = (
        SOURCE_ROOT / "mcp/control_context_remote_runtime.py"
    ).read_text(encoding="utf-8")
    requirements = (
        SOURCE_ROOT / "mcp/control-context-mcp-sdk-requirements.txt"
    ).read_text(encoding="utf-8")
    check(
        "runtime-never-applies-migrations-and-declares-the-PostgreSQL-driver-bound",
        "apply_postgres_migrations(" not in source_text
        and "psycopg[binary]>=3.2,<4" in requirements,
    )

    contract = yaml.safe_load(
        (SOURCE_ROOT / "standards/control-context-state-service.yaml").read_text(encoding="utf-8")
    )["control_context_state_service"]["remote_MCP_service_boundary"]
    check(
        "contract-bounds-runtime-assembly-below-operational-activation",
        contract["provider_neutral_runtime_assembly_implemented"] is True
        and contract["local_runtime_assembly_proven"] is True
        and contract["deployed"] is False
        and contract["identity_provider_selected"] is False
        and contract["local_contract_evidence_is_remote_activation"] is False,
    )
    return {
        "schema": "cerebro-control-context-remote-runtime-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
        "evidence_class": "LOCAL_RUNTIME_ASSEMBLY_NOT_LIVE_DEPLOYMENT",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["selftest"], default="selftest")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = selftest()
    except Exception as exc:
        result = {"result": "BLOCK", "error": str(exc)}
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result.get("result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
