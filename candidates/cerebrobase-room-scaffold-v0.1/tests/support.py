"""Test support: temp roots, configs, an in-process loopback server and a tiny cookie-keeping HTTP client."""
from __future__ import annotations

import http.client
import json
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cerebrobase.config import validate_config  # noqa: E402
from cerebrobase.db import migrate, seed_fixtures  # noqa: E402
from cerebrobase.web import App, build_server  # noqa: E402

CANARY_BODY = "PRIVAT-KANARI-7731-ikke-logg"
CANARY_SECRET = "HEMMELIG-KANARI-5521"


def config_dict(base: Path, **over) -> dict:
    c = {"config_schema": "cerebrobase.config/v1", "environment": "DEV", "build_id": "SOURCE_TREE",
         "db_path": str(base / "data" / "db" / "rooms.sqlite3"), "private_data_root": str(base / "data" / "private"),
         "public_assets_root": str(base / "public"), "runtime_root": str(base / "run"), "bind_host": "127.0.0.1",
         "port": 18000, "public_origin": "http://127.0.0.1:18000", "auth_mode": "SYNTHETIC", "cookie_secure": False,
         "capabilities": {"tools": {"fixture.echo": True}}, "min_free_bytes": 1024}
    c.update(over)
    return c


class Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Client:
    def __init__(self, port: int, origin: str) -> None:
        self.port, self.origin, self.cookies = port, origin, {}

    def request(self, method: str, path: str, form: dict | None = None, headers: dict | None = None,
                raw_body: bytes | None = None, send_origin: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = raw_body
        if form is not None:
            body = urlencode(form).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        if method == "POST" and send_origin:
            h.setdefault("Origin", self.origin)
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                val = rest.split(";", 1)[0]
                if "Max-Age=0" in v or not val:
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = val
        conn.close()
        return r.status, dict(r.getheaders()), data

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, form=None, **kw):
        return self.request("POST", path, form=form, **kw)

    def login(self, identity: str):
        st, _, body = self.get("/logg-inn")
        pre = re.search(rb'name="prelogin" value="([^"]+)"', body).group(1).decode()
        return self.post("/logg-inn", {"identity": identity, "prelogin": pre})

    def csrf(self) -> str:
        return self.cookies.get("cb_csrf", "")


class ServerCase(unittest.TestCase):
    env_over: dict = {}

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="cb-test-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        (self.base / "public").mkdir()
        self.clock = Clock()
        self.start_server(**self.env_over)

    def start_server(self, **over) -> None:
        raw = config_dict(self.base, **over)
        self.cfg = validate_config(raw)
        self.cfg.private_data_root.mkdir(parents=True, exist_ok=True)      # preflight creates it in real starts
        migrate(self.cfg.db_path)
        seed_fixtures(self.cfg.db_path, "DEV" if self.cfg.environment == "PROD" else self.cfg.environment)
        self.app = App(self.cfg, clock=self.clock)
        self.srv = build_server_on_free_port(self.app)
        self.port = self.srv.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        # public_origin must match the real port for Origin checks
        object.__setattr__(self.cfg, "public_origin", self.origin)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def client(self) -> Client:
        return Client(self.port, self.origin)

    def logged_in(self, identity: str) -> Client:
        c = self.client()
        st, h, _ = c.login(identity)
        self.assertEqual((st, h.get("Location")), (303, "/rom"))
        return c

    def db(self):
        import sqlite3
        con = sqlite3.connect(str(self.cfg.db_path), isolation_level=None)
        con.row_factory = sqlite3.Row
        return con

    def log_text(self) -> str:
        p = self.cfg.runtime_root / "logs" / "app.jsonl"
        return p.read_text(encoding="utf-8") if p.exists() else ""


def build_server_on_free_port(app: App):
    object.__setattr__(app.cfg, "port", 0)
    return build_server(app)


def jbody(data: bytes) -> dict:
    return json.loads(data)
