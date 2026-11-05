"""Railway-compatible production entrypoint for the Context remote MCP service.

All provider choices and secrets are supplied at runtime.  The governed Source
modules remain unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "mcp"
CONTEXT_DIR = ROOT / "tooling" / "context"
for path in (MCP_DIR, CONTEXT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import jwt  # noqa: E402
from control_context_remote_runtime import (  # noqa: E402
    ControlContextRemoteRuntime,
    ControlContextRemoteRuntimeConfig,
    assemble_postgres_control_context_remote_runtime_from_connection_factory,
)
from control_context_remote_service import LocalRuntimeOAuthBinding, VerifiedBearerToken  # noqa: E402
from control_context_tools import HmacControlResolutionAttestor  # noqa: E402
from control_context_state_postgres import make_psycopg_connection_factory  # noqa: E402
from deployment.pm_admission_wiring import construct_pm_admission_owner, bind_pm_admission_owner  # noqa: E402
from assistant_overlay_owner import PostgresAssistantOverlayDecisionResolver  # noqa: E402
from pm_owner_atomic import PmOwnerAssignment  # noqa: E402
from x9_receiver_host import configuration_from_mapping, X9ReceiverError  # noqa: E402


HOST = re.compile(r"^[A-Za-z0-9.-]+$")


class OAuthBootstrapMiddleware:
    """Return OAuth discovery immediately instead of opening an anonymous SSE stream."""

    def __init__(
        self,
        app: Any,
        *,
        resource_metadata_uri: str,
        protected_resource_metadata: dict[str, Any],
        context_runtime: ControlContextRemoteRuntime,
        context_connection_factory: Callable[[], Any],
    ) -> None:
        self._app = app
        self._resource_metadata_uri = resource_metadata_uri
        self._protected_resource_metadata = protected_resource_metadata
        # Keep the actual provider-owned objects in process for Source host composition.
        self._context_runtime = context_runtime
        self._context_connection_factory = context_connection_factory

    @staticmethod
    async def _json_response(
        send: Any,
        status: int,
        payload: dict[str, Any],
        headers: list[tuple[bytes, bytes]],
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    *headers,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "GET":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            await self._json_response(
                send,
                200,
                self._protected_resource_metadata,
                [],
            )
            return

        if path.rstrip("/") == "/mcp":
            has_authorization = any(
                name.lower() == b"authorization" and bool(value.strip())
                for name, value in scope.get("headers", [])
            )
            if not has_authorization:
                challenge = (
                    'Bearer resource_metadata="'
                    + self._resource_metadata_uri.replace('"', "")
                    + '"'
                ).encode("ascii")
                await self._json_response(
                    send,
                    401,
                    {"error": "authentication_required"},
                    [(b"www-authenticate", challenge)],
                )
                return

        await self._app(scope, receive, send)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required-environment-variable-missing:{name}")
    return value


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _public_resource() -> tuple[str, str]:
    explicit = os.environ.get("CEREBRO_RESOURCE_URL", "").strip().rstrip("/")
    if explicit:
        if not explicit.startswith("https://"):
            raise RuntimeError("CEREBRO_RESOURCE_URL-must-be-https")
        host = explicit.removeprefix("https://")
        if "/" in host or not HOST.fullmatch(host):
            raise RuntimeError("CEREBRO_RESOURCE_URL-must-be-origin")
        return explicit, host.lower()
    host = _required("RAILWAY_PUBLIC_DOMAIN").lower()
    if not HOST.fullmatch(host):
        raise RuntimeError("RAILWAY_PUBLIC_DOMAIN-invalid")
    return f"https://{host}", host


def _runtime_dsn() -> str:
    explicit = os.environ.get("CEREBRO_RUNTIME_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    host = _required("PGHOST")
    port = _required("PGPORT")
    database = _required("PGDATABASE")
    user = _required("CEREBRO_RUNTIME_DB_USER")
    password = _required("CEREBRO_RUNTIME_DB_PASSWORD")
    from urllib.parse import quote

    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def _attestation_secret() -> bytes:
    encoded = _required("CEREBRO_ATTESTATION_SECRET_B64")
    try:
        secret = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception as exc:
        raise RuntimeError("CEREBRO_ATTESTATION_SECRET_B64-invalid") from exc
    if len(secret) < 32:
        raise RuntimeError("CEREBRO_ATTESTATION_SECRET_B64-too-short")
    return secret


def _standing_worker_grant() -> dict[str, Any] | None:
    raw = os.environ.get("CEREBRO_STANDING_WORKER_GRANT_JSON", "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("CEREBRO_STANDING_WORKER_GRANT_JSON-invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("CEREBRO_STANDING_WORKER_GRANT_JSON-object-required")
    return value


def _pm_owner_assignment() -> PmOwnerAssignment | None:
    """Read host-owned nonsecret PM session policy; absent means fail closed."""
    raw = os.environ.get("CEREBRO_PM_OWNER_ASSIGNMENT_JSON", "").strip()
    if not raw:
        return None
    try:
        return PmOwnerAssignment.from_mapping(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("CEREBRO_PM_OWNER_ASSIGNMENT_JSON-invalid") from exc


def _x9_custody_configuration():
    """Read independently assigned nonsecret X9 policy; absent stays closed."""
    raw = os.environ.get("CEREBRO_X9_CUSTODY_JSON", "").strip()
    if not raw:
        return None
    try:
        return configuration_from_mapping(json.loads(raw))
    except (ValueError, TypeError, X9ReceiverError) as exc:
        raise RuntimeError("CEREBRO_X9_CUSTODY_JSON-invalid") from exc


def _pm_owner_d1_publisher_assignment(pm_owner: PmOwnerAssignment | None):
    """BK11: host-owned nonsecret D1 publisher assignment; absent keeps the D1 owner outbox OFF."""
    raw = os.environ.get("CEREBRO_PM_OWNER_D1_PUBLISHER_JSON", "").strip()
    if not raw:
        return None
    if pm_owner is None:
        raise RuntimeError("CEREBRO_PM_OWNER_D1_PUBLISHER_JSON-requires-PM-owner-assignment")
    from pm_owner_d1_outbox import D1PublisherAssignment
    try:
        return D1PublisherAssignment.from_mapping(json.loads(raw))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("CEREBRO_PM_OWNER_D1_PUBLISHER_JSON-invalid") from exc


def _room_return_producer_binding():
    """Protected host-selected exact assignment; absent keeps registration disabled."""
    raw = os.environ.get("CEREBRO_ROOM_RETURN_PRODUCER_JSON", "").strip()
    if not raw:
        return None
    if len(raw.encode("utf-8")) > 8192:
        raise RuntimeError("CEREBRO_ROOM_RETURN_PRODUCER_JSON-too-large")
    try:
        from room_return_d1_outbox import RoomReturnProducerBinding
        return RoomReturnProducerBinding.from_mapping(json.loads(raw))
    except Exception:
        raise RuntimeError("CEREBRO_ROOM_RETURN_PRODUCER_JSON-invalid") from None


def _room_return_producer_bindings():
    """Additive exact immutable set; legacy singleton remains selected unchanged."""
    legacy = _room_return_producer_binding()
    raw = os.environ.get('CEREBRO_ROOM_RETURN_PRODUCERS_JSON', '').strip()
    if not raw:
        return (legacy,) if legacy is not None else (None,)
    try:
        from room_return_d1_outbox import RoomReturnProducerBinding
        if len(raw.encode('utf-8')) > 65536:
            raise ValueError()
        values = json.loads(raw)
        if type(values) is not list or not 1 <= len(values) <= 8:
            raise ValueError()
        bindings = ((legacy,) if legacy is not None else ()) + tuple(
            RoomReturnProducerBinding.from_mapping(v) for v in values)
        if len(bindings) > 8 or len(set(bindings)) != len(bindings):
            raise ValueError()
        return bindings
    except Exception:
        raise RuntimeError('CEREBRO_ROOM_RETURN_PRODUCERS_JSON-invalid') from None


def _room_return_actor_reader(state_port):
    raw = os.environ.get('CEREBRO_ROOM_RETURN_ACTOR_READERS_JSON', '').strip()
    if not raw:
        return None
    try:
        from room_return_actor_reader import RoomReturnActorReadBinding, RoomReturnActorReader
        if len(raw.encode('utf-8')) > 65536:
            raise ValueError()
        values = json.loads(raw)
        if type(values) is not list or not 1 <= len(values) <= 8:
            raise ValueError()
        return RoomReturnActorReader(state_port,tuple(RoomReturnActorReadBinding.from_mapping(v) for v in values))
    except Exception:
        raise RuntimeError('CEREBRO_ROOM_RETURN_ACTOR_READERS_JSON-invalid') from None


def _local_runtime_binding(issuer: str, pm_owner: PmOwnerAssignment | None):
    """Optional nonsecret host policy; absent or disabled never binds a grant."""
    raw = os.environ.get("CEREBRO_LOCAL_RUNTIME_BINDING_JSON", "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
        required = {"issuer", "client_id", "runtime_audience", "tenant_ref", "workspace_ref",
                    "project_ref", "principal_ref", "session_ref", "enabled"}
        if not isinstance(value, dict) or set(value) != required or type(value["enabled"]) is not bool:
            raise ValueError()
        binding = LocalRuntimeOAuthBinding(**value)
        if (binding.issuer != issuer or pm_owner is None
                or (binding.tenant_ref,binding.workspace_ref,binding.project_ref) !=
                   (pm_owner.tenant_ref,pm_owner.workspace_ref,pm_owner.project_ref)):
            raise ValueError()
        return binding
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CEREBRO_LOCAL_RUNTIME_BINDING_JSON-invalid") from exc


class JwksBearerTokenVerifier:
    """Verify an asymmetric JWT against the configured provider JWKS."""

    def __init__(self, *, jwks_url: str, algorithms: list[str]):
        if not jwks_url.startswith("https://"):
            raise RuntimeError("CEREBRO_OAUTH_JWKS_URL-must-be-https")
        if not algorithms or any(value not in {"RS256", "RS384", "RS512", "ES256"} for value in algorithms):
            raise RuntimeError("CEREBRO_OAUTH_ALGORITHMS-invalid")
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
        self._algorithms = algorithms

    def verify(self, token: str) -> VerifiedBearerToken:
        signing_key = self._client.get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=self._algorithms,
            options={
                "verify_aud": False,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_iss": False,
            },
        )
        # OIDC providers commonly serialize the issuer with one trailing slash.
        # Source configuration requires canonical no-trailing-slash HTTPS URLs.
        if isinstance(claims.get("iss"), str):
            claims["iss"] = claims["iss"].rstrip("/")
        return VerifiedBearerToken(claims=claims, signature_verified=True)


def _build_app() -> Any:
    resource, public_host = _public_resource()
    issuer = _required("CEREBRO_OAUTH_ISSUER").rstrip("/")
    jwks_url = os.environ.get(
        "CEREBRO_OAUTH_JWKS_URL",
        issuer + "/.well-known/jwks.json",
    ).strip()
    claim_namespace = os.environ.get(
        "CEREBRO_OAUTH_CLAIM_NAMESPACE",
        resource + "/claims",
    ).strip().rstrip("/")
    config = ControlContextRemoteRuntimeConfig.from_mapping(
        {
            "schema": "cerebro-control-context-remote-runtime-config/v1",
            "service": {
                "schema": "cerebro-control-context-remote-mcp-service-config/v1",
                "resource": resource,
                "authorization_servers": [issuer],
                "resource_documentation": os.environ.get(
                    "CEREBRO_RESOURCE_DOCUMENTATION",
                    resource,
                ).strip().rstrip("/"),
                "service_name": "cerebro-project-control",
                "service_version": "1.0.0",
                "identity_claims": {
                    "tenant": claim_namespace + "/tenant",
                    "workspace": claim_namespace + "/workspace",
                    "principal": "sub",
                },
                "clock_skew_seconds": 60,
                "transport": "STREAMABLE_HTTP",
                "paths": {
                    "mcp": "/mcp",
                    "protected_resource_metadata": "/.well-known/oauth-protected-resource",
                    "health": "/healthz",
                },
            },
            "transport_security": {
                "allowed_hosts": [public_host],
                "allowed_origins": _csv("CEREBRO_ALLOWED_ORIGINS"),
                "max_request_body_size": 1024 * 1024,
            },
            "state_backend": "POSTGRESQL",
            "postgres": {
                "connect_timeout_seconds": 10,
                "application_name": "cerebro-control-context-state-service",
                "runtime_applies_migrations": False,
            },
        }
    )
    pm_owner_assignment = _pm_owner_assignment()
    runtime_binding = _local_runtime_binding(issuer, pm_owner_assignment)
    config = replace(config, service=replace(
        config.service, local_runtime_binding=runtime_binding,
        project_claim=claim_namespace + "/project"))
    token_verifier = JwksBearerTokenVerifier(
        jwks_url=jwks_url,
        algorithms=_csv("CEREBRO_OAUTH_ALGORITHMS") or ["RS256"],
    )
    attestor = HmacControlResolutionAttestor(
        key_id=os.environ.get("CEREBRO_ATTESTATION_KEY_ID", "context-resolution-v1"),
        secret=_attestation_secret(),
    )
    connection_factory = make_psycopg_connection_factory(
        _runtime_dsn(),
        connect_timeout_seconds=config.postgres_connect_timeout_seconds,
        application_name=config.postgres_application_name,
    )
    # Route B: runtime only acknowledges/reads; publication uses privileged operator command.
    from worker_method_owner import PostgresWorkerMethodOwner
    method_owner_raw=os.environ.get('CEREBRO_WORKER_METHOD_OWNER_JSON','').strip()
    method_owner=(PostgresWorkerMethodOwner(connection_factory,json.loads(method_owner_raw),
                  _standing_worker_grant()) if method_owner_raw else None)
    assistant_overlay_resolver = PostgresAssistantOverlayDecisionResolver(connection_factory)
    runtime_notice_connection_factory = (
        make_psycopg_connection_factory(
            _required("CEREBRO_LOCAL_RUNTIME_READ_DATABASE_URL"),
            connect_timeout_seconds=config.postgres_connect_timeout_seconds,
            application_name="cerebro-signalvev-runtime-read")
        if runtime_binding is not None and runtime_binding.enabled else None
    )
    pm_admission_profile = os.environ.get('CEREBRO_PM_ADMISSION_PROFILE', '')
    pm_admission_owner = construct_pm_admission_owner(connection_factory, pm_admission_profile)
    runtime = assemble_postgres_control_context_remote_runtime_from_connection_factory(
        config=config,
        connection_factory=connection_factory,
        token_verifier=token_verifier,
        resolution_attestation_verifier=attestor,
        boot_birth_attestation_issuer=attestor,
        assistant_overlay_control_resolver=assistant_overlay_resolver,
        assistant_overlay_attestation_issuer=attestor,
        standing_worker_grant=_standing_worker_grant(),
        # Absent explicit owner-attributed own-generation mapping stays OFF.
        worker_method_adoption_authorizer=method_owner,
        pm_owner_assignment=pm_owner_assignment,
        x9_configuration=_x9_custody_configuration(),
        pm_owner_d1_publisher_assignment=_pm_owner_d1_publisher_assignment(pm_owner_assignment),
        runtime_notice_connection_factory=runtime_notice_connection_factory,
        pm_profile_verifier=pm_admission_owner,
    )
    bind_pm_admission_owner(runtime, pm_admission_owner, attestor)
    # Future separately selected read-only PM port; absent flag preserves be7 behavior.
    from deployment.pm_public_resolution_wiring import bind_public_pm_resolution_if_enabled
    bind_public_pm_resolution_if_enabled(runtime, attestor,
        enabled=os.environ.get("CEREBRO_PM_PUBLIC_RESOLUTION_ENABLED", ""))
    # Sole installation-owned Assistant constructor input; no caller fields/key.
    trial_owner_raw = os.environ.get("CEREBRO_ASSISTANT_TRIAL_OWNER_JSON", "")
    if trial_owner_raw:
        try:
            import time
            from qualified_constructor_plan import prepare_constructor_objects
            if len(trial_owner_raw.encode("utf-8")) > 8192:
                raise ValueError()
            trial_owner = json.loads(trial_owner_raw)
            fields = {"profile_ref", "supplement_path", "supplement_sha256", "lock_path",
                      "reader_uid", "reader_gid", "writer_contract_ref"}
            if type(trial_owner) is not dict or set(trial_owner) != fields:
                raise ValueError()
            if (trial_owner["supplement_path"] != "/run/cerebro/assistant-trial/build7-one-trial-supplement.json"
                    or trial_owner["lock_path"] != "/run/cerebro/assistant-trial/build7-one-trial-owner.lock"):
                raise ValueError()
            tools = runtime.service._tools
            owner_objects = prepare_constructor_objects(
                connection_factory=connection_factory, real_clock=time.time,
                existing_issuer=tools._assistant_overlay_attestation_issuer,
                existing_verifier=tools._resolution_attestation_verifier,
                current_source_continuity_verifier=tools._assistant_overlay_source_continuity_verifier,
                **trial_owner)
            tools._assistant_overlay_control_resolver = owner_objects[
                "host_constructor_arguments"]["assistant_overlay_control_resolver"]
        except Exception:
            raise RuntimeError("CEREBRO_ASSISTANT_TRIAL_OWNER_JSON_UNQUALIFIED") from None
    bk11_profile = os.environ.get("CEREBRO_BK11_SERVICE_CUSTODY_PROFILE", "")
    if bk11_profile:
        from bk11_service_custody import PROFILE, ContextBk11ServiceHost
        if bk11_profile != PROFILE:
            raise ValueError("BK11_EXPLICIT_SERVICE_CUSTODY_PROFILE_REQUIRED")
        runtime.service._tools.bind_bk11_service_custody(ContextBk11ServiceHost(runtime.state_port))
    from actor_admission_host import ActorAdmissionHost
    from actor_material_provider import ActorMaterialProvider
    # Normal MCP is never the administrative issuer, even when OAuth subjects
    # or signed clients are shared by chats. Private owner entry is separate.
    admission=ActorAdmissionHost(runtime.state_port)
    runtime.service._tools.bind_actor_admission(admission)
    from room_return_d1_outbox import RoomReturnD1Outbox, RoomReturnOutboxHost
    runtime.service._tools.bind_room_return_d1_outbox(RoomReturnOutboxHost(tuple(
        RoomReturnD1Outbox(state_port=runtime.state_port, binding=b) for b in _room_return_producer_bindings()),
        actor_reader=_room_return_actor_reader(runtime.state_port),admission_provider=ActorMaterialProvider(admission)))
    # Auth0 publishes its OAuth issuer with a trailing slash; discovery requires an exact match.
    protected_resource_metadata = {
        "resource": resource,
        "authorization_servers": [issuer + "/"],
        "scopes_supported": ["project_state:read", "project_state:transition"],
        "resource_documentation": config.service.resource_documentation,
    }
    return OAuthBootstrapMiddleware(
        runtime.app,
        resource_metadata_uri=resource + "/.well-known/oauth-protected-resource",
        protected_resource_metadata=protected_resource_metadata,
        context_runtime=runtime,
        context_connection_factory=connection_factory,
    )


app = _build_app()
