#!/usr/bin/env python3
"""Provider-neutral remote MCP authentication and service boundary.

This module deliberately does not open a listener, select an identity provider,
fetch signing keys, or deploy infrastructure.  The companion
``control_context_mcp_sdk`` module binds the public metadata, tool descriptors,
readiness probe and ``invoke`` method to the official MCP SDK streamable-HTTP
server. Token signature verification is a constructor-bound dependency;
identity and authority never come from tool input.
"""

from __future__ import annotations

import copy
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from control_context_tools import (
    ControlContextMcpTools,
    ControlContextToolError,
    McpToolCallContext,
    VerifiedMcpIdentity,
    _bind_verified_caller_observation,
    _observe_host_transport_metadata,
    tool_definitions,
)


REMOTE_SERVICE_SCHEMA = "cerebro-control-context-remote-mcp-service/v1"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
STATE_SCOPES = frozenset({"project_state:read", "project_state:transition"})
TOOL_REQUIRED_SCOPES = {
    "read_authenticated_binding_basis_v1": "project_state:read",
    "read_standing_worker_policy_observation_v1": "project_state:read",
    "read_authenticated_caller_observation_v1": "project_state:read",
    "close_bk11_service_runtime_v1": "project_state:transition",
    "open_bk11_service_runtime_v1": "project_state:transition",
    "verify_bk11_service_lease_v1": "project_state:read",
    "control_bk11_service_attempt_v1": "project_state:transition",
    "accept_bk11_backend_delivery_v1": "project_state:transition",
    "read_bk11_backend_disposition_v1": "project_state:read",
    "discover_bk11_backend_obligations_v1": "project_state:read",
    "reconcile_bk11_backend_obligation_v1": "project_state:read",
    "create_ready_unbound_boot_generation": "project_state:transition",
    "attach_worker_from_standing_grant": "project_state:transition",
    "adopt_worker_method": "project_state:transition",
    "read_worker_method_evidence": "project_state:read",
    "acknowledge_worker_method_read": "project_state:transition",
    "create_pre_role_generation": "project_state:transition",
    "complete_pre_role_generation": "project_state:transition",
    "attach_role_overlay": "project_state:transition",
    "read_boot_generation_state": "project_state:read",
    "resume_ready_unbound_assistant_overlay": "project_state:transition",
    "resume_ready_unbound_pm_overlay": "project_state:transition",
    "read_project_control_state": "project_state:read",
    "commit_pm_owner_ready": "project_state:transition",
    "read_pm_owner_ready": "project_state:read",
    "read_own_pm_profile_v1": "project_state:read",
    "read_x9_pointer": "project_state:read",
    "observe_signalvev_notice_v1": "project_state:read",
    "transport_resolve_signalvev_notice_v1": "project_state:read",
    "read_local_runtime_binding_v1": "project_state:read",
    "consume_x9_pointer": "project_state:transition",
    "append_x9_pointer": "project_state:transition",
    "prepare_signalvev_notice_v1": "project_state:transition",
    "read_pm_owner_d1_outbox_v1": "project_state:read",
    "settle_pm_owner_d1_outbox_v1": "project_state:transition",
    "register_room_return_v1": "project_state:transition",
    "read_room_return_d1_outbox_v1": "project_state:read",
    "read_accepted_room_return_v1": "project_state:read",
    "manage_actor_admission_v1": "project_state:transition",
    "settle_room_return_d1_outbox_v1": "project_state:transition",
    "resolve_d1_pointer_v1": "project_state:read",
    "resolve_project_manager_control_event": "project_state:transition",
    "begin_project_control_event": "project_state:transition",
    "complete_project_control_event": "project_state:transition",
    "create_project_control_instance": "project_state:transition",
    "set_default_project_control_instance": "project_state:transition",
    "arm_human_t3_break_glass": "project_state:transition",
    "confirm_human_t3_break_glass": "project_state:transition",
}
ALLOWED_REQUEST_META = frozenset({"openai/session", "openai/subject"})
_AUTH_ERROR = re.compile(r"^[a-z_]{1,64}$")


class RemoteMcpServiceError(RuntimeError):
    pass


