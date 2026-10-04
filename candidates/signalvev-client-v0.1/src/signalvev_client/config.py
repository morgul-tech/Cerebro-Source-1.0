"""Bounded client configuration (TOML, stdlib tomllib). Offline: no network, no environment variables.

Fails CLOSED with ConfigError(code, detail). Transport credentials are only ever referenced by FILE PATH (nats
credentials file, TLS files); an inline secret key (password/token/seed/jwt/...) or a URL with embedded userinfo is
refused. A transport credential authenticates a connection; it grants no owner or PM authority.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import _bootstrap  # noqa: F401  (binds reference resources before the core is imported)
from signalvev_sensing import Interest
from signalvev_sensing.d0 import MAX_TTL_SECONDS
from signalvev_sensing.model import ID_RE

from .errors import ConfigError

MAX_CONFIG_BYTES = 64 * 1024
RESOLVER_SYNTHETIC = "synthetic_fixture"
RESOLVER_FACTORY = "factory"

_SECRET_KEY_FRAGMENTS = ("password", "passwd", "token", "secret", "seed", "nkey", "jwt", "apikey", "api_key", "private_key",
                         "user")
_ALLOWED = {
    "node": {"id"},
    "nats": {"server", "connect_timeout_seconds", "flush_timeout_seconds", "credentials_file", "tls_ca_file",
             "tls_cert_file", "tls_key_file", "tls_hostname", "allow_plaintext", "max_reconnect_attempts",
             "reconnect_wait_seconds"},
    "evidence": {"dir"},
    "send": {"ttl_seconds"},
    "listen": {"interest", "max_queue"},
    "resolver": {"kind", "fixture_file", "factory"},
}
_INTEREST_KEYS = {"owner_ref", "referent_type", "referent_id"}


def _err(code: str, detail: str = "") -> ConfigError:
    return ConfigError(code, detail)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ClientConfig:
    node_id: str
    server: str
    connect_timeout: float
    flush_timeout: float
    credentials_file: Path | None
    tls_ca_file: Path | None
    tls_cert_file: Path | None
    tls_key_file: Path | None
    tls_hostname: str | None
    allow_plaintext: bool
    max_reconnect_attempts: int
    reconnect_wait: float
    evidence_dir: Path
    ttl_seconds: int
    interests: tuple[Interest, ...]
    max_queue: int
    resolver_kind: str
    resolver_fixture: Path | None
    resolver_factory: str | None
    base_dir: Path

    @property
    def tls_requested(self) -> bool:
        return urlsplit(self.server).scheme == "tls" or any(
            p is not None for p in (self.tls_ca_file, self.tls_cert_file, self.tls_key_file))

    def public_summary(self) -> dict[str, Any]:
        """Non-secret view for health/config output. Paths are reported as configured/absent, never their contents."""
        return {
            "node_id": self.node_id,
            "server": self.server,
            "transport_security": "TLS" if self.tls_requested else "PLAINTEXT_LOOPBACK_OR_EXPLICITLY_ALLOWED",
            "credentials_file": "configured" if self.credentials_file else "absent",
            "tls_files": {"ca": bool(self.tls_ca_file), "cert": bool(self.tls_cert_file), "key": bool(self.tls_key_file)},
            "connect_timeout_seconds": self.connect_timeout,
            "flush_timeout_seconds": self.flush_timeout,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "evidence_dir": str(self.evidence_dir),
            "send_ttl_seconds": self.ttl_seconds,
            "listen_interests": len(self.interests),
            "listen_max_queue": self.max_queue,
            "resolver_kind": self.resolver_kind,
        }


def _check_keys(section: str, table: Mapping[str, Any]) -> None:
    allowed = _ALLOWED[section]
    for key in table:
        lowered = str(key).lower()
        if any(f in lowered for f in _SECRET_KEY_FRAGMENTS) and key not in allowed:
            raise _err("INLINE_SECRET_NOT_ALLOWED", f"{section}.{key}: reference credentials by file path only")
        if key not in allowed:
            raise _err("UNKNOWN_KEY", f"{section}.{key}")


def _table(doc: Mapping[str, Any], name: str, *, required: bool) -> Mapping[str, Any]:
    value = doc.get(name)
    if value is None:
        if required:
            raise _err("MISSING_SECTION", name)
        return {}
    if not isinstance(value, Mapping):
        raise _err("SECTION_NOT_A_TABLE", name)
    return value


def _number(table: Mapping[str, Any], key: str, default: float, lo: float, hi: float, where: str) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise _err("VALUE_OUT_OF_BOUNDS", f"{where}.{key} must be a number in [{lo}, {hi}]")
    return float(value)


def _integer(table: Mapping[str, Any], key: str, default: int, lo: int, hi: int, where: str) -> int:
    value = table.get(key, default)
    if type(value) is not int or not lo <= value <= hi:
        raise _err("VALUE_OUT_OF_BOUNDS", f"{where}.{key} must be an integer in [{lo}, {hi}]")
    return value


def _path(table: Mapping[str, Any], key: str, base: Path, where: str, *, must_exist: bool) -> Path | None:
    value = table.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise _err("PATH_INVALID", f"{where}.{key}")
    p = Path(value)
    p = p if p.is_absolute() else base / p
    if must_exist and not p.is_file():
        raise _err("FILE_NOT_FOUND", f"{where}.{key}")
    return p


def parse_config(doc: Mapping[str, Any], *, base_dir: Path | str = ".", check_files: bool = True) -> ClientConfig:
    if not isinstance(doc, Mapping):
        raise _err("CONFIG_NOT_A_TABLE")
    base = Path(base_dir).resolve()
    for top in doc:
        if top not in _ALLOWED:
            raise _err("UNKNOWN_SECTION", str(top))
    node, nats, ev = _table(doc, "node", required=True), _table(doc, "nats", required=True), _table(doc, "evidence", required=True)
    send, listen, resolver = _table(doc, "send", required=False), _table(doc, "listen", required=False), _table(doc, "resolver", required=False)
    for name, table in (("node", node), ("nats", nats), ("evidence", ev), ("send", send), ("listen", listen),
                        ("resolver", resolver)):
        _check_keys(name, table)

    node_id = node.get("id")
    if not (isinstance(node_id, str) and ID_RE.match(node_id)):
        raise _err("NODE_ID_INVALID", "node.id must match the Signalvev id grammar")

    server = nats.get("server")
    if not isinstance(server, str) or not server:
        raise _err("SERVER_MISSING", "nats.server")
    try:
        parts = urlsplit(server)
        port = parts.port
    except ValueError:
        raise _err("SERVER_INVALID", "nats.server is not a valid URL") from None
    if parts.scheme not in ("nats", "tls") or not parts.hostname or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise _err("SERVER_INVALID", "nats.server must be nats://host[:port] or tls://host[:port]")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _err("INLINE_SECRET_NOT_ALLOWED", "nats.server must not embed user/password; use credentials_file")
    if port is not None and not 1 <= port <= 65535:
        raise _err("SERVER_INVALID", "port")
    allow_plaintext = nats.get("allow_plaintext", False)
    if type(allow_plaintext) is not bool:
        raise _err("VALUE_OUT_OF_BOUNDS", "nats.allow_plaintext must be a boolean")

    tls_ca = _path(nats, "tls_ca_file", base, "nats", must_exist=check_files)
    tls_cert = _path(nats, "tls_cert_file", base, "nats", must_exist=check_files)
    tls_key = _path(nats, "tls_key_file", base, "nats", must_exist=check_files)
    creds = _path(nats, "credentials_file", base, "nats", must_exist=check_files)
    if (tls_cert is None) != (tls_key is None):
        raise _err("TLS_CERT_KEY_PAIR_INCOMPLETE", "tls_cert_file and tls_key_file go together")
    tls_requested = parts.scheme == "tls" or any(p is not None for p in (tls_ca, tls_cert, tls_key))
    if not tls_requested and not _is_loopback(parts.hostname) and not allow_plaintext:
        raise _err("PLAINTEXT_NON_LOOPBACK_REFUSED",
                   "use tls:// (or TLS files) for a non-loopback server, or set allow_plaintext=true explicitly")
    tls_hostname = nats.get("tls_hostname")
    if tls_hostname is not None and not (isinstance(tls_hostname, str) and 0 < len(tls_hostname) <= 253):
        raise _err("VALUE_OUT_OF_BOUNDS", "nats.tls_hostname")

    ev_dir = ev.get("dir")
    if not isinstance(ev_dir, str) or not ev_dir or "\x00" in ev_dir:
        raise _err("EVIDENCE_DIR_MISSING", "evidence.dir")
    ev_path = Path(ev_dir)
    ev_path = ev_path if ev_path.is_absolute() else base / ev_path
    if ev_path.exists() and not ev_path.is_dir():
        raise _err("EVIDENCE_DIR_NOT_A_DIRECTORY", "evidence.dir")

    interests: list[Interest] = []
    raw_interests = listen.get("interest", [])
    if not isinstance(raw_interests, list) or len(raw_interests) > 64:
        raise _err("VALUE_OUT_OF_BOUNDS", "listen.interest must be a list of at most 64 tables")
    for i, item in enumerate(raw_interests):
        if not isinstance(item, Mapping) or not set(item) <= _INTEREST_KEYS or not {"owner_ref", "referent_type"} <= set(item):
            raise _err("INTEREST_INVALID", f"listen.interest[{i}] needs owner_ref + referent_type (+ optional referent_id)")
        rid = item.get("referent_id")
        if not (ID_RE.match(str(item["owner_ref"])) and ID_RE.match(str(item["referent_type"]))
                and (rid is None or ID_RE.match(str(rid)))):
            raise _err("INTEREST_INVALID", f"listen.interest[{i}] id grammar")
        interests.append(Interest(item["owner_ref"], item["referent_type"], rid))

    kind = resolver.get("kind", RESOLVER_SYNTHETIC)
    fixture = _path(resolver, "fixture_file", base, "resolver", must_exist=False)
    factory = resolver.get("factory")
    if kind == RESOLVER_SYNTHETIC:
        if factory is not None:
            raise _err("RESOLVER_INVALID", "factory is only valid with kind = 'factory'")
        if check_files and fixture is not None and not fixture.is_file():
            raise _err("FILE_NOT_FOUND", "resolver.fixture_file")
    elif kind == RESOLVER_FACTORY:
        if not (isinstance(factory, str) and factory.count(":") == 1 and all(factory.split(":"))):
            raise _err("RESOLVER_INVALID", "resolver.factory must be 'package.module:callable'")
        if fixture is not None:
            raise _err("RESOLVER_INVALID", "fixture_file is only valid with kind = 'synthetic_fixture'")
    else:
        raise _err("RESOLVER_INVALID", "resolver.kind must be 'synthetic_fixture' or 'factory'")

    return ClientConfig(
        node_id=node_id, server=server,
        connect_timeout=_number(nats, "connect_timeout_seconds", 5.0, 0.1, 60.0, "nats"),
        flush_timeout=_number(nats, "flush_timeout_seconds", 2.0, 0.1, 60.0, "nats"),
        credentials_file=creds, tls_ca_file=tls_ca, tls_cert_file=tls_cert, tls_key_file=tls_key, tls_hostname=tls_hostname,
        allow_plaintext=allow_plaintext,
        max_reconnect_attempts=_integer(nats, "max_reconnect_attempts", 30, 0, 1000, "nats"),
        reconnect_wait=_number(nats, "reconnect_wait_seconds", 2.0, 0.1, 60.0, "nats"),
        evidence_dir=ev_path,
        ttl_seconds=_integer(send, "ttl_seconds", 30, 1, MAX_TTL_SECONDS, "send"),
        interests=tuple(interests), max_queue=_integer(listen, "max_queue", 256, 1, 10_000, "listen"),
        resolver_kind=kind, resolver_fixture=fixture, resolver_factory=factory, base_dir=base)


def load_config(path: Path | str, *, check_files: bool = True) -> ClientConfig:
    p = Path(path)
    try:
        if p.stat().st_size > MAX_CONFIG_BYTES:
            raise _err("CONFIG_TOO_LARGE", f"> {MAX_CONFIG_BYTES} bytes")
        raw = p.read_bytes()
    except OSError:
        raise _err("CONFIG_UNREADABLE", p.name) from None
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _err("CONFIG_NOT_VALID_TOML", type(exc).__name__) from None
    return parse_config(doc, base_dir=p.resolve().parent, check_files=check_files)
