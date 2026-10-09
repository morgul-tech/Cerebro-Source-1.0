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
_MIGRATION_MANIFEST = json.loads(
    (SOURCE_ROOT / "tooling/context/control_context_postgres_migrations.json").read_text(encoding="utf-8")
)
MIGRATIONS = tuple(
    (entry["migration_id"], entry["schema_version"], entry["checksum_sha256"])
    for entry in _MIGRATION_MANIFEST["migrations"]
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


class RuntimePmPrepublicationBasisReader:
    def read_prepublication_basis(self, **_: Any) -> dict[str, Any]:
        return {}


class RuntimePmDispositionPublisherPort:
    def publish_and_readback(self, **_: Any) -> dict[str, Any]:
        return {}


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
        self.migrations = copy.deepcopy(migrations if migrations is not None else list(MIGRATIONS))
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
        and all(migration_id != "0006-project-commissioning-session" for migration_id, _, _ in MIGRATIONS)
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
            ProbeFactory(migrations=[MIGRATIONS[0], (MIGRATIONS[1][0], MIGRATIONS[1][1], "0" * 64)])
        )()
        is False,
    )
    check("HG04-readiness-requires-0003-applied-migration",
          any(m[0] == "0003-human-t3-break-glass" for m in MIGRATIONS)
          and PostgresStateServiceReadinessProbe(ProbeFactory(migrations=[m for m in MIGRATIONS if m[0] != "0003-human-t3-break-glass"]))() is False)
    check("HG04-readiness-requires-three-custody-relations",
          {"cerebro_human_t3_break_glass_heads", "cerebro_human_t3_break_glass_revisions", "cerebro_human_t3_break_glass_receipts"} <= set(REQUIRED_POSTGRES_RELATIONS)
          and PostgresStateServiceReadinessProbe(ProbeFactory(relation_count=len(REQUIRED_POSTGRES_RELATIONS)-3))() is False)
    drifted = list(MIGRATIONS)
    drifted = [(m[0], m[1], "0" * 64) if m[0] == "0003-human-t3-break-glass" else m for m in drifted]
    check("HG04-readiness-rejects-0003-checksum-drift", PostgresStateServiceReadinessProbe(ProbeFactory(migrations=drifted))() is False)
    check(
        "readiness-fails-closed-without-leaking-database-errors",
        PostgresStateServiceReadinessProbe(
            ProbeFactory(error=RuntimeError("SECRET-DATABASE-DETAIL"))
        )()
        is False,
    )

    runtime_source = (
        SOURCE_ROOT / "mcp/control_context_remote_runtime.py"
    ).read_text(encoding="utf-8")
    check("P1074-readiness-requires-0004-and-succession-custody-relations",
          any(m[0] == "0004-principal-succession-permit" for m in MIGRATIONS)
          and PostgresStateServiceReadinessProbe(ProbeFactory(migrations=[m for m in MIGRATIONS if m[0] != "0004-principal-succession-permit"]))() is False
          and {"cerebro_principal_succession_permit_heads", "cerebro_principal_succession_permit_revisions", "cerebro_principal_succession_permit_receipts"} <= set(REQUIRED_POSTGRES_RELATIONS))
    check("P1074-readiness-rejects-0004-checksum-drift",
          PostgresStateServiceReadinessProbe(ProbeFactory(migrations=[(m[0],m[1],"0"*64) if m[0] == "0004-principal-succession-permit" else m for m in MIGRATIONS]))() is False)
    check("P1074-production-composition-trust-seams-source-contract",
          all(token in runtime_source for token in ("PrincipalSuccessionPermitProvider(",
              "runtime-ambiguous-succession-reader-prohibited", "runtime-succession-PM-profile-verifier-required",
              "principal_succession_inputs_reader=principal_succession_inputs_reader",
              "principal_succession_mcp_authorizer=principal_succession_mcp_authorizer")))
    check(
        "P669-runtime-composition-exposes-constructor-bound-permit-capabilities",
        all(token in runtime_source for token in (
            "principal_succession_reader: Any | None",
            "machine_diary_effect_verifier: Any | None",
            '"principal_succession_reader_bound"',
            '"machine_diary_effect_verifier_bound"',
            "principal_succession_reader=principal_succession_reader",
            "machine_diary_effect_verifier=machine_diary_effect_verifier",
        )),
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
    check("HG04-default-runtime-unbound-not-activated", runtime_descriptor["human_t3_host_bound"] is False
          and runtime_descriptor["human_t3_remote_activation"] == "NOT_PROVEN")
    check("HG04-capability-alone-cannot-bind-runtime", _expect_error(
          lambda: assemble_postgres_control_context_remote_runtime_from_connection_factory(config=config,
                  connection_factory=runtime_factory, token_verifier=StaticTokenVerifier(), resolution_attestation_verifier=attestor,
                  human_t3_effect_capability=object(), clock=lambda: NOW), ControlContextRemoteRuntimeError))
    from control_resolution_host_validation import human_t3_fixture
    _, _, reader, effect, _, _ = human_t3_fixture()
    bound_t3_runtime = assemble_postgres_control_context_remote_runtime_from_connection_factory(config=config,
                       connection_factory=runtime_factory, token_verifier=StaticTokenVerifier(), resolution_attestation_verifier=attestor,
                       human_t3_current_reader=reader, human_t3_effect_capability=effect, clock=lambda: NOW)
    check("HG04-runtime-constructor-binds-local-host-without-remote-claim", bound_t3_runtime.descriptor()["human_t3_host_bound"] is True
          and bound_t3_runtime.descriptor()["human_t3_remote_activation"] == "NOT_PROVEN" and effect.calls == [])
    serialized_descriptor = json.dumps(runtime_descriptor, sort_keys=True).lower()
    check(
        "complete-runtime-is-assembled-with-official-SDK-and-explicitly-not-deployed",
        runtime_descriptor["status"] == "ASSEMBLED_NOT_DEPLOYED"
        and runtime_descriptor["state_backend"] == "POSTGRESQL"
        and runtime_descriptor["runtime_applies_migrations"] is False
        and runtime_descriptor["deployed"] is False
        and runtime_descriptor["identity_provider_selected"] is False
        and runtime_descriptor["pm_lifecycle_verifier_bound"] is False,
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


    class RuntimePmProfileVerifier:
        def verify(self, *, binding: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
            return {
                "schema": "cerebro-project-manager-profile-verification/v1",
                "result": "PASS",
                "profile": "PROJECT_MANAGER",
                "session_ref": session.get("session_ref"),
                "binding_fingerprint": "a" * 64,
                "verifier_ref": "REMOTE-RUNTIME-PM-SELFTEST",
            }

    class RuntimePersistenceVerifier:
        def verify(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
            return {"result": "PASS", "receipt": receipt}

    class RuntimeCapabilityResolver:
        def is_available(self, **_: Any) -> bool:
            return False

        def executor(self, **_: Any) -> Any:
            raise RuntimeError("not-used")

    class RuntimeSuccessionProvider:
        def read_principal_succession_permit(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("not-used-by-composition-test")

        def verify_machine_diary_effect(self, **_: Any) -> dict[str, Any]:
            return {"result": "PASS"}

    succession_provider = RuntimeSuccessionProvider()
    from control_context_tools_validation import principal_succession_provider_fixture
    from control_context_principal_succession_provider import PrincipalSuccessionPermitProvider
    from control_context_state_port import StateBindingError
    _,_,succession_inputs,succession_authorizer,diary_verifier,_ = principal_succession_provider_fixture()
    custody_args = dict(config=config, connection_factory=ProbeFactory(),
        token_verifier=StaticTokenVerifier(), resolution_attestation_verifier=attestor,
        pm_profile_verifier=RuntimePmProfileVerifier(), machine_diary_effect_verifier=diary_verifier,
        principal_succession_inputs_reader=succession_inputs,
        principal_succession_mcp_authorizer=succession_authorizer, clock=lambda:NOW)
    custody_runtime=assemble_postgres_control_context_remote_runtime_from_connection_factory(**custody_args)
    check("P1074-real-production-custody-provider-constructor-bound-same-state-port",
          isinstance(custody_runtime.principal_succession_reader,PrincipalSuccessionPermitProvider)
          and custody_runtime.principal_succession_reader.state_port is custody_runtime.state_port
          and custody_runtime.pm_lifecycle_verifier is not None
          and custody_runtime.machine_diary_effect_verifier is diary_verifier
          and custody_runtime.descriptor()["principal_succession_custody_provider_bound"] is True
          and custody_runtime.descriptor()["principal_succession_live_custody_proven"] is False)
    check("P1074-production-custody-composition-requires-PM-verifier",
          _expect_error(lambda:assemble_postgres_control_context_remote_runtime_from_connection_factory(
              **{**custody_args,"pm_profile_verifier":None}),ControlContextRemoteRuntimeError))
    check("P1074-production-custody-composition-requires-independent-diary-verifier",
          _expect_error(lambda:assemble_postgres_control_context_remote_runtime_from_connection_factory(
              **{**custody_args,"machine_diary_effect_verifier":None}),StateBindingError))
    check("P1074-production-custody-composition-rejects-ambiguous-reader",
          _expect_error(lambda:assemble_postgres_control_context_remote_runtime_from_connection_factory(
              **{**custody_args,"principal_succession_reader":succession_provider}),ControlContextRemoteRuntimeError))
    lifecycle_runtime_factory = ProbeFactory()
    runtime_pm_basis_reader = RuntimePmPrepublicationBasisReader()
    runtime_pm_publisher_port = RuntimePmDispositionPublisherPort()
    lifecycle_runtime = assemble_postgres_control_context_remote_runtime_from_connection_factory(
        config=config,
        connection_factory=lifecycle_runtime_factory,
        token_verifier=StaticTokenVerifier(),
        resolution_attestation_verifier=attestor,
        pm_profile_verifier=RuntimePmProfileVerifier(),
        pm_prepublication_basis_reader=runtime_pm_basis_reader,
        pm_disposition_publisher_port=runtime_pm_publisher_port,
        principal_succession_reader=succession_provider,
        machine_diary_effect_verifier=succession_provider,
        clock=lambda: NOW,
    )
    check(
        "P669-runtime-binds-principal-succession-provider",
        lifecycle_runtime.principal_succession_reader is succession_provider
        and lifecycle_runtime.machine_diary_effect_verifier is succession_provider
        and lifecycle_runtime.descriptor()["principal_succession_reader_bound"] is True
        and lifecycle_runtime.descriptor()["machine_diary_effect_verifier_bound"] is True,
    )
    check(
        "P554-runtime-binds-combined-pm-lifecycle-verifier",
        lifecycle_runtime.pm_lifecycle_verifier is not None
        and callable(getattr(lifecycle_runtime.pm_lifecycle_verifier, "verify", None))
        and callable(getattr(lifecycle_runtime.pm_lifecycle_verifier, "verify_lifecycle_effect", None))
        and lifecycle_runtime.descriptor()["pm_lifecycle_verifier_bound"] is True,
    )
    bound_host = lifecycle_runtime.bind_control_resolution_host(
        persistence_verifier=RuntimePersistenceVerifier(),
        capability_resolver=RuntimeCapabilityResolver(),
    )
    check(
        "P554-runtime-binds-same-verifier-into-normal-host",
        getattr(bound_host, "_pm_profile_verifier", None)
        is lifecycle_runtime.pm_lifecycle_verifier,
    )
    check(
        "ROMA-I41-runtime-composes-trusted-A7-basis-and-publisher-into-normal-host",
        lifecycle_runtime.descriptor()["pm_durable_disposition_guard_bound"] is True
        and getattr(bound_host, "_pm_disposition_publisher", None) is not None
        and getattr(bound_host._pm_disposition_publisher, "_basis_reader", None)
            is runtime_pm_basis_reader
        and getattr(bound_host._pm_disposition_publisher, "_publisher_port", None)
            is runtime_pm_publisher_port,
    )
    class RomAReader:
        def __init__(self):
            self.calls = []
        def read_current(self, content_ref, kind, selector):
            self.calls.append((content_ref, kind, selector))
            raise AssertionError("no-provider-read-during-construction")
    class RomASender:
        def __init__(self):
            self.calls = []
        def send_selected(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("no-send-during-construction")
    rom_reader, rom_sender = RomAReader(), RomASender()
    rom_runtime = assemble_postgres_control_context_remote_runtime_from_connection_factory(
        config=config, connection_factory=ProbeFactory(),
        token_verifier=StaticTokenVerifier(), resolution_attestation_verifier=attestor,
        pm_profile_verifier=RuntimePmProfileVerifier(),
        rom_a_bounded_content_provider=rom_reader,
        rom_a_selected_dispatcher=rom_sender, clock=lambda: NOW,
    )
    rom_host = rom_runtime.bind_control_resolution_host(
        persistence_verifier=RuntimePersistenceVerifier(),
        capability_resolver=RuntimeCapabilityResolver(),
    )
    check("ROMA-normal-runtime-binds-server-owned-paired-ports-with-no-effect",
          rom_runtime.descriptor()["rom_a_normal_return_ports_bound"] is True
          and rom_runtime.descriptor()["rom_a_recipient_use_proven"] is False
          and rom_host._bounded_content_provider is rom_reader
          and rom_host._rom_a_selected_dispatcher is rom_sender
          and rom_reader.calls == [] and rom_sender.calls == [])
    # Trusted constructor configuration only; no caller-selected output route.
    from unittest.mock import patch
    import control_context_remote_runtime as runtime_module
    from rom_a_google_docs_ports import FixedGoogleDocsNamedRangeReader
    docs_args = dict(config=config, connection_factory=ProbeFactory(),
                     token_verifier=StaticTokenVerifier(), resolution_attestation_verifier=attestor,
                     pm_profile_verifier=RuntimePmProfileVerifier(),
                     rom_a_bounded_content_provider=rom_reader,
                     rom_a_selected_dispatcher=rom_sender, clock=lambda: NOW)
    fixed_output = SOURCE_ROOT / "fixture-docs-output-not-created"
    docs_runtime = assemble_postgres_control_context_remote_runtime_from_connection_factory(
        **docs_args, rom_a_docs_out_dir=fixed_output)
    docs_host = docs_runtime.bind_control_resolution_host(
        persistence_verifier=RuntimePersistenceVerifier(),
        capability_resolver=RuntimeCapabilityResolver())
    check("ROMA-docs-fixed-output-reaches-normal-host-without-effect",
          docs_runtime.rom_a_docs_out_dir is fixed_output
          and docs_host._rom_a_docs_out_dir is fixed_output
          and rom_reader.calls == [] and rom_sender.calls == [])
    dsn_args = {key: value for key, value in docs_args.items() if key != "connection_factory"}
    with patch.object(runtime_module, "make_psycopg_connection_factory",
                      return_value=ProbeFactory()):
        dsn_runtime = runtime_module.assemble_postgres_control_context_remote_runtime(
            **dsn_args, postgres_dsn="fixture:never-connected", rom_a_docs_out_dir=fixed_output)
    dsn_host = dsn_runtime.bind_control_resolution_host(
        persistence_verifier=RuntimePersistenceVerifier(),
        capability_resolver=RuntimeCapabilityResolver())
    check("ROMA-docs-dsn-factory-forwards-same-fixed-output",
          dsn_host._rom_a_docs_out_dir is fixed_output
          and rom_reader.calls == [] and rom_sender.calls == [])
    # A typed inert reader reaches the None-output fence before any provider call.
    old_reader = rom_host._bounded_content_provider
    rom_host._bounded_content_provider = object.__new__(FixedGoogleDocsNamedRangeReader)
    try:
        rom_host.dispatch_rom_a_docs_selection(
            target={}, recipient_ref="fixture:A1", task_ref="fixture:task",
            task_revision="fixture:r1")
        none_closed = False
    except ValueError as exc:
        none_closed = str(exc) == "host-rom-a-docs-fixed-output-unbound"
    finally:
        rom_host._bounded_content_provider = old_reader
    check("ROMA-docs-default-None-fails-closed-before-provider-or-send",
          rom_runtime.rom_a_docs_out_dir is None
          and rom_host._rom_a_docs_out_dir is None and none_closed
          and rom_reader.calls == [] and rom_sender.calls == [])
    check("ROMA-docs-relative-or-string-output-rejected",
          all(_expect_error(lambda bad=bad:
              assemble_postgres_control_context_remote_runtime_from_connection_factory(
                  **docs_args, rom_a_docs_out_dir=bad), ControlContextRemoteRuntimeError)
              for bad in (Path("relative-output"), str(fixed_output))))
    check("ROMA-docs-caller-output-injection-rejected",
          _expect_error(lambda: docs_host.dispatch_rom_a_docs_selection(
              target={}, recipient_ref="fixture:A1", task_ref="fixture:task",
              task_revision="fixture:r1", out_dir=str(fixed_output)), TypeError)
          and _expect_error(lambda: docs_host.dispatch_rom_a_docs_selection(
              target={}, recipient_ref="fixture:A1", task_ref="fixture:task",
              task_revision="fixture:r1", rom_a_docs_out_dir=fixed_output), TypeError)
          and _expect_error(lambda: docs_runtime.bind_control_resolution_host(
              persistence_verifier=RuntimePersistenceVerifier(),
              capability_resolver=RuntimeCapabilityResolver(),
              rom_a_docs_out_dir=fixed_output), TypeError)
          and rom_reader.calls == [] and rom_sender.calls == [])
    check("ROMA-normal-runtime-refuses-one-sided-or-invalid-ports",
          _expect_error(lambda: assemble_postgres_control_context_remote_runtime_from_connection_factory(
              config=config, connection_factory=ProbeFactory(), token_verifier=StaticTokenVerifier(),
              resolution_attestation_verifier=attestor,
              rom_a_bounded_content_provider=rom_reader, clock=lambda: NOW), ControlContextRemoteRuntimeError)
          and _expect_error(lambda: assemble_postgres_control_context_remote_runtime_from_connection_factory(
              config=config, connection_factory=ProbeFactory(), token_verifier=StaticTokenVerifier(),
              resolution_attestation_verifier=attestor,
              rom_a_bounded_content_provider=object(), rom_a_selected_dispatcher=rom_sender,
              clock=lambda: NOW), ControlContextRemoteRuntimeError))
    check(
        "ROMA-I41-runtime-rejects-one-sided-durable-disposition-port-binding",
        _expect_error(
            lambda: assemble_postgres_control_context_remote_runtime_from_connection_factory(
                config=config,
                connection_factory=ProbeFactory(),
                token_verifier=StaticTokenVerifier(),
                resolution_attestation_verifier=attestor,
                pm_profile_verifier=RuntimePmProfileVerifier(),
                pm_prepublication_basis_reader=RuntimePmPrepublicationBasisReader(),
                clock=lambda: NOW,
            ),
            ControlContextRemoteRuntimeError,
        ),
    )
    check(
        "P554-readiness-requires-existing-actor-shadow-relations",
        "cerebro_actor_generation_shadow_heads" in REQUIRED_POSTGRES_RELATIONS
        and "cerebro_actor_generation_shadow_revisions" in REQUIRED_POSTGRES_RELATIONS,
    )
    check(
        "P1099-readiness-requires-pre-role-generation-relations",
        "cerebro_pre_role_generation_heads" in REQUIRED_POSTGRES_RELATIONS
        and "cerebro_pre_role_generation_revisions" in REQUIRED_POSTGRES_RELATIONS,
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