class RemoteMcpConfigurationError(RemoteMcpServiceError):
    pass


class RemoteMcpAuthenticationError(RemoteMcpServiceError):
    def __init__(self, error: str, description: str, *, required_scope: str):
        if not _AUTH_ERROR.fullmatch(error):
            raise ValueError("OAuth-error-code-invalid")
        super().__init__(description)
        self.error = error
        self.description = description
        self.required_scope = required_scope


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RemoteMcpConfigurationError(message)


def _canonical_https_url(value: Any, *, field: str, origin_only: bool) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{field}-required")
    candidate = value.strip()
    _require(
        all(32 < ord(character) < 127 for character in candidate),
        f"{field}-contains-unsafe-character",
    )
    parsed = urlsplit(candidate)
    _require(parsed.scheme == "https", f"{field}-https-required")
    _require(bool(parsed.netloc) and parsed.hostname is not None, f"{field}-host-required")
    try:
        parsed.port
    except ValueError as exc:
        raise RemoteMcpConfigurationError(f"{field}-port-invalid") from exc
    _require(parsed.username is None and parsed.password is None, f"{field}-userinfo-prohibited")
    _require(not parsed.query and not parsed.fragment, f"{field}-query-or-fragment-prohibited")
    if origin_only:
        _require(parsed.path in {"", "/"}, f"{field}-origin-only")
        canonical = urlunsplit(("https", parsed.netloc, "", "", ""))
    else:
        _require("//" not in parsed.path, f"{field}-path-invalid")
        canonical = urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))
    _require(candidate.rstrip("/") == canonical, f"{field}-must-be-canonical")
    return canonical


@dataclass(frozen=True)
class LocalRuntimeOAuthBinding:
    """Proposed grant, inert unless an owner explicitly enables it at the host."""
    issuer: str
    client_id: str
    tenant_ref: str
    workspace_ref: str
    project_ref: str
    principal_ref: str
    runtime_audience: str
    session_ref: str = ""
    enabled: bool = False


@dataclass(frozen=True)
class RemoteMcpServiceConfig:
    resource: str
    authorization_servers: tuple[str, ...]
    resource_documentation: str
    service_name: str = "cerebro-project-control"
    service_version: str = "1.0.0"
    tenant_claim: str = "cerebro_tenant"
    workspace_claim: str = "cerebro_workspace"
    principal_claim: str = "sub"
    project_claim: str = "cerebro_project"
    clock_skew_seconds: int = 60
    local_runtime_binding: LocalRuntimeOAuthBinding | None = None

    def __post_init__(self) -> None:
        resource = _canonical_https_url(self.resource, field="resource", origin_only=True)
        _require(resource == self.resource, "resource-must-be-canonical")
        if self.local_runtime_binding is not None:
            audience = _canonical_https_url(
                self.local_runtime_binding.runtime_audience,
                field="runtime-audience", origin_only=False)
            _require(
                audience == resource + "/bk07-runtime",
                "runtime-audience-must-match-dedicated-resource",
            )
        _require(
            isinstance(self.authorization_servers, tuple) and bool(self.authorization_servers),
            "authorization-servers-required",
        )
        canonical_issuers = tuple(
            _canonical_https_url(value, field="authorization-server", origin_only=False)
            for value in self.authorization_servers
        )
        _require(canonical_issuers == self.authorization_servers, "authorization-servers-must-be-canonical")
        _require(len(set(canonical_issuers)) == len(canonical_issuers), "authorization-server-duplicate")
        documentation = _canonical_https_url(
            self.resource_documentation,
            field="resource-documentation",
            origin_only=False,
        )
        _require(documentation == self.resource_documentation, "resource-documentation-must-be-canonical")
        for field in ("service_name", "service_version", "tenant_claim", "workspace_claim", "principal_claim", "project_claim"):
            value = getattr(self, field)
            _require(isinstance(value, str) and bool(value.strip()), f"{field}-required")
            _require(value == value.strip(), f"{field}-must-be-trimmed")
        _require(
            isinstance(self.clock_skew_seconds, int)
            and not isinstance(self.clock_skew_seconds, bool)
            and 0 <= self.clock_skew_seconds <= 300,
            "clock-skew-seconds-invalid",
        )

    @property
    def protected_resource_metadata_uri(self) -> str:
        return self.resource + PROTECTED_RESOURCE_PATH

    @property
    def mcp_endpoint(self) -> str:
        return self.resource + MCP_PATH

    @property
    def health_endpoint(self) -> str:
        return self.resource + HEALTH_PATH


