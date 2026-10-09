"""Provider-neutral identity boundary + the SYNTHETIC adapter (DEV/STAGING only) + server-side authorization.

AuthProvider: authenticate -> (opaque session token, csrf token); resolve(token) -> Principal | None; revoke(token).
Identity, role and membership are resolved server-side from the session; caller headers, form role, account_id or
room_id are never trusted. Tokens are random 256-bit values stored only as SHA-256; they are never logged or shown in
health. Expired, revoked, disabled-account and removed-membership sessions fail closed.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .db import FIXTURE_ACCOUNTS, connect


@dataclass(frozen=True)
class Principal:
    account_id: str
    handle: str
    display_name: str
    app_role: str
    session_id: str
    csrf_sha256: str


class AuthProvider(Protocol):
    mode: str

    def authenticate(self, identity: str) -> tuple[str, str] | None: ...
    def resolve(self, token: str | None) -> Principal | None: ...
    def revoke(self, token: str | None) -> bool: ...


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


class SyntheticAuthProvider:
    """Local/staging test identities only. Refused by config in PROD and on non-loopback binds."""
    mode = "SYNTHETIC"
    IDENTITIES = tuple(h for _, h, _, _ in FIXTURE_ACCOUNTS)

    def __init__(self, db_path: Path, ttl_seconds: int, clock: Callable[[], float] = time.time) -> None:
        self.db_path, self.ttl, self.clock = db_path, ttl_seconds, clock

    def authenticate(self, identity: str) -> tuple[str, str] | None:
        if identity not in self.IDENTITIES:
            return None
        con = connect(self.db_path)
        try:
            row = con.execute("SELECT id FROM accounts WHERE handle=? AND synthetic=1 AND status='ACTIVE'",
                              (identity,)).fetchone()
            if row is None:
                return None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = int(self.clock())
            con.execute("INSERT INTO sessions(id, token_sha256, csrf_sha256, account_id, created_at, expires_at) "
                        "VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, _sha(token), _sha(csrf), row["id"], now,
                                                 now + self.ttl))
            return token, csrf
        finally:
            con.close()

    def resolve(self, token: str | None) -> Principal | None:
        if not token or len(token) > 128:
            return None
        con = connect(self.db_path)
        try:
            row = con.execute(
                "SELECT s.id AS sid, s.csrf_sha256, s.expires_at, s.revoked_at, a.id, a.handle, a.display_name, "
                "a.app_role, a.status FROM sessions s JOIN accounts a ON a.id = s.account_id WHERE s.token_sha256=?",
                (_sha(token),)).fetchone()
        finally:
            con.close()
        if row is None or row["revoked_at"] is not None or row["status"] != "ACTIVE":
            return None
        if int(self.clock()) >= row["expires_at"]:
            return None
        return Principal(row["id"], row["handle"], row["display_name"], row["app_role"], row["sid"],
                         row["csrf_sha256"])

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        con = connect(self.db_path)
        try:
            cur = con.execute("UPDATE sessions SET revoked_at=? WHERE token_sha256=? AND revoked_at IS NULL",
                              (int(self.clock()), _sha(token)))
            return cur.rowcount == 1
        finally:
            con.close()


class ExternalAuthProviderUnqualified:
    """Placeholder for the real provider: never authenticates anything until the owner qualifies an implementation."""
    mode = "EXTERNAL"

    def authenticate(self, identity: str):
        return None

    def resolve(self, token):
        return None

    def revoke(self, token):
        return False


def csrf_ok(principal: Principal, presented: str | None) -> bool:
    return bool(presented) and len(presented) <= 128 and hmac.compare_digest(_sha(presented), principal.csrf_sha256)


# ----------------------------------------------------------------------------------- server-side authorization
def room_for(con: sqlite3.Connection, principal: Principal, room_id: str) -> sqlite3.Row | None:
    """The room only if the principal has an ACTIVE membership. Foreign and nonexistent rooms look identical."""
    if not isinstance(room_id, str) or len(room_id) > 80:
        return None
    return con.execute(
        "SELECT r.id, r.slug, r.kind, r.owner_account_id FROM rooms r JOIN memberships m ON m.room_id = r.id "
        "WHERE r.id=? AND m.account_id=? AND m.status='ACTIVE'", (room_id, principal.account_id)).fetchone()


def own_rooms(con: sqlite3.Connection, principal: Principal) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT r.id, r.slug, r.kind FROM rooms r JOIN memberships m ON m.room_id = r.id "
        "WHERE m.account_id=? AND m.status='ACTIVE' ORDER BY r.kind, r.id", (principal.account_id,)))


def has_capability(con: sqlite3.Connection, principal: Principal, room_id: str, capability: str) -> bool:
    if room_for(con, principal, room_id) is None:
        return False
    return con.execute("SELECT 1 FROM capabilities WHERE account_id=? AND room_id=? AND capability=?",
                       (principal.account_id, room_id, capability)).fetchone() is not None


def admin_room(con: sqlite3.Connection, principal: Principal) -> sqlite3.Row | None:
    """ADMIN role is an application role. It does NOT imply membership: the admin panel needs the role AND an
    ACTIVE membership in an ADMIN_PRIVATE room."""
    if principal.app_role != "ADMIN":
        return None
    for r in own_rooms(con, principal):
        if r["kind"] == "ADMIN_PRIVATE":
            return r
    return None


def audit(con: sqlite3.Connection, kind: str, outcome: str, *, account_id: str | None = None,
          room_id: str | None = None, tool_id: str | None = None, clock: Callable[[], float] = time.time) -> str:
    """Privacy-minimized: event id, time, kind, outcome, minimal ids. Never bodies, params, tokens or secret refs."""
    event_id = uuid.uuid4().hex
    con.execute("INSERT INTO audit_events(event_id, at, kind, outcome, account_id, room_id, tool_id) "
                "VALUES (?,?,?,?,?,?,?)", (event_id, int(clock()), kind, outcome, account_id, room_id, tool_id))
    return event_id
