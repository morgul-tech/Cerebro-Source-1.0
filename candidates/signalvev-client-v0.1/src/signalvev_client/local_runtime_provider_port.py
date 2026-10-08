"""Fixed BK07 protected Context transport. No token or secret is logged or retained."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
from pathlib import Path
from typing import Any

ISSUER = "https://dev-lmzknalapbdhfdua.us.auth0.com/"
CLIENT_ID = "s6Lu5katfEwbnFEoRyxq7RFvx1fG5i1v"
AUDIENCE = "https://cerebro-context-deploy-production.up.railway.app/bk07-runtime"
MCP_URL = "https://cerebro-context-deploy-production.up.railway.app/mcp"
SCOPE = "project_state:read"
SECRET_PATH = Path(r"D:\Cerebro\Run\Signalvev\BK07\normal-use\runtime-custody\auth0-client.dpapi")
ENTROPY = b"CEREBRO_BK07_RUNTIME_M2M_V1"
READ_TOOL = "read_local_runtime_binding_v1"


class PortUnavailable(RuntimeError):
    """A deliberately nonsecret failure code."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _unprotect(blob: bytes) -> bytes:
    if os.name != "nt":
        raise PortUnavailable("BK07_WINDOWS_CUSTODY_REQUIRED")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob),
    ]
    crypt.CryptUnprotectData.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    input_buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    entropy_buf = (ctypes.c_ubyte * len(ENTROPY)).from_buffer_copy(ENTROPY)
    source = _Blob(len(blob), input_buf)
    entropy = _Blob(len(ENTROPY), entropy_buf)
    output = _Blob()
    if not crypt.CryptUnprotectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0,
        ctypes.byref(output),
    ):
        raise PortUnavailable("BK07_CUSTODY_UNPROTECT_FAILED")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.memset(output.pbData, 0, output.cbData)
        kernel.LocalFree(output.pbData)


def _claim_payload(token: str) -> dict[str, Any]:
    try:
        middle = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4)))
    except (IndexError, ValueError, UnicodeError) as exc:
        raise PortUnavailable("BK07_TOKEN_SHAPE_INVALID") from exc


def _token() -> str:
    import httpx2
    try:
        secret = _unprotect(SECRET_PATH.read_bytes()).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise PortUnavailable("BK07_PROTECTED_CUSTODY_MISSING") from exc
    try:
        response = httpx2.post(
            ISSUER + "oauth/token",
            json={"grant_type": "client_credentials", "client_id": CLIENT_ID,
                  "client_secret": secret, "audience": AUDIENCE, "scope": SCOPE},
            timeout=10,
        )
    except httpx2.RequestError:
        raise PortUnavailable("BK07_TOKEN_PROVIDER_UNAVAILABLE") from None
    finally:
        secret = ""
    if response.status_code != 200:
        raise PortUnavailable("BK07_TOKEN_PROVIDER_REFUSED_" + str(response.status_code))
    try:
        value = response.json()
        token = value["access_token"]
        claims = _claim_payload(token)
        audience = claims.get("aud")
        valid_audience = audience == AUDIENCE or (isinstance(audience, list) and AUDIENCE in audience)
        if not (
            value.get("token_type", "").lower() == "bearer"
            and value.get("expires_in", 301) <= 300
            and claims.get("iss") == ISSUER
            and valid_audience
            and claims.get("azp") == CLIENT_ID
            and claims.get("sub") == CLIENT_ID + "@clients"
            and claims.get("gty") == "client-credentials"
            and claims.get("scope") == SCOPE
            and 0 < claims.get("exp", 0) - claims.get("iat", 0) <= 300
        ):
            raise PortUnavailable("BK07_TOKEN_CLAIMS_MISMATCH")
        return token
    except (KeyError, TypeError, ValueError) as exc:
        raise PortUnavailable("BK07_TOKEN_RESPONSE_INVALID") from exc


async def _call() -> dict[str, Any]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = _token()
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=15) as http:
        async with streamable_http_client(MCP_URL, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(READ_TOOL, {})
                if result.is_error:
                    raise PortUnavailable("BK07_CONTEXT_TOOL_REFUSED")
                if not isinstance(result.structured_content, dict):
                    raise PortUnavailable("BK07_CONTEXT_RESULT_UNSTRUCTURED")
                return {"structuredContent": result.structured_content}


def read_current_binding() -> dict[str, Any]:
    """Use only the protected M2M route; callers cannot choose a provider tuple."""
    try:
        result = asyncio.run(_call())
        payload = result["structuredContent"]
        if (not isinstance(payload, dict)
                or set(payload) != {"schema", "binding", "currentness",
                                        "repository_permission_required"}
                or payload["schema"] != "cerebro-local-runtime-binding-read/v1"
                or payload["currentness"] != "CURRENT"
                or payload["repository_permission_required"] is not False
                or not isinstance(payload["binding"], dict)):
            raise PortUnavailable("BK07_BINDING_READ_INVALID")
        return dict(payload["binding"])
    except PortUnavailable:
        raise
    except Exception:
        raise PortUnavailable("BK07_CONTEXT_UNAVAILABLE") from None
