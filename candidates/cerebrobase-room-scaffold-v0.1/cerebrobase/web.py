"""Bounded loopback HTTP server for local/private-staging preview (NOT a publicly qualified production server).

stdlib http.server.ThreadingHTTPServer + one handler. Bounded body/header, socket timeouts, a concurrency cap, a
per-client token-bucket rate limit, request IDs, typed JSON/HTML errors, structured redacted JSON logs, an explicit
static allowlist (one in-memory CSS file; never a filesystem path), security headers, server-side session/role/
membership resolution, CSRF + Origin checks on every mutating request, and graceful stop via signal or STOP file.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import signal
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from . import SCHEMA_VERSION, ui
from .auth import (ExternalAuthProviderUnqualified, Principal, SyntheticAuthProvider, admin_room, audit, csrf_ok,
                   has_capability, own_rooms, room_for)
from .buildinfo import runtime_build
from .config import Config
from .db import connect
from .interfaces import P02_FILES, P03_MIRRORS, UNAVAILABLE
from .preflight import readiness
from .postkasse import Postkasse, PostkasseError
from .tools import invoke as invoke_tool
from .tools import tool_view

STATIC = {"/static/app.css": (Path(__file__).resolve().parent / "static" / "app.css", "text/css; charset=utf-8")}
_ROOM = re.compile(r"^/rom/([A-Za-z0-9_-]{1,80})$")
_API = re.compile(r"^/api/rom/([A-Za-z0-9_-]{1,80})/(filer|speil)$")
_TOOL = re.compile(r"^/admin/verktoy/([a-z0-9_.-]{1,64})$")
_POSTKASSE = re.compile(r"^/rom/([A-Za-z0-9_-]{1,80})/postkasse(?:/([A-Za-z0-9_-]{1,128})(?:/(ack|reply))?)?$")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin"}
MAX_CONCURRENCY = 32


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float]) -> None:
        self.rate, self.clock, self.lock, self.buckets = per_minute / 60.0, clock, threading.Lock(), {}
        self.capacity = float(per_minute)

    def allow(self, key: str) -> bool:
        with self.lock:
            now = self.clock()
            tokens, last = self.buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if len(self.buckets) > 4096:
                self.buckets.clear()                          # bounded memory
            if tokens < 1.0:
                self.buckets[key] = (tokens, now)
                return False
            self.buckets[key] = (tokens - 1.0, now)
            return True


class JsonLog:
    """Structured, redacted: never cookies, tokens, bodies, query strings, params or secret refs."""
    ALLOWED = {"t", "rid", "event", "method", "route", "status", "ms", "acct", "code", "build_id", "env"}

    def __init__(self, path: Path | None) -> None:
        self.path, self.lock = path, threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **fields) -> None:
        rec = {k: v for k, v in fields.items() if k in self.ALLOWED}
        rec["t"] = int(time.time())
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
        if self.path is not None:
            with self.lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)


class App:
    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.clock = cfg, clock
        self.auth = (SyntheticAuthProvider(cfg.db_path, cfg.session_ttl_seconds, clock) if cfg.auth_mode == "SYNTHETIC"
                     and cfg.environment != "PROD" else ExternalAuthProviderUnqualified())
        self.limiter = RateLimiter(cfg.limits["rate_per_minute"], time.monotonic)
        self.log = JsonLog(cfg.runtime_root / "logs" / "app.jsonl")
        self.build = runtime_build()
        self.postkasse = Postkasse(cfg)
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENCY)

    def db(self) -> sqlite3.Connection:
        return connect(self.cfg.db_path)


def make_handler(app: App):
    cfg = app.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "cerebrobase-scaffold"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 10

        # ------------------------------------------------------------------ plumbing
        def log_message(self, *args):          # silence default stderr access log (it would include paths/ip)
            return

        def _send(self, status: int, body: bytes, ctype: str, *, headers: dict | None = None, cookies=()):
            self._status = status
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", self.rid)
            self.send_header("Cache-Control", "no-store")
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            for c in cookies:
                self.send_header("Set-Cookie", c)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, status: int, obj: dict, **kw):
            self._send(status, json.dumps({**obj, "request_id": self.rid}, sort_keys=True).encode(),
                       "application/json; charset=utf-8", **kw)

        def html(self, status: int, title: str, body: str, principal=None, csrf=None, admin_link=False, **kw):
            doc = ui.page(title, body, env=cfg.environment, build_id=app.build["build_id"], principal=principal,
                          csrf=csrf, admin_link=admin_link)
            self._send(status, doc.encode("utf-8"), "text/html; charset=utf-8", **kw)

        def redirect(self, location: str, cookies=()):
            self._send(303, b"", "text/plain; charset=utf-8", headers={"Location": location}, cookies=cookies)

        def cookie(self, name: str) -> str | None:
            raw = self.headers.get("Cookie", "")
            if len(raw) > 4096:
                return None
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == name and v:
                    return v
            return None

        def set_cookie(self, name: str, value: str, max_age: int) -> str:
            sec = "; Secure" if cfg.cookie_secure else ""
            return f"{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{sec}"

        def principal(self) -> Principal | None:
            return app.auth.resolve(self.cookie("cb_session"))

        def csrf_plain(self, p: Principal) -> str | None:
            c = self.cookie("cb_csrf")
            return c if c and csrf_ok(p, c) else None

        def origin_ok(self) -> bool:
            origin, ref = self.headers.get("Origin"), self.headers.get("Referer")
            if origin is not None:
                return origin == cfg.public_origin
            if ref is not None:
                return ref == cfg.public_origin or ref.startswith(cfg.public_origin + "/")
            return True                                    # no Origin/Referer: CSRF token still required

        def form(self) -> dict | None:
            n = self.headers.get("Content-Length")
            if n is None or not n.isdigit():
                self.json(411, {"error": "LENGTH_REQUIRED"})
                return None
            n = int(n)
            if n > cfg.limits["max_body_bytes"]:
                self.close_connection = True
                self.json(413, {"error": "BODY_TOO_LARGE", "max_bytes": cfg.limits["max_body_bytes"]})
                return None
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
            body = self.rfile.read(n)
            if ctype != "application/x-www-form-urlencoded":
                self.json(415, {"error": "UNSUPPORTED_MEDIA_TYPE"})
                return None
            try:
                parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False,
                                  max_num_fields=20)
            except (UnicodeDecodeError, ValueError):
                self.json(400, {"error": "FORM_INVALID"})
                return None
            return {k: v[0] for k, v in parsed.items()}

        # ------------------------------------------------------------------ dispatch
        def _dispatch(self, method: str):
            self.rid = uuid.uuid4().hex[:16]
            self._status, self._acct, self._route = 0, None, "?"
            t0 = time.monotonic()
            if not app.slots.acquire(blocking=False):
                self.close_connection = True
                self.json(503, {"error": "BUSY"})
                return self._log(t0)
            try:
                client = self.client_address[0] if self.client_address else "?"
                if not app.limiter.allow(client):
                    self._route = "rate-limited"
                    self.close_connection = True
                    self.json(429, {"error": "RATE_LIMITED"}, headers={"Retry-After": "60"})
                    return
                path = self.path.split("?", 1)[0]
                if len(self.path) > 2048:
                    self.json(414, {"error": "URI_TOO_LONG"})
                    return
                try:
                    getattr(self, f"_{method}")(path)
                except PostkasseError as exc:
                    self.json(exc.status, {"error": exc.code})
                except sqlite3.Error:
                    self._route = self._route or "?"
                    self.json(503, {"error": "DB_UNAVAILABLE"})
            except Exception:  # noqa: BLE001 - never leak internals
                if not self._status:
                    self.json(500, {"error": "INTERNAL_ERROR"})
            finally:
                app.slots.release()
                self._log(t0)

        def _log(self, t0):
            app.log.write(event="http", rid=self.rid, method=self.command, route=self._route, status=self._status,
                          ms=round((time.monotonic() - t0) * 1000, 2), acct=self._acct)

        def do_GET(self):
            self._dispatch("get")

        def do_HEAD(self):
            self._dispatch("get")

        def do_POST(self):
            self._dispatch("post")

        def _other(self):
            self.rid = uuid.uuid4().hex[:16]
            self._status, self._acct, self._route = 0, None, "method"
            self.json(405, {"error": "METHOD_NOT_ALLOWED"}, headers={"Allow": "GET, HEAD, POST"})
            self._log(time.monotonic())

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = _other

        # ------------------------------------------------------------------ GET routes
        def _get(self, path: str):
            if path == "/health":
                self._route = "/health"
                return self.json(200, {"status": "alive", "build_id": app.build["build_id"]})
            if path == "/ready":
                self._route = "/ready"
                r = readiness(cfg)
                return self.json(200 if r["ready"] else 503,
                                 {"status": "ready" if r["ready"] else "not_ready", "build_id": r["build_id"]})
            if path in STATIC:
                self._route = path
                fpath, ctype = STATIC[path]
                return self._send(200, fpath.read_bytes(), ctype)
            if path == "/logg-inn":
                self._route = "/logg-inn"
                if cfg.auth_mode != "SYNTHETIC" or cfg.environment == "PROD":
                    return self.html(503, "Innlogging", ui.message_body(
                        "Innlogging er ikke klar", "Ingen kvalifisert innloggingsleverandør er satt opp."))
                # Keep the browser's pending double-submit token stable across
                # parallel login pages/prefetch redirects. A second GET must not
                # invalidate a form already displayed in the same cookie jar.
                pre = self.cookie("cb_prelogin")
                if not pre or not re.fullmatch(r"[A-Za-z0-9_-]{32}", pre):
                    pre = secrets.token_urlsafe(24)
                return self.html(200, "Testinngang", ui.login_body(pre),
                                 cookies=[self.set_cookie("cb_prelogin", pre, 600)])
            p = self.principal()
            if p is None:
                self._route = "unauthenticated"
                if path.startswith("/api/") or path.endswith(".json"):
                    return self.json(401, {"error": "AUTH_REQUIRED"})
                return self.redirect("/logg-inn")
            self._acct = p.account_id
            csrf = self.csrf_plain(p)
            con = app.db()
            try:
                is_admin = admin_room(con, p) is not None
                if path in ("/", "/rom"):
                    self._route = "/rom"
                    rooms = own_rooms(con, p)
                    if not rooms:
                        return self.html(404, "Ingen rom", ui.message_body("Ingen rom", "Du har ikke noe aktivt rom."),
                                         principal=p, csrf=csrf)
                    pick = next((r for r in rooms if r["kind"] == "ADMIN_PRIVATE"), rooms[0]) if is_admin else rooms[0]
                    return self.redirect(f"/rom/{pick['id']}")
                m = _ROOM.match(path)
                if m:
                    self._route = "/rom/{id}"
                    room = room_for(con, p, m.group(1))
                    if room is None:                            # foreign == missing: no existence oracle
                        return self.html(404, "Fant ikke", ui.message_body("Fant ikke rommet",
                                                                           "Rommet finnes ikke for deg."),
                                         principal=p, csrf=csrf, admin_link=is_admin)
                    return self.html(200, "Mitt rom", ui.room_body(room, p, is_admin_room=room["kind"] ==
                                                                   "ADMIN_PRIVATE", postkasse_enabled=
                                                                   app.postkasse.configured(room["id"])), principal=p, csrf=csrf,
                                     admin_link=is_admin)
                m = _POSTKASSE.match(path)
                if m:
                    self._route = "/rom/{id}/postkasse" + ("/{message}" if m.group(2) else "")
                    room = room_for(con, p, m.group(1))
                    if room is None:
                        return self.json(404, {"error": "NOT_FOUND"})
                    if m.group(3):
                        return self.json(404, {"error": "NOT_FOUND"})
                    if m.group(2):
                        message = app.postkasse.message(room["id"], m.group(2))
                        body = ui.postkasse_message_body(room["id"], message, csrf or "")
                    else:
                        messages, contacts = app.postkasse.mailbox(room["id"])
                        body = ui.postkasse_body(room["id"], messages, contacts, csrf or "")
                    return self.html(200, "Postkasse", body, principal=p, csrf=csrf, admin_link=is_admin)
                m = _API.match(path)
                if m:
                    self._route = "/api/rom/{id}/" + m.group(2)
                    if room_for(con, p, m.group(1)) is None:
                        return self.json(404, {"error": "NOT_FOUND"})
                    return self.json(503, {"error": UNAVAILABLE, "capability": P02_FILES if m.group(2) == "filer"
                                           else P03_MIRRORS})
                if path == "/admin":
                    self._route = "/admin"
                    ar = admin_room(con, p)
                    if ar is None:
                        audit(con, "ADMIN_ENTRY", "DENIED", account_id=p.account_id, clock=app.clock)
                        return self.html(403, "Ingen tilgang", ui.message_body("Ingen tilgang",
                                                                               "Denne siden krever adminrollen."),
                                         principal=p, csrf=csrf)
                    return self._admin_page(con, p, ar, csrf, 200, None)
                if path == "/admin/drift.json":
                    self._route = "/admin/drift.json"
                    ar = admin_room(con, p)
                    if ar is None or not has_capability(con, p, ar["id"], "ops:read"):
                        return self.json(403, {"error": "FORBIDDEN"})
                    return self.json(200, self._ops(con))
                self._route = "not-found"
                return self.html(404, "Fant ikke", ui.message_body("Fant ikke siden", "Siden finnes ikke."),
                                 principal=p, csrf=csrf, admin_link=is_admin)
            finally:
                con.close()

        def _ops(self, con) -> dict:
            r = readiness(cfg)
            return {"environment": cfg.environment, "build_id": app.build["build_id"], "schema_version": SCHEMA_VERSION,
                    "ready": r["ready"], "checks": r["checks"],
                    "rooms": con.execute("SELECT count(*) FROM rooms").fetchone()[0],
                    "accounts": con.execute("SELECT count(*) FROM accounts").fetchone()[0]}

        def _admin_page(self, con, p, ar, csrf, status, result):
            tools = tool_view(cfg.environment, cfg.capabilities.get("tools", {}))
            ops = self._ops(con) if has_capability(con, p, ar["id"], "ops:read") else {
                "environment": cfg.environment, "build_id": app.build["build_id"], "schema_version": "-",
                "ready": False, "checks": {}, "rooms": "-", "accounts": "-"}
            return self.html(status, "Admininngang", ui.admin_body(tools, ops, csrf or "", result), principal=p,
                             csrf=csrf, admin_link=True)

        # ------------------------------------------------------------------ POST routes
        def _post(self, path: str):
            if not self.origin_ok():
                self._route = "foreign-origin"
                return self.json(403, {"error": "ORIGIN_REJECTED"})
            if path == "/logg-inn":
                self._route = "/logg-inn"
                f = self.form()
                if f is None:
                    return
                pre = self.cookie("cb_prelogin")
                if not pre or not hmac.compare_digest(pre, f.get("prelogin", "")):
                    return self.json(403, {"error": "CSRF_REJECTED"})
                pair = app.auth.authenticate(f.get("identity", ""))
                if pair is None:
                    return self.html(401, "Testinngang", ui.login_body(pre, "Ukjent testidentitet."))
                token, csrf = pair
                p = app.auth.resolve(token)
                self._acct = p.account_id if p else None
                return self.redirect("/rom", cookies=[self.set_cookie("cb_session", token, cfg.session_ttl_seconds),
                                                      self.set_cookie("cb_csrf", csrf, cfg.session_ttl_seconds),
                                                      self.set_cookie("cb_prelogin", "", 0)])
            p = self.principal()
            if p is None:
                self._route = "unauthenticated"
                return self.json(401, {"error": "AUTH_REQUIRED"})
            self._acct = p.account_id
            f = self.form()
            if f is None:
                return
            if not csrf_ok(p, f.get("csrf")):
                self._route = "csrf-rejected"
                return self.json(403, {"error": "CSRF_REJECTED"})
            if path == "/logg-ut":
                self._route = "/logg-ut"
                app.auth.revoke(self.cookie("cb_session"))
                return self.redirect("/logg-inn", cookies=[self.set_cookie("cb_session", "", 0),
                                                           self.set_cookie("cb_csrf", "", 0)])
            m = _POSTKASSE.match(path)
            if m:
                self._route = "/rom/{id}/postkasse/" + (m.group(3) or "send")
                con = app.db()
                try:
                    room = room_for(con, p, m.group(1))
                    if room is None:
                        return self.json(404, {"error": "NOT_FOUND"})
                    # A client-supplied sender/room selector is never an authority input.
                    if any(k in f for k in ("room_id", "sender_room_id", "recipient_room_id")):
                        return self.json(400, {"error": "POSTKASSE_SEND_REJECTED"})
                    if m.group(3) == "ack":
                        app.postkasse.ack(room["id"], m.group(2))
                        return self.redirect(f"/rom/{room['id']}/postkasse/{m.group(2)}")
                    if m.group(3) == "reply":
                        app.postkasse.reply(room["id"], m.group(2), f.get("tekst", ""), f.get("dedupe_key", ""))
                        return self.redirect(f"/rom/{room['id']}/postkasse")
                    if m.group(2):
                        return self.json(404, {"error": "NOT_FOUND"})
                    app.postkasse.send(room["id"], f.get("recipient", ""), f.get("tekst", ""),
                                       f.get("dedupe_key", ""))
                    return self.redirect(f"/rom/{room['id']}/postkasse")
                finally:
                    con.close()
            m = _TOOL.match(path)
            if m:
                self._route = "/admin/verktoy/{id}"
                con = app.db()
                try:
                    ar = admin_room(con, p)
                    room_id = ar["id"] if ar is not None else (own_rooms(con, p) or [{"id": "-"}])[0]["id"]
                    status, res = invoke_tool(con, p, room_id, m.group(1), {"tekst": f.get("tekst", "")},
                                              environment=cfg.environment,
                                              enabled=cfg.capabilities.get("tools", {}), clock=app.clock)
                    if ar is None:
                        return self.json(status if status != 200 else 403, {"outcome": res["outcome"],
                                                                            "code": res.get("code"),
                                                                            "receipt": res["receipt"]})
                    msg = (f"Testverktøy kjørt (syntetisk). Kvittering {res['receipt']}." if res["outcome"] == "OK"
                           else f"Avvist: {res.get('code')}. Kvittering {res['receipt']}.")
                    return self._admin_page(con, p, ar, f.get("csrf"), status, msg)
                finally:
                    con.close()
            self._route = "not-found"
            return self.json(404, {"error": "NOT_FOUND"})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX: SO_REUSEADDR only skips TIME_WAIT (a live listener still blocks). Windows: never SO_REUSEADDR
    # (it would allow port hijacking); use SO_EXCLUSIVEADDRUSE instead.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """Never print tracebacks (paths, internals) to stderr; a dropped/garbled connection is just closed."""
        return

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def build_server(app: App) -> Server:
    import socket
    Server.address_family = socket.AF_INET6 if ":" in app.cfg.bind_host else socket.AF_INET
    return Server((app.cfg.bind_host, app.cfg.port), make_handler(app))


def serve(app: App, *, stop_poll: float = 0.25) -> int:
    """Blocking; graceful stop on SIGTERM/SIGINT or when <runtime_root>/STOP appears. Writes/removes a pidfile."""
    rt = app.cfg.runtime_root
    rt.mkdir(parents=True, exist_ok=True)
    stop_file, pid_file = rt / "STOP", rt / "app.pid"
    if stop_file.exists():
        stop_file.unlink()
    srv = build_server(app)
    srv.timeout = stop_poll
    stopping = threading.Event()

    def _sig(*_):
        stopping.set()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass
    pid_file.write_text(json.dumps({"pid": os.getpid(), "build_id": app.build["build_id"], "port": app.cfg.port}))
    app.log.write(event="start", build_id=app.build["build_id"], env=app.cfg.environment)
    try:
        while not stopping.is_set() and not stop_file.exists():
            srv.handle_request()
    finally:
        srv.server_close()
        app.log.write(event="stop", build_id=app.build["build_id"], env=app.cfg.environment)
        for f in (pid_file, stop_file):
            try:
                f.unlink()
            except OSError:
                pass
    return 0
