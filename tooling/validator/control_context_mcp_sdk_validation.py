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

from control_context_mcp_sdk import (  # noqa: E402
    OFFICIAL_MCP_SDK_VALIDATED_VERSION,
    ControlContextMcpSdkBindingError,
    add_openai_tool_security_schemes,
    create_streamable_http_app,
    official_mcp_sdk_runtime,
)
from control_context_remote_service import (  # noqa: E402
    PROTECTED_RESOURCE_PATH,
    STATE_SCOPES,
    TOOL_REQUIRED_SCOPES,
    ControlContextRemoteMcpService,
    RemoteMcpServiceConfig,
    VerifiedBearerToken,
)
from control_context_registry import DIRECTIVE_SCHEMA  # noqa: E402
from control_context_state_port import InMemoryControlContextStatePort  # noqa: E402
from control_context_tools import (  # noqa: E402
    ControlContextMcpTools,
    HmacControlResolutionAttestor,
    McpToolCallContext,
    VerifiedMcpIdentity,
)


NOW = 2_000_000_000.0
RESOURCE = "https://mcp.cerebro.invalid"
ISSUER = "https://auth.cerebro.invalid"
PROTOCOL_VERSION = "2025-06-18"


def _expect_error(function: Callable[[], Any], expected: type[BaseException]) -> bool:
    try:
        function()
    except expected:
        return True
    return False


def _claims(scopes: str) -> dict[str, Any]:
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
    def verify(self, token: str) -> VerifiedBearerToken:
        values = {
            "read-token": VerifiedBearerToken(
                claims=_claims("project_state:read"),
                signature_verified=True,
            ),
            "transition-token": VerifiedBearerToken(
                claims=_claims("project_state:read project_state:transition"),
                signature_verified=True,
            ),
        }
        return values.get(token, VerifiedBearerToken(claims={}, signature_verified=False))


def _root() -> dict[str, Any]:
    return {
        "context_id": "CTX-ROOT",
        "human_label": "Hovedspor",
        "objective_ref": "OBJ-TOTAL-MCP-REVISION",
        "scope_ref": "SCOPE-TOTAL-MCP-REVISION",
        "basis_refs": ["OFFICIAL-MCP-SDK-LOCAL-PROTOCOL-PROOF"],
        "project_basis_ref": "PROJECT-BASIS-TOTAL-MCP-REVISION-V1",
        "quality_trace_ref": "QUALITY-DEEP-V1",
        "completion_criteria_refs": ["V1-ACCEPTANCE"],
    }


def _json(response: Any) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("JSON-object-response-required")
    return value


