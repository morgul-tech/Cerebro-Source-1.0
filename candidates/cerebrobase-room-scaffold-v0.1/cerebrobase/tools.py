"""Small explicit ToolDefinition/ToolAdapter registry. No plugin execution, URL fetch, commands or dynamic loading.

A tool may run only if ALL hold: it is QUALIFIED (an adapter exists), the environment is allowed, the config enables
it, the principal has the required app role, an ACTIVE membership in the room and the exact scoped capability.
External Cerebro tools without a supplied, attested port are UNQUALIFIED: invocation is rejected and the UI says so.
Every decision writes a redacted audit event (no params, bodies, tokens or secret refs).
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .auth import Principal, audit, has_capability, room_for

QUALIFIED, UNQUALIFIED = "QUALIFIED", "UNQUALIFIED"


@dataclass(frozen=True)
class ToolDefinition:
    tool_id: str
    title_no: str
    kind: str                         # LOCAL_SYNTHETIC_ONLY | EXTERNAL
    required_role: str
    required_capability: str
    environments: tuple[str, ...]
    status: str
    unqualified_reason: str | None = None


class ToolAdapter:
    def invoke(self, params: Mapping[str, Any]) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class FixtureEchoAdapter(ToolAdapter):
    """Harmless LOCAL_SYNTHETIC_ONLY adapter: proves an allowed invocation; returns only a length and a digest."""

    def invoke(self, params: Mapping[str, Any]) -> dict:
        text = str(params.get("tekst", ""))[:256]
        return {"outcome": "OK", "chars": len(text), "digest": hashlib.sha256(text.encode()).hexdigest()[:12]}


REGISTRY: dict[str, ToolDefinition] = {
    "fixture.echo": ToolDefinition("fixture.echo", "Testverktøy (kun lokal syntetisk)", "LOCAL_SYNTHETIC_ONLY",
                                   "ADMIN", "tool:fixture.echo", ("DEV", "STAGING"), QUALIFIED),
    "postkasse.lese": ToolDefinition("postkasse.lese", "Proto-Postkasse (lese)", "EXTERNAL", "ADMIN",
                                     "tool:postkasse.lese", ("DEV", "STAGING", "PROD"), UNQUALIFIED,
                                     "Ingen kvalifisert port/avhengighet levert"),
    "signalvev.status": ToolDefinition("signalvev.status", "Signalvev-status", "EXTERNAL", "ADMIN",
                                       "tool:signalvev.status", ("DEV", "STAGING", "PROD"), UNQUALIFIED,
                                       "Ingen kvalifisert port/avhengighet levert"),
    "drive.navigasjon": ToolDefinition("drive.navigasjon", "Drive-navigasjon", "EXTERNAL", "ADMIN",
                                       "tool:drive.navigasjon", ("DEV", "STAGING", "PROD"), UNQUALIFIED,
                                       "Ingen kvalifisert port/avhengighet levert"),
}
ADAPTERS: dict[str, ToolAdapter] = {"fixture.echo": FixtureEchoAdapter()}


def tool_view(environment: str, enabled: Mapping[str, bool]) -> list[dict]:
    out = []
    for t in REGISTRY.values():
        operative = (t.status == QUALIFIED and environment in t.environments and enabled.get(t.tool_id) is True
                     and t.tool_id in ADAPTERS)
        out.append({"tool_id": t.tool_id, "title": t.title_no, "status": "OPERATIVE" if operative else UNQUALIFIED
                    if t.status == UNQUALIFIED else "DISABLED", "reason": t.unqualified_reason})
    return out


def invoke(con: sqlite3.Connection, principal: Principal, room_id: str, tool_id: str, params: Mapping[str, Any], *,
           environment: str, enabled: Mapping[str, bool], clock: Callable[[], float]) -> tuple[int, dict]:
    t = REGISTRY.get(tool_id)

    def deny(code: str, status: int = 403) -> tuple[int, dict]:
        ev = audit(con, "TOOL_INVOKE", "DENIED:" + code, account_id=principal.account_id,
                   room_id=room_id if room_for(con, principal, room_id) else None,
                   tool_id=tool_id if t else None, clock=clock)
        return status, {"outcome": "DENIED", "code": code, "receipt": ev}

    if t is None:
        return deny("TOOL_UNKNOWN", 404)
    if t.status != QUALIFIED or tool_id not in ADAPTERS:
        return deny("TOOL_UNQUALIFIED", 409)
    if environment not in t.environments:
        return deny("TOOL_NOT_ALLOWED_IN_ENVIRONMENT")
    if enabled.get(tool_id) is not True:
        return deny("TOOL_DISABLED_BY_CONFIG")
    if principal.app_role != t.required_role:
        return deny("ROLE_REQUIRED")
    if room_for(con, principal, room_id) is None:
        return deny("ROOM_NOT_FOUND", 404)
    if not has_capability(con, principal, room_id, t.required_capability):
        return deny("CAPABILITY_REQUIRED")
    try:
        result = ADAPTERS[tool_id].invoke(params)
    except Exception:  # noqa: BLE001
        ev = audit(con, "TOOL_INVOKE", "FAILED", account_id=principal.account_id, room_id=room_id,
                   tool_id=tool_id, clock=clock)
        return 500, {"outcome": "FAILED", "receipt": ev}
    ev = audit(con, "TOOL_INVOKE", "OK", account_id=principal.account_id, room_id=room_id, tool_id=tool_id,
               clock=clock)
    return 200, {"outcome": "OK", "tool_id": tool_id, "receipt": ev, "result": result}
