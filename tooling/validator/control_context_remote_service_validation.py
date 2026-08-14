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

from control_context_remote_service import (  # noqa: E402
    HEALTH_PATH,
    MCP_PATH,
    PROTECTED_RESOURCE_PATH,
    STATE_SCOPES,
    TOOL_REQUIRED_SCOPES,
    ControlContextRemoteMcpService,
    RemoteMcpConfigurationError,
    RemoteMcpServiceConfig,
    VerifiedBearerToken,
)
from control_context_registry import DIRECTIVE_SCHEMA  # noqa: E402
from control_context_state_port import InMemoryControlContextStatePort  # noqa: E402
from control_context_tools import (  # noqa: E402
    ControlContextMcpTools,
    ControlContextToolAuthorizationError,
    ControlContextToolError,
    HmacControlResolutionAttestor,
    McpToolCallContext,
    VerifiedMcpIdentity,
)


NOW = 2_000_000_000.0
RESOURCE = "https://mcp.cerebro.invalid"
ISSUER = "https://auth.cerebro.invalid"


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


def _claims(*, scopes: str = "project_state:read project_state:transition") -> dict[str, Any]:
    return {
        "iss": ISSUER,
        "aud": RESOURCE,
        "exp": NOW + 3600,
        "nbf": NOW - 60,
        "scope": scopes,
        "sub": "OAUTH-PRINCIPAL-1",
        "cerebro_tenant": "TENANT-1",
        "cerebro_workspace": "WORKSPACE-1",
    }


class StaticTokenVerifier:
    def __init__(self, values: dict[str, VerifiedBearerToken]):
        self._values = values

    def verify(self, token: str) -> VerifiedBearerToken:
        if token == "verifier-explodes":
            raise RuntimeError("SECRET-VERIFIER-INTERNAL-MUST-NOT-LEAK")
        return self._values.get(token, VerifiedBearerToken(claims={}, signature_verified=False))


def _identity() -> VerifiedMcpIdentity:
    return VerifiedMcpIdentity(
        tenant_ref="TENANT-1",
        workspace_ref="WORKSPACE-1",
        principal_ref="OAUTH-PRINCIPAL-1",
        scopes=frozenset(STATE_SCOPES),
        token_verified=True,
    )


def _root() -> dict[str, Any]:
    return {
        "context_id": "CTX-ROOT",
        "human_label": "Hovedspor",
        "objective_ref": "OBJ-TOTAL-MCP-REVISION",
        "scope_ref": "SCOPE-TOTAL-MCP-REVISION",
        "basis_refs": ["REMOTE-MCP-LOCAL-CONTRACT"],
        "project_basis_ref": "PROJECT-BASIS-TOTAL-MCP-REVISION-V1",
        "quality_trace_ref": "QUALITY-DEEP-V1",
        "completion_criteria_refs": ["V1-ACCEPTANCE"],
    }