def _matches_declared_output_schema(
    tool_by_name: dict[str, dict[str, Any]],
    tool_name: str,
    structured_content: Any,
) -> bool:
    if not isinstance(structured_content, dict):
        return False
    schema = tool_by_name.get(tool_name, {}).get("outputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not isinstance(properties, dict):
        return False
    if not set(required).issubset(structured_content):
        return False
    for field, field_schema in properties.items():
        if (
            field in structured_content
            and isinstance(field_schema, dict)
            and "const" in field_schema
            and structured_content[field] != field_schema["const"]
        ):
            return False
    return True


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []

    def check(name: str, condition: bool) -> None:
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    try:
        from starlette.testclient import TestClient
    except Exception as exc:
        raise RuntimeError("official-MCP-SDK-test-runtime-unavailable") from exc

    runtime = official_mcp_sdk_runtime()
    requirements = (
        SOURCE_ROOT / "mcp/control-context-mcp-sdk-requirements.txt"
    ).read_text(encoding="utf-8")
    check(
        "official-MCP-Python-SDK-runtime-is-exactly-the-validated-version",
        runtime["distribution"] == "mcp"
        and runtime["version"] == OFFICIAL_MCP_SDK_VALIDATED_VERSION
        and f"mcp=={OFFICIAL_MCP_SDK_VALIDATED_VERSION}" in requirements,
    )

    config = RemoteMcpServiceConfig(
        resource=RESOURCE,
        authorization_servers=(ISSUER,),
        resource_documentation="https://docs.cerebro.invalid/project-control",
        service_version="1.0.0-sdk-local-proof",
        clock_skew_seconds=30,
    )
    port = InMemoryControlContextStatePort()
    attestor = HmacControlResolutionAttestor(
        key_id="MCP-SDK-SELFTEST",
        secret=b"official-mcp-sdk-selftest-key-0001",
    )
    tools = ControlContextMcpTools(port, attestor)
    ready = [False]
    service = ControlContextRemoteMcpService(
        config=config,
        tools=tools,
        token_verifier=StaticTokenVerifier(),
        readiness_probe=lambda: ready[0],
        clock=lambda: NOW,
    )
    trusted_context = McpToolCallContext(
        identity=VerifiedMcpIdentity(
            tenant_ref="TENANT-1",
            workspace_ref="WORKSPACE-1",
            principal_ref="OAUTH-PRINCIPAL-1",
            scopes=frozenset(STATE_SCOPES),
            token_verified=True,
        ),
        request_meta={
            "openai/session": "SDK-CHAT-1",
            "openai/subject": "HOST-CORRELATION-NOT-AUTHORITY",
        },
    )
    create_payload = {
        "project_ref": "TOTAL_MCP_REVISION",
        "aggregate_id": "AGG-TOTAL-MCP-REVISION",
        "source_revision": "b49110d16f363f58d1cd79432acb236ab3ac3014",
        "event_id": "EVENT-SDK-CREATE",
        "decision_ref": "MCPD-SDK-CREATE",
        "root": _root(),
        "make_default": True,
    }
    create_args = copy.deepcopy(create_payload)
    create_args["control_resolution_attestation"] = attestor.seal(
        operation="create_project_control_instance",
        payload=create_payload,
        context=trusted_context,
    )
    check(
        "transport-security-rejects-wildcard-duplicate-and-non-HTTPS-inputs",
        _expect_error(
            lambda: create_streamable_http_app(service, allowed_hosts=("*",)),
            ControlContextMcpSdkBindingError,
        )
        and _expect_error(
            lambda: create_streamable_http_app(
                service,
                allowed_hosts=("mcp.cerebro.invalid", "mcp.cerebro.invalid"),
            ),
            ControlContextMcpSdkBindingError,
        )
        and _expect_error(
            lambda: create_streamable_http_app(
                service,
                allowed_hosts=("mcp.cerebro.invalid",),
                allowed_origins=("http://chatgpt.com",),
            ),
            ControlContextMcpSdkBindingError,
        ),
    )
    app = create_streamable_http_app(service, allowed_hosts=("testserver",))
    base_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    protocol_headers = {**base_headers, "MCP-Protocol-Version": PROTOCOL_VERSION}

    with TestClient(app) as client:
        metadata_response = client.get(PROTECTED_RESOURCE_PATH)
        metadata = _json(metadata_response)
        check(
            "official-ASGI-app-serves-exact-public-protected-resource-metadata",
            metadata_response.status_code == 200
            and metadata["resource"] == RESOURCE
            and metadata["authorization_servers"] == [ISSUER]
            and metadata_response.headers.get("cache-control") == "no-store",
        )
        unavailable = client.get("/healthz")
        check(
            "official-ASGI-readiness-fails-closed-before-durable-probe",
            unavailable.status_code == 503
            and _json(unavailable)["status"] == "NOT_READY",
        )
        ready[0] = True
        available = client.get("/healthz")
        check(
            "official-ASGI-readiness-is-minimal-after-durable-probe",
            available.status_code == 200
            and _json(available)["status"] == "READY"
            and "dsn" not in available.text.lower()
            and "token" not in available.text.lower(),
        )

        initialize_response = client.post(
            "/mcp",
            headers=base_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "cerebro-sdk-selftest", "version": "1.0.0"},
                },
            },
        )
        initialize = _json(initialize_response)
        check(
            "official-SDK-negotiates-streamable-HTTP-initialize-on-stable-path",
            initialize_response.status_code == 200
            and initialize["result"]["protocolVersion"] == PROTOCOL_VERSION
            and initialize["result"]["serverInfo"]["name"] == "cerebro-project-control"
            and "mcp-session-id" not in initialize_response.headers,
        )

        list_response = client.post(
            "/mcp",
            headers=protocol_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        listed = _json(list_response)["result"]["tools"]
        tool_by_name = {tool["name"]: tool for tool in listed}
        check(
            "official-SDK-lists-the-complete-private-tool-surface-without-authentication",
            list_response.status_code == 200
            and {tool["name"] for tool in listed} == set(TOOL_REQUIRED_SCOPES),
        )
        check(
            "OpenAI-top-level-and-backcompat-security-schemes-are-identical-on-wire",
            all(
                tool.get("securitySchemes")
                == tool.get("_meta", {}).get("securitySchemes")
                == [
                    {
                        "type": "oauth2",
                        "scopes": [TOOL_REQUIRED_SCOPES[tool["name"]]],
                    }
                ]
                for tool in listed
            ),
        )
        check(
            "official-SDK-wire-surface-publishes-one-bounded-output-schema-per-tool",
            all(
                isinstance(tool.get("outputSchema"), dict)
                and tool["outputSchema"].get("type") == "object"
                and isinstance(tool["outputSchema"].get("required"), list)
                for tool in listed
            ),
        )
        current_handshake = client.post(
            "/mcp",
            headers=base_headers,
            json={
                "jsonrpc": "2.0",
                "id": 20,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "cerebro-sdk-selftest", "version": "1.0.0"},
                },
            },
        )
        current_list = client.post(
            "/mcp",
            headers={**base_headers, "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
        )
        current_tools = _json(current_list).get("result", {}).get("tools", [])
        check(
            "official-SDK-current-handshake-era-preserves-the-same-tool-contract",
            current_handshake.status_code == 200
            and _json(current_handshake)["result"]["protocolVersion"] == "2025-11-25"
            and current_list.status_code == 200
            and {tool.get("name") for tool in current_tools} == set(TOOL_REQUIRED_SCOPES)
            and all(
                tool.get("securitySchemes")
                == tool.get("_meta", {}).get("securitySchemes")
                for tool in current_tools
            ),
        )
        check(
            "wire-tool-annotations-remain-conservative-and-repository-free",
            all(
                tool.get("annotations", {}).get("destructiveHint") is False
                and tool.get("annotations", {}).get("openWorldHint") is False
                for tool in listed
            )
            and "repository_write" not in list_response.text
            and "github" not in list_response.text.lower(),
        )

        create_response = client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json={
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {
                    "name": "create_project_control_instance",
                    "arguments": create_args,
                    "_meta": trusted_context.request_meta,
                },
            },
        )
        create_result = _json(create_response)["result"]
        created_state = create_result.get("structuredContent", {})
        check(
            "official-SDK-create-call-establishes-the-protocol-fixture-state",
            create_response.status_code == 200
            and create_result["isError"] is False
            and created_state["project"]["project_ref"] == "TOTAL_MCP_REVISION"
            and created_state["repository_permission_required"] is False
            and _matches_declared_output_schema(
                tool_by_name,
                "create_project_control_instance",
                created_state,
            ),
        )

        missing_token = client.post(
            "/mcp",
            headers=protocol_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "read_project_control_state",
                    "arguments": {"project_ref": "TOTAL_MCP_REVISION"},
                    "_meta": {"openai/session": "SDK-CHAT-1"},
                },
            },
        )
        missing_result = _json(missing_token)["result"]
        challenge = missing_result.get("_meta", {}).get("mcp/www_authenticate", [""])[0]
        check(
            "unauthenticated-tool-call-returns-the-OAuth-linking-challenge-on-wire",
            missing_token.status_code == 200
            and missing_result["isError"] is True
            and f'resource_metadata="{RESOURCE}{PROTECTED_RESOURCE_PATH}"' in challenge
            and 'scope="project_state:read"' in challenge,
        )

        read_response = client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer read-token"},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "read_project_control_state",
                    "arguments": {"project_ref": "TOTAL_MCP_REVISION"},
                    "_meta": {
                        "openai/session": "SDK-CHAT-1",
                        "openai/subject": "HOST-CORRELATION-NOT-AUTHORITY",
                        "tenant_ref": "ATTACKER-TENANT",
                    },
                },
            },
        )
        read_result = _json(read_response)["result"]
        check(
            "verified-bearer-header-and-host-meta-reach-the-service-through-the-SDK",
            read_response.status_code == 200
            and read_result["isError"] is False
            and read_result["structuredContent"]["project"]["tenant_ref"] == "TENANT-1"
            and read_result["structuredContent"]["project"]["tenant_ref"] != "ATTACKER-TENANT"
            and read_result["_meta"]["cerebro/subjectCorrelationPresent"] is True
            and bool(read_result["content"][0]["text"]),
        )

        insufficient = client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer read-token"},
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "begin_project_control_event",
                    "arguments": {
                        "event_id": "EVENT-SDK-NO-SCOPE",
                        "idempotency_key": "IDEM-SDK-NO-SCOPE",
                    },
                    "_meta": {"openai/session": "SDK-CHAT-1"},
                },
            },
        )
        insufficient_result = _json(insufficient)["result"]
        check(
            "per-tool-transition-scope-is-enforced-through-the-official-SDK",
            insufficient_result["isError"] is True
            and 'error="insufficient_scope"'
            in insufficient_result["_meta"]["mcp/www_authenticate"][0],
        )

        begun = client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "begin_project_control_event",
                    "arguments": {
                        "event_id": "EVENT-SDK-1",
                        "idempotency_key": "IDEM-SDK-1",
                    },
                    "_meta": {"openai/session": "SDK-CHAT-1"},
                },
            },
        )
        begun_result = _json(begun)["result"]
        check(
            "stateless-transport-preserves-durable-control-session-correlation",
            begun_result["isError"] is False
            and begun_result["structuredContent"]["session"]["session_ref"]
            == "chatgpt:SDK-CHAT-1"
            and "mcp-session-id" not in begun.headers,
        )
        begun_state = begun_result["structuredContent"]
        check(
            "begin-result-conforms-to-its-declared-output-schema",
            _matches_declared_output_schema(
                tool_by_name,
                "begin_project_control_event",
                begun_state,
            ),
        )

        directive = {
            "schema": DIRECTIVE_SCHEMA,
            "event_id": "EVENT-SDK-1",
            "decision_ref": "MCPD-SDK-NOOP",
            "expected_project_revision": begun_state["expected_project_revision"],
            "expected_project_fingerprint": begun_state["expected_project_fingerprint"],
            "expected_session_revision": begun_state["expected_session_revision"],
            "expected_session_fingerprint": begun_state["expected_session_fingerprint"],
            "project_operations": [],
            "session_operations": [],
        }
        complete_payload = {
            "event_id": "EVENT-SDK-1",
            "directive": directive,
            "navigation_options_candidate": None,
        }
        complete_args = {
            "event_id": complete_payload["event_id"],
            "directive": copy.deepcopy(complete_payload["directive"]),
        }
        complete_args["control_resolution_attestation"] = attestor.seal(
            operation="complete_project_control_event",
            payload=complete_payload,
            context=trusted_context,
        )
        complete_envelope = {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {
                "name": "complete_project_control_event",
                "arguments": complete_args,
                "_meta": trusted_context.request_meta,
            },
        }
        completed = client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json=complete_envelope,
        )
        completed_result = _json(completed)["result"]
        completed_state = completed_result.get("structuredContent", {})
        check(
            "official-SDK-completes-the-event-and-returns-commit-bound-state",
            completed.status_code == 200
            and completed_result["isError"] is False
            and completed_state["result"] == "PASS"
            and completed_state["receipt"]["event_id"] == "EVENT-SDK-1"
            and completed_state["project"]["fingerprint"]
            == completed_state["receipt"]["project_fingerprint_after"]
            and completed_state["session"]["fingerprint"]
            == completed_state["receipt"]["session_fingerprint_after"]
            and completed_state["navigation_activation"]["result"] == "NOT_REQUESTED"
            and _matches_declared_output_schema(
                tool_by_name,
                "complete_project_control_event",
                completed_state,
            ),
        )
        replay_envelope = copy.deepcopy(complete_envelope)
        replay_envelope["id"] = 24
        completed_replay = client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json=replay_envelope,
        )
        replay_result = _json(completed_replay)["result"]
        check(
            "exact-completion-replay-is-idempotent-through-the-official-SDK",
            replay_result["isError"] is False
            and replay_result["structuredContent"] == completed_state,
        )

        duplicate_headers = [
            ("Accept", "application/json, text/event-stream"),
            ("Content-Type", "application/json"),
            ("MCP-Protocol-Version", PROTOCOL_VERSION),
            ("Authorization", "Bearer read-token"),
            ("authorization", "Bearer transition-token"),
        ]
        duplicate = client.post(
            "/mcp",
            headers=duplicate_headers,
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "read_project_control_state",
                    "arguments": {"project_ref": "TOTAL_MCP_REVISION"},
                    "_meta": {"openai/session": "SDK-CHAT-1"},
                },
            },
        )
        duplicate_result = _json(duplicate)["result"]
        check(
            "duplicate-authorization-headers-survive-transport-and-fail-closed",
            duplicate_result["isError"] is True
            and 'error="invalid_request"'
            in duplicate_result["_meta"]["mcp/www_authenticate"][0],
        )

        unknown = client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "repository_write",
                    "arguments": {},
                    "_meta": {"openai/session": "SDK-CHAT-1"},
                },
            },
        )
        unknown_result = _json(unknown)["result"]
        check(
            "unknown-repository-like-call-becomes-a-bounded-tool-error",
            unknown_result["isError"] is True
            and unknown_result["content"][0]["text"]
            == "The project-control request was rejected."
            and "transition-token" not in unknown.text,
        )

    reconnect_service = ControlContextRemoteMcpService(
        config=config,
        tools=ControlContextMcpTools(port, attestor),
        token_verifier=StaticTokenVerifier(),
        readiness_probe=lambda: True,
        clock=lambda: NOW,
    )
    reconnect_app = create_streamable_http_app(
        reconnect_service,
        allowed_hosts=("testserver",),
    )
    with TestClient(reconnect_app) as reconnect_client:
        reconnect_initialize = reconnect_client.post(
            "/mcp",
            headers=base_headers,
            json={
                "jsonrpc": "2.0",
                "id": 30,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "cerebro-sdk-reconnect", "version": "1.0.0"},
                },
            },
        )
        reconnect_call = {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "begin_project_control_event",
                "arguments": {
                    "event_id": "EVENT-SDK-RECONNECT",
                    "idempotency_key": "IDEM-SDK-RECONNECT",
                },
                "_meta": {"openai/session": "SDK-CHAT-1"},
            },
        }
        reconnected = reconnect_client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json=reconnect_call,
        )
        reconnected_result = _json(reconnected)["result"]
        reconnected_state = reconnected_result.get("structuredContent", {})
        check(
            "fresh-official-SDK-service-instance-reconnects-to-the-same-state-port",
            reconnect_initialize.status_code == 200
            and reconnected_result["isError"] is False
            and reconnected_state["session"]["session_ref"] == "chatgpt:SDK-CHAT-1"
            and reconnected_state["project"]["fingerprint"]
            == completed_state["project"]["fingerprint"],
        )
        second_session_call = {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {
                "name": "begin_project_control_event",
                "arguments": {
                    "event_id": "EVENT-SDK-SECOND-SESSION",
                    "idempotency_key": "IDEM-SDK-SECOND-SESSION",
                },
                "_meta": {"openai/session": "SDK-CHAT-2"},
            },
        }
        second_session = reconnect_client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json=second_session_call,
        )
        second_session_result = _json(second_session)["result"]
        second_session_state = second_session_result.get("structuredContent", {})
        check(
            "fresh-host-session-shares-the-project-tree-but-has-distinct-focus-state",
            second_session_result["isError"] is False
            and second_session_state["project"]["fingerprint"]
            == reconnected_state["project"]["fingerprint"]
            and second_session_state["session"]["session_ref"] == "chatgpt:SDK-CHAT-2"
            and second_session_state["session"]["session_ref"]
            != reconnected_state["session"]["session_ref"],
        )
        reconnect_replay = copy.deepcopy(reconnect_call)
        reconnect_replay["id"] = 33
        first_session_replay = reconnect_client.post(
            "/mcp",
            headers={**protocol_headers, "Authorization": "Bearer transition-token"},
            json=reconnect_replay,
        )
        first_session_replay_result = _json(first_session_replay)["result"]
        check(
            "second-session-begin-does-not-move-or-rewrite-the-first-session-binding",
            first_session_replay_result["isError"] is False
            and first_session_replay_result["structuredContent"] == reconnected_state,
        )

    conflict = {
        "jsonrpc": "2.0",
        "id": 9,
        "result": {
            "tools": [
                {
                    "name": "read_project_control_state",
                    "securitySchemes": [{"type": "noauth"}],
                    "_meta": {
                        "securitySchemes": [
                            {"type": "oauth2", "scopes": ["project_state:read"]}
                        ]
                    },
                }
            ]
        },
    }
    check(
        "security-scheme-wire-adapter-fails-closed-on-conflicting-metadata",
        _expect_error(
            lambda: add_openai_tool_security_schemes(conflict),
            ControlContextMcpSdkBindingError,
        ),
    )

    limited_app = create_streamable_http_app(
        service,
        allowed_hosts=("testserver",),
        max_request_body_size=1024,
    )
    with TestClient(limited_app) as client:
        oversized = client.post(
            "/mcp",
            headers=base_headers,
            content=b"{" + b" " * 2048 + b"}",
        )
        check(
            "official-SDK-request-body-limit-rejects-oversized-input-before-dispatch",
            oversized.status_code == 413,
        )

    contract = yaml.safe_load(
        (SOURCE_ROOT / "standards/control-context-state-service.yaml").read_text(encoding="utf-8")
    )["control_context_state_service"]["remote_MCP_service_boundary"]
    check(
        "contract-bounds-local-SDK-proof-below-live-deployment",
        contract["official_MCP_SDK_binding_implemented"] is True
        and contract["local_official_MCP_SDK_protocol_proven"] is True
        and contract["official_MCP_SDK_transport_bound"] is True
        and contract["deployed"] is False
        and contract["identity_provider_selected"] is False
        and contract["local_contract_evidence_is_remote_activation"] is False,
    )
    return {
        "schema": "cerebro-control-context-official-mcp-sdk-selftest/v1",
        "result": "PASS" if all(item["result"] == "PASS" for item in tests) else "FAIL",
        "test_count": len(tests),
        "failures": [item for item in tests if item["result"] != "PASS"],
        "tests": tests,
        "evidence_class": "LOCAL_OFFICIAL_SDK_PROTOCOL_NOT_REMOTE_ACTIVATION",
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
