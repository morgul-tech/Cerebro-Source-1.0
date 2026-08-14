#!/usr/bin/env python3
"""Official MCP SDK binding for the remote project-control service.

The domain service remains provider neutral.  This module binds it to the
official Python MCP SDK's stateless streamable-HTTP ASGI application, exposes
the public OAuth resource metadata and readiness routes, and preserves the
OpenAI tool-security extension on the wire.

No listener is opened at import time.  Deployment code must inject the already
configured ``ControlContextRemoteMcpService`` and then serve the returned ASGI
application with its approved process and network runtime.
"""

from __future__ import annotations

import copy
import importlib.metadata
import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from control_context_remote_service import (
    HEALTH_PATH,
    MCP_PATH,
    PROTECTED_RESOURCE_PATH,
    ControlContextRemoteMcpService,
)
from control_context_tools import ControlContextToolError


OFFICIAL_MCP_SDK_DISTRIBUTION = "mcp"
OFFICIAL_MCP_SDK_SUPPORTED_MAJOR = 2
OFFICIAL_MCP_SDK_VALIDATED_VERSION = "2.0.0"
DEFAULT_MAX_REQUEST_BODY_SIZE = 1024 * 1024
logger = logging.getLogger(__name__)


class ControlContextMcpSdkBindingError(RuntimeError):
    pass


class _RawHeaderMapping(Mapping[str, str]):
    """Mapping view whose ``items`` preserves duplicate ASGI headers."""

    def __init__(self, items: Iterable[tuple[str, str]]):
        self._items = tuple(items)
        self._lookup: dict[str, str] = {}
        for name, value in self._items:
            self._lookup[name] = value

    def __getitem__(self, key: str) -> str:
        return self._lookup[key]

    def __iter__(self):
        return iter(self._lookup)

    def __len__(self) -> int:
        return len(self._lookup)

    def items(self):
        return self._items


def _sdk_modules() -> tuple[Any, Any, Any, Any, Any, Any]:
    try:
        version = importlib.metadata.version(OFFICIAL_MCP_SDK_DISTRIBUTION)
        major_text = version.split(".", 1)[0]
        major = int(major_text)
        if major != OFFICIAL_MCP_SDK_SUPPORTED_MAJOR:
            raise ControlContextMcpSdkBindingError(
                f"official-MCP-SDK-major-unsupported:{major_text}"
            )
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.responses import JSONResponse
        from starlette.routing import Route
    except ControlContextMcpSdkBindingError:
        raise
    except Exception as exc:
        raise ControlContextMcpSdkBindingError(
            "official-MCP-SDK-runtime-dependency-unavailable"
        ) from exc
    return types, Server, TransportSecuritySettings, JSONResponse, Route, version


def official_mcp_sdk_runtime() -> dict[str, Any]:
    """Return bounded, non-secret runtime evidence for readiness diagnostics."""

    _, _, _, _, _, version = _sdk_modules()
    return {
        "distribution": OFFICIAL_MCP_SDK_DISTRIBUTION,
        "version": version,
        "supported_major": OFFICIAL_MCP_SDK_SUPPORTED_MAJOR,
        "validated_version": OFFICIAL_MCP_SDK_VALIDATED_VERSION,
    }