def _auth_error(value: dict[str, Any], *, error: str, scope: str) -> bool:
    challenge = value.get("_meta", {}).get("mcp/www_authenticate", [""])[0]
    return (
        value.get("isError") is True
        and challenge.startswith("Bearer ")
        and f'error="{error}"' in challenge
        and f'scope="{scope}"' in challenge
        and f'resource_metadata="{RESOURCE}{PROTECTED_RESOURCE_PATH}"' in challenge
    )


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    config = RemoteMcpServiceConfig(
        resource=RESOURCE,
        authorization_servers=(ISSUER,),
        resource_documentation="https://docs.cerebro.invalid/project-control",
        service_version="1.0.0-local-contract",
        clock_skew_seconds=30,
    )
    bad_issuer = _claims()
    bad_issuer["iss"] = "https://attacker.invalid"
    bad_audience = _claims()
    bad_audience["aud"] = "https://other.cerebro.invalid"
    expired = _claims()
    expired["exp"] = NOW - 31
    future = _claims()
    future["nbf"] = NOW + 31
    missing_identity = _claims()
    missing_identity.pop("cerebro_tenant")
    boolean_expiry = _claims()
    boolean_expiry["exp"] = True
    audience_list = _claims(scopes="project_state:read")
    audience_list["aud"] = ["https://unrelated.invalid", RESOURCE]
    scp_list = _claims(scopes="")
    scp_list.pop("scope")
    scp_list["scp"] = ["project_state:read"]
    tokens = {
        "transition-token": VerifiedBearerToken(claims=_claims(), signature_verified=True),
        "read-token": VerifiedBearerToken(
            claims=_claims(scopes="project_state:read"),
            signature_verified=True,
        ),
        "unsigned-token": VerifiedBearerToken(claims=_claims(), signature_verified=False),
        "bad-issuer-token": VerifiedBearerToken(claims=bad_issuer, signature_verified=True),
        "bad-audience-token": VerifiedBearerToken(claims=bad_audience, signature_verified=True),
        "expired-token": VerifiedBearerToken(claims=expired, signature_verified=True),
        "future-token": VerifiedBearerToken(claims=future, signature_verified=True),
        "missing-identity-token": VerifiedBearerToken(claims=missing_identity, signature_verified=True),
        "boolean-expiry-token": VerifiedBearerToken(claims=boolean_expiry, signature_verified=True),
        "audience-list-token": VerifiedBearerToken(claims=audience_list, signature_verified=True),
        "scp-list-token": VerifiedBearerToken(claims=scp_list, signature_verified=True),
    }
    verifier = StaticTokenVerifier(tokens)
    port = InMemoryControlContextStatePort()
    attestor = HmacControlResolutionAttestor(
        key_id="REMOTE-SERVICE-SELFTEST",
        secret=b"remote-service-selftest-secret-key-0001",
    )
    tools = ControlContextMcpTools(port, attestor)
    ready = [False]
    service = ControlContextRemoteMcpService(
        config=config,
        tools=tools,
        token_verifier=verifier,
        readiness_probe=lambda: ready[0],
        clock=lambda: NOW,
    )

    metadata = service.protected_resource_metadata()
    check(
        "RFC9728-protected-resource-metadata-is-provider-neutral-and-exact",
        metadata == {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "scopes_supported": sorted(STATE_SCOPES),
            "resource_documentation": "https://docs.cerebro.invalid/project-control",
        },
    )
    descriptor = service.server_descriptor()
    check(
        "remote-service-declares-stable-streamable-HTTP-boundary",
        descriptor["transport"] == "STREAMABLE_HTTP"
        and descriptor["mcp_path"] == MCP_PATH
        and len(descriptor["instructions"].encode("utf-8")) <= 512,
    )
    definitions = service.list_tools()
    check(
        "every-private-tool-has-an-explicit-minimum-OAuth-scope",
        len(definitions) == len(TOOL_REQUIRED_SCOPES)
        and all(
            item.get("securitySchemes")
            == [{"type": "oauth2", "scopes": [TOOL_REQUIRED_SCOPES[item["name"]]]}]
            for item in definitions
        ),
    )
    check(
        "tool-metadata-preserves-accurate-safety-annotations-and-no-repository-surface",
        all(
            item.get("annotations", {}).get("destructiveHint") is False
            and item.get("annotations", {}).get("openWorldHint") is False
            for item in definitions
        )
        and "repository" not in json.dumps(
            [
                {
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "inputSchema": item.get("inputSchema"),
                }
                for item in definitions
            ],
            sort_keys=True,
        ).lower()
        and all(
            item.get("outputSchema", {})
            .get("properties", {})
            .get("repository_permission_required")
            == {"const": False}
            for item in definitions
        ),
    )
    schema = json.loads(
        (SOURCE_ROOT / "mcp/control-context-remote-service-config.schema.json").read_text(encoding="utf-8")
    )
    check(
        "deployment-config-schema-locks-HTTPS-and-canonical-paths-without-secrets",
        schema["properties"]["transport"]["const"] == "STREAMABLE_HTTP"
        and schema["properties"]["paths"]["properties"]["mcp"]["const"] == MCP_PATH
        and schema["properties"]["paths"]["properties"]["health"]["const"] == HEALTH_PATH
        and "secret" not in json.dumps(schema, sort_keys=True).lower()
        and "token" not in json.dumps(schema, sort_keys=True).lower(),
    )
    check(
        "non-HTTPS-or-path-scoped-resource-identifiers-are-rejected",
        _expect_error(
            lambda: RemoteMcpServiceConfig(
                resource="http://mcp.cerebro.invalid",
                authorization_servers=(ISSUER,),
                resource_documentation="https://docs.cerebro.invalid/project-control",
            ),
            RemoteMcpConfigurationError,
        )
        and _expect_error(
            lambda: RemoteMcpServiceConfig(
                resource="https://mcp.cerebro.invalid/private",
                authorization_servers=(ISSUER,),
                resource_documentation="https://docs.cerebro.invalid/project-control",
            ),
            RemoteMcpConfigurationError,
        ),
    )
    check(
        "readiness-never-claims-ready-before-durable-probe-passes",
        service.readiness()["status"] == "NOT_READY",
    )
    ready[0] = True
    check(
        "readiness-is-minimal-and-does-not-disclose-credentials",
        service.readiness()["status"] == "READY"
        and "token" not in json.dumps(service.readiness(), sort_keys=True).lower()
        and "dsn" not in json.dumps(service.readiness(), sort_keys=True).lower(),
    )

    request_meta = {
        "openai/session": "REMOTE-CHAT-1",
        "openai/subject": "HOST-CORRELATION-NOT-AUTHORITY",
        "tenant_ref": "ATTACKER-TENANT",
    }
    trusted_context = McpToolCallContext(identity=_identity(), request_meta=request_meta)
    create_payload = {
        "project_ref": "TOTAL_MCP_REVISION",
        "aggregate_id": "AGG-TOTAL-MCP-REVISION",
        "source_revision": "b49110d16f363f58d1cd79432acb236ab3ac3014",
        "event_id": "EVENT-REMOTE-CREATE",
        "decision_ref": "MCPD-REMOTE-CREATE",
        "root": _root(),
        "make_default": True,
    }
    create_args = copy.deepcopy(create_payload)
    create_args["control_resolution_attestation"] = attestor.seal(
        operation="create_project_control_instance",
        payload=create_payload,
        context=trusted_context,
    )
    created = service.invoke(
        tool_name="create_project_control_instance",
        args=create_args,
        headers={"Authorization": "Bearer transition-token"},
        request_meta=request_meta,
    )
    check(
        "verified-token-identity-drives-attested-project-creation",
        created["structuredContent"]["project"]["tenant_ref"] == "TENANT-1"
        and created["structuredContent"]["project"]["workspace_ref"] == "WORKSPACE-1",
    )
    read = service.invoke(
        tool_name="read_project_control_state",
        args={"project_ref": "TOTAL_MCP_REVISION"},
        headers={"authorization": "Bearer read-token"},
        request_meta=request_meta,
    )
    check(
        "read-scope-token-can-read-only-its-verified-tenant-workspace",
        read["structuredContent"]["project"]["tenant_ref"] == "TENANT-1"
        and read["structuredContent"]["project"]["workspace_ref"] == "WORKSPACE-1",
    )
    check(
        "exact-resource-in-audience-list-and-scp-list-are-supported",
        service.invoke(
            tool_name="read_project_control_state",
            args={"project_ref": "TOTAL_MCP_REVISION"},
            headers={"Authorization": "Bearer audience-list-token"},
            request_meta=request_meta,
        )["structuredContent"]["project"]["project_ref"]
        == "TOTAL_MCP_REVISION"
        and service.invoke(
            tool_name="read_project_control_state",
            args={"project_ref": "TOTAL_MCP_REVISION"},
            headers={"Authorization": "Bearer scp-list-token"},
            request_meta=request_meta,
        )["structuredContent"]["project"]["project_ref"]
        == "TOTAL_MCP_REVISION",
    )
    begun = service.invoke(
        tool_name="begin_project_control_event",
        args={"event_id": "EVENT-REMOTE-1", "idempotency_key": "IDEM-REMOTE-1"},
        headers={"Authorization": "Bearer transition-token"},
        request_meta=request_meta,
    )
    check(
        "host-subject-and-unapproved-meta-remain-correlation-not-authorization",
        begun["structuredContent"]["session"]["principal_ref"] == "OAUTH-PRINCIPAL-1"
        and begun["structuredContent"]["session"]["principal_ref"] != request_meta["openai/subject"]
        and begun["structuredContent"]["session"]["tenant_ref"] != request_meta["tenant_ref"],
    )
    begun_state = begun["structuredContent"]
    directive = {
        "schema": DIRECTIVE_SCHEMA,
        "event_id": "EVENT-REMOTE-1",
        "decision_ref": "MCPD-REMOTE-NOOP",
        "expected_project_revision": begun_state["expected_project_revision"],
        "expected_project_fingerprint": begun_state["expected_project_fingerprint"],
        "expected_session_revision": begun_state["expected_session_revision"],
        "expected_session_fingerprint": begun_state["expected_session_fingerprint"],
        "project_operations": [],
        "session_operations": [],
    }
    complete_payload = {
        "event_id": "EVENT-REMOTE-1",
        "directive": directive,
        "navigation_options_candidate": None,
    }
    complete_args = {
        "event_id": complete_payload["event_id"],
        "directive": copy.deepcopy(complete_payload["directive"]),
        "control_resolution_attestation": attestor.seal(
            operation="complete_project_control_event",
            payload=complete_payload,
            context=trusted_context,
        ),
    }
    completed = service.invoke(
        tool_name="complete_project_control_event",
        args=complete_args,
        headers={"Authorization": "Bearer transition-token"},
        request_meta=request_meta,
    )
    completed_replay = service.invoke(
        tool_name="complete_project_control_event",
        args=complete_args,
        headers={"Authorization": "Bearer transition-token"},
        request_meta=request_meta,
    )
    check(
        "remote-boundary-completes-and-idempotently-replays-one-attested-event",
        completed["structuredContent"]["result"] == "PASS"
        and completed["structuredContent"]["receipt"]["event_id"] == "EVENT-REMOTE-1"
        and completed_replay["structuredContent"] == completed["structuredContent"],
    )
    reconnect_service = ControlContextRemoteMcpService(
        config=config,
        tools=ControlContextMcpTools(port, attestor),
        token_verifier=verifier,
        readiness_probe=lambda: True,
        clock=lambda: NOW,
    )
    reconnected = reconnect_service.invoke(
        tool_name="begin_project_control_event",
        args={
            "event_id": "EVENT-REMOTE-RECONNECT",
            "idempotency_key": "IDEM-REMOTE-RECONNECT",
        },
        headers={"Authorization": "Bearer transition-token"},
        request_meta=request_meta,
    )
    check(
        "fresh-service-boundary-reconnects-through-the-shared-state-port",
        reconnected["structuredContent"]["session"]["session_ref"]
        == "chatgpt:REMOTE-CHAT-1"
        and reconnected["structuredContent"]["project"]["fingerprint"]
        == completed["structuredContent"]["project"]["fingerprint"],
    )
    check(
        "remote-boundary-does-not-accept-local-session-fallback-metadata",
        _expect_error(
            lambda: service.invoke(
                tool_name="begin_project_control_event",
                args={"event_id": "EVENT-LOCAL-META", "idempotency_key": "IDEM-LOCAL-META"},
                headers={"Authorization": "Bearer transition-token"},
                request_meta={"cerebro/session": "UNAPPROVED-REMOTE-FALLBACK"},
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    check(
        "oversized-trusted-host-metadata-is-rejected-before-state-access",
        _expect_error(
            lambda: service.invoke(
                tool_name="read_project_control_state",
                args={"project_ref": "TOTAL_MCP_REVISION"},
                headers={"Authorization": "Bearer read-token"},
                request_meta={"openai/session": "X" * 513},
            ),
            ControlContextToolError,
        ),
    )

    missing = service.invoke(
        tool_name="read_project_control_state",
        args={"project_ref": "TOTAL_MCP_REVISION"},
        headers={},
        request_meta=request_meta,
    )
    check(
        "missing-token-emits-tool-level-OAuth-challenge",
        _auth_error(missing, error="invalid_token", scope="project_state:read"),
    )
    check(
        "unverified-signature-is-rejected-before-tool-dispatch",
        _auth_error(
            service.invoke(
                tool_name="read_project_control_state",
                args={"project_ref": "TOTAL_MCP_REVISION"},
                headers={"Authorization": "Bearer unsigned-token"},
                request_meta=request_meta,
            ),
            error="invalid_token",
            scope="project_state:read",
        ),
    )
    for name, token in (
        ("wrong-issuer-is-rejected", "bad-issuer-token"),
        ("wrong-audience-is-rejected", "bad-audience-token"),
        ("expired-token-is-rejected", "expired-token"),
        ("not-yet-valid-token-is-rejected", "future-token"),
        ("missing-bound-identity-claim-is-rejected", "missing-identity-token"),
        ("boolean-expiry-is-not-accepted-as-a-numeric-date", "boolean-expiry-token"),
    ):
        check(
            name,
            _auth_error(
                service.invoke(
                    tool_name="read_project_control_state",
                    args={"project_ref": "TOTAL_MCP_REVISION"},
                    headers={"Authorization": f"Bearer {token}"},
                    request_meta=request_meta,
                ),
                error="invalid_token",
                scope="project_state:read",
            ),
        )
    insufficient = service.invoke(
        tool_name="begin_project_control_event",
        args={"event_id": "EVENT-NO-SCOPE", "idempotency_key": "IDEM-NO-SCOPE"},
        headers={"Authorization": "Bearer read-token"},
        request_meta=request_meta,
    )
    check(
        "read-scope-cannot-cross-transition-boundary",
        _auth_error(insufficient, error="insufficient_scope", scope="project_state:transition"),
    )
    check(
        "multiple-authorization-headers-are-rejected-as-an-invalid-request",
        _auth_error(
            service.invoke(
                tool_name="read_project_control_state",
                args={"project_ref": "TOTAL_MCP_REVISION"},
                headers={
                    "Authorization": "Bearer read-token",
                    "authorization": "Bearer transition-token",
                },
                request_meta=request_meta,
            ),
            error="invalid_request",
            scope="project_state:read",
        ),
    )
    verifier_failure = service.invoke(
        tool_name="read_project_control_state",
        args={"project_ref": "TOTAL_MCP_REVISION"},
        headers={"Authorization": "Bearer verifier-explodes"},
        request_meta=request_meta,
    )
    check(
        "token-and-verifier-internals-never-appear-in-OAuth-error-result",
        "verifier-explodes" not in json.dumps(verifier_failure)
        and "SECRET-VERIFIER-INTERNAL" not in json.dumps(verifier_failure),
    )
    check(
        "tool-input-cannot-override-OAuth-identity-through-remote-boundary",
        _expect_error(
            lambda: service.invoke(
                tool_name="read_project_control_state",
                args={"project_ref": "TOTAL_MCP_REVISION", "principal_ref": "ATTACKER"},
                headers={"Authorization": "Bearer read-token"},
                request_meta=request_meta,
            ),
            ControlContextToolAuthorizationError,
        ),
    )
    check(
        "unknown-tool-is-rejected-before-authentication-or-dispatch",
        _expect_error(
            lambda: service.invoke(
                tool_name="repository_write",
                args={},
                headers={"Authorization": "Bearer transition-token"},
                request_meta=request_meta,
            ),
            ControlContextToolError,
        ),
    )

    contract = yaml.safe_load(
        (SOURCE_ROOT / "standards/control-context-state-service.yaml").read_text(encoding="utf-8")
    )["control_context_state_service"]
    remote = contract["remote_MCP_service_boundary"]
    check(
        "contract-distinguishes-local-service-proof-from-live-deployment",
        remote["local_authentication_and_service_contract_proven"] is True
        and remote["official_MCP_SDK_binding_implemented"] is True
        and remote["official_MCP_SDK_transport_bound"] is True
        and remote["local_official_MCP_SDK_protocol_proven"] is True
        and remote["identity_provider_selected"] is False
        and remote["deployed"] is False
        and remote["local_contract_evidence_is_remote_activation"] is False,
    )
    return {
        "schema": "cerebro-control-context-remote-service-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
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