@dataclass(frozen=True)
class VerifiedBearerToken:
    """Claims returned only after a trusted verifier validates the signature."""

    claims: Mapping[str, Any]
    signature_verified: bool


class BearerTokenVerifier(Protocol):
    def verify(self, token: str) -> VerifiedBearerToken:
        """Verify cryptographic validity and return untrusted claims for policy checks."""


def _number_claim(value: Any, *, field: str, required_scope: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RemoteMcpAuthenticationError(
            "invalid_token",
            f"The access token has no valid {field} claim.",
            required_scope=required_scope,
        )
    number = float(value)
    if not math.isfinite(number):
        raise RemoteMcpAuthenticationError(
            "invalid_token",
            f"The access token has no valid {field} claim.",
            required_scope=required_scope,
        )
    return number


def _claim_text(claims: Mapping[str, Any], field: str, *, required_scope: str) -> str:
    value = claims.get(field)
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip().encode("utf-8")) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise RemoteMcpAuthenticationError(
            "invalid_token",
            f"The verified access token is missing the required {field} identity claim.",
            required_scope=required_scope,
        )
    return value.strip()


def _claim_scopes(claims: Mapping[str, Any]) -> frozenset[str]:
    values: set[str] = set()
    scope = claims.get("scope")
    if isinstance(scope, str):
        values.update(part for part in scope.split() if part)
    scp = claims.get("scp")
    if isinstance(scp, str):
        values.update(part for part in scp.split() if part)
    elif isinstance(scp, (list, tuple)):
        for part in scp:
            if isinstance(part, str) and part.strip():
                values.add(part.strip())
    return frozenset(values)


def _audiences(claims: Mapping[str, Any]) -> frozenset[str]:
    audience = claims.get("aud")
    if isinstance(audience, str) and audience:
        return frozenset({audience})
    if isinstance(audience, (list, tuple)):
        return frozenset(value for value in audience if isinstance(value, str) and value)
    return frozenset()


def _only_audience(claims: Mapping[str, Any], expected: str) -> bool:
    audience = claims.get("aud")
    return audience == expected or (
        isinstance(audience, (list, tuple))
        and len(audience) == 1
        and audience[0] == expected
    )


def _verified_client_ref(claims):
    # Same verified access token, never request metadata/tool input. Conflicting
    # azp/client_id claims fail this bounded privileged discriminator closed.
    values=[claims[k] for k in ('azp','client_id') if k in claims]
    if not values or any(type(v)is not str or not v or len(v)>512
        or any(ord(c)<33 or ord(c)==127 for c in v) for v in values):return None
    return values[0] if len(set(values))==1 else None