def _sdk_tool(types: Any, definition: Mapping[str, Any]) -> Any:
    value = copy.deepcopy(dict(definition))
    schemes = value.pop("securitySchemes", None)
    if not isinstance(schemes, list) or not schemes:
        raise ControlContextMcpSdkBindingError("tool-security-schemes-required")
    meta = value.get("_meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ControlContextMcpSdkBindingError("tool-meta-object-required")
    meta["securitySchemes"] = copy.deepcopy(schemes)
    value["_meta"] = meta
    try:
        return types.Tool.model_validate(value, by_alias=True)
    except Exception as exc:
        raise ControlContextMcpSdkBindingError("tool-definition-invalid-for-official-SDK") from exc


def _request_headers(context: Any) -> Mapping[str, str]:
    request = getattr(context, "request", None)
    headers = getattr(request, "headers", None)
    raw = getattr(headers, "raw", None)
    if isinstance(raw, list):
        values: list[tuple[str, str]] = []
        for name, value in raw:
            if isinstance(name, bytes) and isinstance(value, bytes):
                values.append((name.decode("latin-1"), value.decode("latin-1")))
        return _RawHeaderMapping(values)
    if isinstance(headers, Mapping):
        return _RawHeaderMapping(
            (str(name), str(value)) for name, value in headers.items()
        )
    return _RawHeaderMapping(())


def _request_meta(params: Any) -> dict[str, Any]:
    meta = getattr(params, "meta", None)
    return dict(meta) if isinstance(meta, Mapping) else {}


def _text_fallback(structured_content: Any) -> str:
    if structured_content is None:
        return "Project-control request completed."
    try:
        return json.dumps(
            structured_content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        return "Project-control request completed with structured output."


def _call_tool_result(types: Any, value: Mapping[str, Any]) -> Any:
    result = copy.deepcopy(dict(value))
    if "content" not in result:
        result["content"] = [
            {"type": "text", "text": _text_fallback(result.get("structuredContent"))}
        ]
    try:
        return types.CallToolResult.model_validate(result, by_alias=True)
    except Exception as exc:
        raise ControlContextMcpSdkBindingError("tool-result-invalid-for-official-SDK") from exc


def create_official_mcp_server(service: ControlContextRemoteMcpService) -> Any:
    """Bind service handlers to the official SDK's low-level typed server."""

    if not isinstance(service, ControlContextRemoteMcpService):
        raise ControlContextMcpSdkBindingError("remote-MCP-service-required")
    types, Server, _, _, _, _ = _sdk_modules()
    definitions = service.list_tools()
    sdk_tools = tuple(_sdk_tool(types, definition) for definition in definitions)
    descriptor = service.server_descriptor()

    async def on_list_tools(context: Any, params: Any) -> Any:
        del context, params
        return types.ListToolsResult(
            tools=list(sdk_tools),
            ttlMs=0,
            cacheScope="private",
        )

    async def on_call_tool(context: Any, params: Any) -> Any:
        try:
            value = service.invoke(
                tool_name=params.name,
                args=params.arguments or {},
                headers=_request_headers(context),
                request_meta=_request_meta(params),
            )
        except ControlContextToolError:
            value = {
                "content": [
                    {"type": "text", "text": "The project-control request was rejected."}
                ],
                "isError": True,
            }
        except Exception:
            logger.exception("project-control MCP tool invocation failed")
            value = {
                "content": [
                    {
                        "type": "text",
                        "text": "The project-control service could not complete the request.",
                    }
                ],
                "isError": True,
            }
        return _call_tool_result(types, value)

    return Server(
        descriptor["name"],
        version=descriptor["version"],
        title="Cerebro Project Control",
        description="Durable hierarchical project-control state for authenticated Cerebro consumers.",
        instructions=descriptor["instructions"],
        website_url=service.config.resource_documentation,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def add_openai_tool_security_schemes(envelope: Any) -> int:
    """Mirror `_meta.securitySchemes` to OpenAI's top-level tool extension.

    The official SDK correctly retains the backwards-compatible `_meta` copy,
    while its core MCP wire model ignores the OpenAI top-level extension.  This
    deterministic post-serialization adapter restores the exact same value.
    """

    if isinstance(envelope, list):
        return sum(add_openai_tool_security_schemes(item) for item in envelope)
    if not isinstance(envelope, dict):
        return 0
    result = envelope.get("result")
    if not isinstance(result, dict) or "tools" not in result:
        return 0
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise ControlContextMcpSdkBindingError("wire-tools-list-invalid")
    count = 0
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ControlContextMcpSdkBindingError("wire-tool-invalid")
        meta = tool.get("_meta")
        schemes = meta.get("securitySchemes") if isinstance(meta, dict) else None
        if not isinstance(schemes, list) or not schemes:
            raise ControlContextMcpSdkBindingError(
                f"wire-tool-security-schemes-missing:{tool['name']}"
            )
        existing = tool.get("securitySchemes")
        if existing is not None and existing != schemes:
            raise ControlContextMcpSdkBindingError(
                f"wire-tool-security-schemes-conflict:{tool['name']}"
            )
        tool["securitySchemes"] = copy.deepcopy(schemes)
        count += 1
    return count


class OpenAiToolSecuritySchemesMiddleware:
    """ASGI response adapter limited to JSON ``tools/list`` responses."""

    def __init__(self, app: Any, *, mcp_path: str = MCP_PATH):
        if not callable(app):
            raise ControlContextMcpSdkBindingError("ASGI-application-required")
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.mcp_path:
            await self.app(scope, receive, send)
            return

        start: dict[str, Any] | None = None
        body_parts: list[bytes] = []

        async def capture(message: dict[str, Any]) -> None:
            nonlocal start
            if message.get("type") == "http.response.start":
                start = dict(message)
                return
            if message.get("type") != "http.response.body":
                await send(message)
                return
            body = message.get("body", b"")
            if isinstance(body, bytes):
                body_parts.append(body)
            if message.get("more_body"):
                return
            response_start = start or {
                "type": "http.response.start",
                "status": 500,
                "headers": [],
            }
            response_body = b"".join(body_parts)
            headers = list(response_start.get("headers", []))
            content_type = next(
                (
                    value.decode("latin-1").lower()
                    for name, value in headers
                    if name.lower() == b"content-type"
                ),
                "",
            )
            encoded = next(
                (value for name, value in headers if name.lower() == b"content-encoding"),
                None,
            )
            if "application/json" in content_type and encoded is None and response_body:
                envelope: Any = None
                try:
                    envelope = json.loads(response_body)
                    add_openai_tool_security_schemes(envelope)
                    response_body = json.dumps(
                        envelope,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                except ControlContextMcpSdkBindingError:
                    logger.exception("OpenAI tool security metadata rendering failed")
                    request_id = None
                    if isinstance(envelope, dict):
                        request_id = envelope.get("id")
                    response_start["status"] = 500
                    response_body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32603,
                                "message": "Tool security metadata could not be rendered.",
                            },
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
            headers = [
                (name, value) for name, value in headers if name.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(response_body)).encode("ascii")))
            response_start["headers"] = headers
            await send(response_start)
            await send({"type": "http.response.body", "body": response_body})

        await self.app(scope, receive, capture)


def _validated_hosts(service: ControlContextRemoteMcpService, values: Iterable[str] | None) -> list[str]:
    if values is None:
        host = urlsplit(service.config.resource).netloc
        values = (host,)
    if isinstance(values, (str, bytes)):
        raise ControlContextMcpSdkBindingError("allowed-hosts-iterable-required")
    result: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or "*" in value
            or "/" in value
            or any(ord(character) <= 32 for character in value)
        ):
            raise ControlContextMcpSdkBindingError("allowed-host-invalid")
        try:
            parsed = urlsplit("//" + value)
            parsed_port = parsed.port
        except ValueError as exc:
            raise ControlContextMcpSdkBindingError("allowed-host-invalid") from exc
        if (
            parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ControlContextMcpSdkBindingError("allowed-host-invalid")
        canonical_host = (
            f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        )
        if parsed_port is not None:
            canonical_host += f":{parsed_port}"
        if value.lower() != canonical_host.lower():
            raise ControlContextMcpSdkBindingError("allowed-host-must-be-canonical")
        result.append(canonical_host.lower())
    if not result or len(set(result)) != len(result):
        raise ControlContextMcpSdkBindingError("allowed-hosts-empty-or-duplicate")
    return result


def _validated_origins(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ControlContextMcpSdkBindingError("allowed-origins-iterable-required")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ControlContextMcpSdkBindingError("allowed-origin-invalid")
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError as exc:
            raise ControlContextMcpSdkBindingError("allowed-origin-invalid") from exc
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ControlContextMcpSdkBindingError("allowed-origin-must-be-HTTPS-origin")
        result.append(value.rstrip("/"))
    if len(set(result)) != len(result):
        raise ControlContextMcpSdkBindingError("allowed-origin-duplicate")
    return result


def create_streamable_http_app(
    service: ControlContextRemoteMcpService,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allowed_origins: Iterable[str] = (),
    max_request_body_size: int = DEFAULT_MAX_REQUEST_BODY_SIZE,
    debug: bool = False,
) -> Any:
    """Create the production-directed, stateless official-SDK ASGI app."""

    if (
        not isinstance(max_request_body_size, int)
        or isinstance(max_request_body_size, bool)
        or not 1024 <= max_request_body_size <= 4 * 1024 * 1024
    ):
        raise ControlContextMcpSdkBindingError("max-request-body-size-invalid")
    if not isinstance(debug, bool):
        raise ControlContextMcpSdkBindingError("debug-boolean-required")
    _, _, TransportSecuritySettings, JSONResponse, Route, _ = _sdk_modules()
    server = create_official_mcp_server(service)
    hosts = _validated_hosts(service, allowed_hosts)
    origins = _validated_origins(allowed_origins)

    async def protected_resource_metadata(request: Any) -> Any:
        del request
        return JSONResponse(
            service.protected_resource_metadata(),
            headers={"Cache-Control": "no-store"},
        )

    async def health(request: Any) -> Any:
        del request
        value = service.readiness()
        return JSONResponse(
            value,
            status_code=200 if value.get("status") == "READY" else 503,
            headers={"Cache-Control": "no-store"},
        )

    app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=max_request_body_size,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
        custom_starlette_routes=[
            Route(PROTECTED_RESOURCE_PATH, protected_resource_metadata, methods=["GET"]),
            Route(HEALTH_PATH, health, methods=["GET"]),
        ],
        debug=debug,
    )
    return OpenAiToolSecuritySchemesMiddleware(app)
