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
    tool_definitions,
)


REMOTE_SERVICE_SCHEMA = "cerebro-control-context-remote-mcp-service/v1"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
STATE_SCOPES = frozenset({"project_state:read", "project_state:transition"})
TOOL_REQUIRED_SCOPES = {
    "read_project_control_state": "project_state:read",
    "begin_project_control_event": "project_state:transition",
    "complete_project_control_event": "project_state:transition",
    "create_project_control_instance": "project_state:transition",
    "set_default_project_control_instance": "project_state:transition",
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
class RemoteMcpServiceConfig:
    resource: str
    authorization_servers: tuple[str, ...]
    resource_documentation: str
    service_name: str = "cerebro-project-control"
    service_version: str = "1.0.0"
    tenant_claim: str = "cerebro_tenant"
    workspace_claim: str = "cerebro_workspace"
    principal_claim: str = "sub"
    clock_skew_seconds: int = 60

    def __post_init__(self) -> None:
        resource = _canonical_https_url(self.resource, field="resource", origin_only=True)
        _require(resource == self.resource, "resource-must-be-canonical")
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
        for field in ("service_name", "service_version", "tenant_claim", "workspace_claim", "principal_claim"):
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

    def authenticate(self, headers: Mapping[str, Any], *, required_scope: str) -> VerifiedMcpIdentity:
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
        if self._config.resource not in _audiences(claims):
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
        identity = VerifiedMcpIdentity(
            tenant_ref=_claim_text(claims, self._config.tenant_claim, required_scope=required_scope),
            workspace_ref=_claim_text(claims, self._config.workspace_claim, required_scope=required_scope),
            principal_ref=_claim_text(claims, self._config.principal_claim, required_scope=required_scope),
            scopes=frozenset(scopes.intersection(STATE_SCOPES)),
            token_verified=True,
            consumer_ref="CHATGPT_REMOTE_MCP",
        )
        identity.validate()
        return identity


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
    ) -> dict[str, Any]:
        if tool_name not in TOOL_REQUIRED_SCOPES:
            raise ControlContextToolError(f"unknown-control-context-tool:{tool_name}")
        required_scope = TOOL_REQUIRED_SCOPES[tool_name]
        try:
            identity = self._authenticator.authenticate(headers, required_scope=required_scope)
        except RemoteMcpAuthenticationError as exc:
            return self.authentication_error_result(exc)
        context = McpToolCallContext(
            identity=identity,
            request_meta=self._request_meta(request_meta),
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