class OAuthBearerAuthenticator:
    """Validate transport credentials and derive the only accepted tool identity."""

    def __init__(
        self,
        *,
        config: RemoteMcpServiceConfig,
        token_verifier: BearerTokenVerifier,
        clock: Callable[[], float] = time.time,
    ) -> None:
        _require(callable(getattr(token_verifier, "verify", None)), "token-verifier-required")
        _require(callable(clock), "clock-required")
        self._config = config
        self._token_verifier = token_verifier
        self._clock = clock

    @staticmethod
    def _bearer(headers: Mapping[str, Any], *, required_scope: str) -> str:
        if not isinstance(headers, Mapping):
            raise RemoteMcpAuthenticationError(
                "invalid_request",
                "Request headers are required.",
                required_scope=required_scope,
            )
        authorization = None
        for name, value in headers.items():
            if isinstance(name, str) and name.lower() == "authorization":
                if authorization is not None:
                    raise RemoteMcpAuthenticationError(
                        "invalid_request",
                        "Multiple authorization headers are not accepted.",
                        required_scope=required_scope,
                    )
                authorization = value
        if not isinstance(authorization, str) or not authorization.strip():
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "A bearer access token is required.",
                required_scope=required_scope,
            )
        parts = authorization.strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or len(parts[1]) > 8192:
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "A valid bearer authorization header is required.",
                required_scope=required_scope,
            )
        return parts[1]

    def authenticate(self, headers: Mapping[str, Any], *, required_scope: str,
                     tool_name: str | None = None) -> VerifiedMcpIdentity:
        return self._authenticate(headers,required_scope=required_scope,tool_name=tool_name,observe=False)[0]

    def authenticate_with_observation(self, headers: Mapping[str, Any], *, required_scope: str,
                                      tool_name: str | None = None):
        return self._authenticate(headers,required_scope=required_scope,tool_name=tool_name,observe=True)

    def _authenticate(self, headers: Mapping[str, Any], *, required_scope: str,
                      tool_name: str | None = None, observe: bool = False):
        token = self._bearer(headers, required_scope=required_scope)
        try:
            verified = self._token_verifier.verify(token)
        except RemoteMcpAuthenticationError:
            raise
        except Exception as exc:
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token could not be verified.",
                required_scope=required_scope,
            ) from exc
        if not isinstance(verified, VerifiedBearerToken) or verified.signature_verified is not True:
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token signature is not verified.",
                required_scope=required_scope,
            )
        claims = verified.claims
        if not isinstance(claims, Mapping):
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The verified access token claims are invalid.",
                required_scope=required_scope,
            )
        issuer = claims.get("iss")
        if issuer not in self._config.authorization_servers:
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token issuer is not authorized for this resource.",
                required_scope=required_scope,
            )
        runtime_tool = tool_name in {"transport_resolve_signalvev_notice_v1", "read_local_runtime_binding_v1"}
        runtime_resolve_tool = tool_name == "transport_resolve_signalvev_notice_v1"
        binding = self._config.local_runtime_binding
        if runtime_tool and (binding is None or (runtime_resolve_tool and binding.enabled is not True)):
            raise RemoteMcpAuthenticationError(
                "insufficient_scope", "The local runtime binding is disabled.",
                required_scope=required_scope)
        audience_matches = (
            _only_audience(claims, binding.runtime_audience)
            if runtime_tool else self._config.resource in _audiences(claims)
        )
        if not audience_matches:
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token audience does not match this resource.",
                required_scope=required_scope,
            )
        try:
            now = float(self._clock())
        except Exception as exc:
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token time validity could not be evaluated.",
                required_scope=required_scope,
            ) from exc
        if not math.isfinite(now):
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token time validity could not be evaluated.",
                required_scope=required_scope,
            )
        skew = self._config.clock_skew_seconds
        expires_at = _number_claim(claims.get("exp"), field="exp", required_scope=required_scope)
        if expires_at <= now - skew:
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token has expired.",
                required_scope=required_scope,
            )
        if (
            "nbf" in claims
            and _number_claim(claims.get("nbf"), field="nbf", required_scope=required_scope) > now + skew
        ):
            raise RemoteMcpAuthenticationError(
                "invalid_token",
                "The access token is not active yet.",
                required_scope=required_scope,
            )
        scopes = _claim_scopes(claims)
        if required_scope not in scopes:
            raise RemoteMcpAuthenticationError(
                "insufficient_scope",
                f"The {required_scope} scope is required.",
                required_scope=required_scope,
            )
        if not runtime_tool and binding is not None and claims.get("azp") == binding.client_id:
            raise RemoteMcpAuthenticationError(
                "insufficient_scope", "The local runtime credential is resolve-only.",
                required_scope=required_scope)
        if runtime_tool:
            if not all(
                isinstance(getattr(binding, key), str) and getattr(binding, key)
                for key in ("issuer", "client_id", "tenant_ref", "workspace_ref", "project_ref", "principal_ref", "session_ref")
            ):
                raise RemoteMcpAuthenticationError(
                    "insufficient_scope", "The local runtime binding is disabled.",
                    required_scope=required_scope)
            if (issuer != binding.issuer or claims.get("azp") != binding.client_id
                    or claims.get("gty") != "client-credentials"
                    or claims.get(self._config.principal_claim) != binding.client_id + "@clients"
                    or claims.get(self._config.tenant_claim) != binding.tenant_ref
                    or claims.get(self._config.workspace_claim) != binding.workspace_ref
                    or claims.get(self._config.principal_claim) != binding.principal_ref
                    or claims.get(self._config.project_claim) != binding.project_ref
                    or scopes != frozenset({"project_state:read"})):
                raise RemoteMcpAuthenticationError(
                    "insufficient_scope", "The scoped local runtime identity is required.",
                    required_scope=required_scope)
            issued_at = _number_claim(claims.get("iat"), field="iat",
                                       required_scope=required_scope)
            if issued_at > now + skew or expires_at - issued_at > 300:
                raise RemoteMcpAuthenticationError(
                    "invalid_token", "A short-lived local runtime token is required.",
                    required_scope=required_scope)
        identity = VerifiedMcpIdentity(
            tenant_ref=_claim_text(claims, self._config.tenant_claim, required_scope=required_scope),
            workspace_ref=_claim_text(claims, self._config.workspace_claim, required_scope=required_scope),
            principal_ref=_claim_text(claims, self._config.principal_claim, required_scope=required_scope),
            scopes=frozenset(scopes.intersection(STATE_SCOPES)),
            token_verified=True,
            authenticated_client_ref=_verified_client_ref(claims),
            consumer_ref="LOCAL_CEREBRO_RUNTIME" if runtime_tool else "CHATGPT_REMOTE_MCP",
        )
        identity.validate()
        observation=None
        if observe:
            present=tuple(key for key in ('azp','client_id') if key in claims)
            status='UNKNOWN_ABSENT';client_ref=None;sources=()
            if present:
                values=[];valid=True
                for key in present:
                    raw=claims[key]
                    try:
                        ok=(type(raw)is str and bool(raw) and raw==raw.strip() and len(raw.encode('utf-8'))<=256
                            and not any(ord(c)<32 or ord(c)==127 for c in raw))
                    except UnicodeError:ok=False
                    if not ok:valid=False
                    else:values.append(raw)
                status='UNKNOWN_INVALID_OR_CONFLICT'
                if valid and len(set(values))==1:
                    status='OBSERVED_SIGNED_CLAIM';client_ref=values[0];sources=present
            observation=_bind_verified_caller_observation(identity,issuer,self._config.resource,
                self._config.service_version,status,client_ref,sources)
        return identity,observation

def _quote_auth_parameter(value: str) -> str:
    safe = value.replace("\\", "\\\\").replace('"', '\\"')
    return safe.replace("\r", " ").replace("\n", " ")


class ControlContextRemoteMcpService:
    """Public contract consumed by an MCP SDK transport binding."""

    def __init__(
        self,
        *,
        config: RemoteMcpServiceConfig,
        tools: ControlContextMcpTools,
        token_verifier: BearerTokenVerifier,
        readiness_probe: Callable[[], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        _require(callable(getattr(tools, "dispatch", None)), "control-context-tools-required")
        _require(callable(readiness_probe), "readiness-probe-required")
        self._config = config
        self._tools = tools
        self._readiness_probe = readiness_probe
        self._authenticator = OAuthBearerAuthenticator(
            config=config,
            token_verifier=token_verifier,
            clock=clock,
        )

    @property
    def config(self) -> RemoteMcpServiceConfig:
        return self._config

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self._config.resource,
            "authorization_servers": list(self._config.authorization_servers),
            "scopes_supported": sorted(STATE_SCOPES),
            "resource_documentation": self._config.resource_documentation,
        }

    def server_descriptor(self) -> dict[str, Any]:
        return {
            "schema": REMOTE_SERVICE_SCHEMA,
            "name": self._config.service_name,
            "version": self._config.service_version,
            "transport": "STREAMABLE_HTTP",
            "mcp_path": MCP_PATH,
            "instructions": (
                "Use begin_project_control_event before project reasoning. Complete the same event only with "
                "an exact MCP control-resolution attestation. State tools never grant repository permission."
            ),
        }

    def list_tools(self) -> list[dict[str, Any]]:
        definitions = tool_definitions()
        names = {item.get("name") for item in definitions}
        if names != set(TOOL_REQUIRED_SCOPES):
            raise ControlContextToolError("remote-MCP-tool-scope-registry-drift")
        for definition in definitions:
            name = definition["name"]
            expected = TOOL_REQUIRED_SCOPES[name]
            schemes = definition.get("securitySchemes")
            if schemes != [{"type": "oauth2", "scopes": [expected]}]:
                raise ControlContextToolError(f"remote-MCP-tool-security-scheme-drift:{name}")
        return copy.deepcopy(definitions)

    def authentication_challenge(
        self,
        *,
        required_scope: str,
        error: str,
        description: str,
    ) -> str:
        if required_scope not in STATE_SCOPES:
            raise RemoteMcpConfigurationError("challenge-scope-invalid")
        if not _AUTH_ERROR.fullmatch(error):
            raise RemoteMcpConfigurationError("challenge-error-invalid")
        values = {
            "resource_metadata": self._config.protected_resource_metadata_uri,
            "scope": required_scope,
            "error": error,
            "error_description": description,
        }
        return "Bearer " + ", ".join(
            f'{name}="{_quote_auth_parameter(value)}"' for name, value in values.items()
        )

    def authentication_error_result(self, error: RemoteMcpAuthenticationError) -> dict[str, Any]:
        challenge = self.authentication_challenge(
            required_scope=error.required_scope,
            error=error.error,
            description=error.description,
        )
        return {
            "content": [{"type": "text", "text": "Authentication or additional authorization is required."}],
            "_meta": {"mcp/www_authenticate": [challenge]},
            "isError": True,
        }

    @staticmethod
    def _request_meta(request_meta: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request_meta, Mapping):
            raise ControlContextToolError("MCP-request-meta-object-required")
        filtered: dict[str, Any] = {}
        for key, value in request_meta.items():
            if not isinstance(key, str) or key not in ALLOWED_REQUEST_META:
                continue
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
                raise ControlContextToolError(f"remote-MCP-request-meta-{key}-invalid")
            filtered[key] = value.strip()
        return filtered

    def invoke(
        self,
        *,
        tool_name: str,
        args: Any,
        headers: Mapping[str, Any],
        request_meta: Mapping[str, Any],
        host_transport_observation=None,
    ) -> dict[str, Any]:
        if tool_name not in TOOL_REQUIRED_SCOPES:
            raise ControlContextToolError(f"unknown-control-context-tool:{tool_name}")
        required_scope = TOOL_REQUIRED_SCOPES[tool_name]
        try:
            observation=None;transport=None
            if tool_name in ('read_authenticated_caller_observation_v1', 'read_authenticated_binding_basis_v1', 'read_standing_worker_policy_observation_v1'):
                identity,observation=self._authenticator.authenticate_with_observation(headers,required_scope=required_scope,tool_name=tool_name)
                transport=_observe_host_transport_metadata(request_meta) if host_transport_observation is None else host_transport_observation
            else:
                identity = self._authenticator.authenticate(headers, required_scope=required_scope,
                                                            tool_name=tool_name)
        except RemoteMcpAuthenticationError as exc:
            return self.authentication_error_result(exc)
        context = McpToolCallContext(
            identity=identity,
            request_meta={} if observation is not None else self._request_meta(request_meta),
            private_headers=None if observation is not None else headers,
            verified_caller_observation=observation,
            verified_transport_observation=transport,
        )
        return self._tools.dispatch(tool_name, args, context)

    def readiness(self) -> dict[str, Any]:
        ready = False
        try:
            ready = self._readiness_probe() is True
        except Exception:
            ready = False
        return {
            "schema": REMOTE_SERVICE_SCHEMA,
            "status": "READY" if ready else "NOT_READY",
            "service": self._config.service_name,
            "version": self._config.service_version,
            "transport": "STREAMABLE_HTTP",
            "mcp_path": MCP_PATH,
            "state_service": "READY" if ready else "NOT_READY",
        }
