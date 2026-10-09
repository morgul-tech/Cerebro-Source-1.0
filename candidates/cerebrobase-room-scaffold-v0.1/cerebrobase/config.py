"""Configuration schema `cerebrobase.config/v1` + validation of the REAL config (not a checklist).

Errors are typed prerequisite codes: {"code", "field", "next"} with the exact field and the minimal next operation.
Path rules: every root is resolved (realpath, also for not-yet-existing paths via the nearest existing ancestor);
DB/private/runtime roots never inside the public assets root and vice versa; STAGING roots never equal to, inside,
or an inode alias of a configured PROD root. Synthetic auth only on a numeric loopback bind (no "localhost" spelling,
no wildcard). PROD refuses the synthetic adapter and seeding, and is never ready without a qualified real auth adapter.
Secret values only through `env:NAME` or `file:/abs/path` references, never inline.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import CONFIG_SCHEMA

ENVIRONMENTS = ("DEV", "STAGING", "PROD")
AUTH_MODES = ("SYNTHETIC", "EXTERNAL")
_SECRET_REF = re.compile(r"^(env:[A-Z][A-Z0-9_]{0,63}|file:/[^\x00]{1,1024}|file:[A-Za-z]:[\\/][^\x00]{1,1024})$")
_KEYS = {"config_schema", "environment", "build_id", "db_path", "private_data_root", "public_assets_root",
         "runtime_root", "bind_host", "port", "public_origin", "auth_mode", "cookie_secure", "session_ttl_seconds",
         "secret_refs", "capabilities", "integrations", "production_roots", "limits", "min_free_bytes"}
_REQUIRED = {"config_schema", "environment", "build_id", "db_path", "private_data_root", "public_assets_root",
             "runtime_root", "bind_host", "port", "public_origin", "auth_mode", "cookie_secure"}
DEFAULT_LIMITS = {"max_body_bytes": 16384, "rate_per_minute": 240, "max_header_bytes": 8192}


class ConfigError(ValueError):
    def __init__(self, problems: list[dict]) -> None:
        super().__init__(";".join(p["code"] for p in problems))
        self.problems = problems


def _p(code: str, field: str, nxt: str) -> dict:
    return {"code": code, "field": field, "next": nxt}


def resolve_path(p: str) -> Path:
    """Realpath for existing and not-yet-existing paths (resolves symlinks of the existing ancestors)."""
    return Path(os.path.realpath(os.path.abspath(p)))


def _existing_ancestor(p: Path) -> Path | None:
    cur = p
    while True:
        if cur.exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def is_within(child: Path, parent: Path) -> bool:
    """Path containment after resolution, plus inode-alias detection over existing ancestors."""
    c, pr = resolve_path(str(child)), resolve_path(str(parent))
    if c == pr or pr in c.parents:
        return True
    if pr.exists():
        cur = _existing_ancestor(c)
        while cur is not None:
            try:
                if os.path.samefile(cur, pr):
                    return True
            except OSError:
                pass
            if cur.parent == cur:
                break
            cur = cur.parent
    return False


def overlaps(a: Path, b: Path) -> bool:
    return is_within(a, b) or is_within(b, a)


@dataclass(frozen=True)
class Config:
    raw: dict
    environment: str
    build_id: str
    db_path: Path
    private_data_root: Path
    public_assets_root: Path
    runtime_root: Path
    bind_host: str
    port: int
    public_origin: str
    auth_mode: str
    cookie_secure: bool
    session_ttl_seconds: int
    secret_refs: dict
    capabilities: dict
    integrations: dict
    production_roots: dict
    limits: dict = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    min_free_bytes: int = 64 * 1024 * 1024

    @property
    def synthetic(self) -> bool:
        return self.auth_mode == "SYNTHETIC"


def load_config(path: str | os.PathLike[str]) -> Config:
    try:
        with open(path, "rb") as fh:
            data = fh.read(65537)
        if len(data) > 65536:
            raise ValueError
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_no_dup, parse_constant=_no_const)
    except (OSError, ValueError, UnicodeDecodeError):
        raise ConfigError([_p("CONFIG_UNREADABLE", "<file>", "supply a readable UTF-8 JSON config <= 64 KiB")]) from None
    return validate_config(raw)


def _no_dup(items):
    out = {}
    for k, v in items:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


def _no_const(_):
    raise ValueError("non-finite")


def validate_config(raw: Any) -> Config:
    probs: list[dict] = []
    if type(raw) is not dict:
        raise ConfigError([_p("CONFIG_NOT_OBJECT", "<root>", "supply a JSON object")])
    for k in sorted(_REQUIRED - set(raw)):
        probs.append(_p("CONFIG_FIELD_MISSING", k, f"add '{k}'"))
    for k in sorted(set(raw) - _KEYS):
        probs.append(_p("CONFIG_FIELD_UNKNOWN", k, f"remove '{k}'"))
    if probs:
        raise ConfigError(probs)
    if raw["config_schema"] != CONFIG_SCHEMA:
        probs.append(_p("CONFIG_SCHEMA_UNSUPPORTED", "config_schema", f"set '{CONFIG_SCHEMA}'"))
    env = raw["environment"]
    if env not in ENVIRONMENTS:
        probs.append(_p("ENVIRONMENT_INVALID", "environment", "use DEV, STAGING or PROD"))
    if raw["auth_mode"] not in AUTH_MODES:
        probs.append(_p("AUTH_MODE_INVALID", "auth_mode", "use SYNTHETIC (DEV/STAGING) or EXTERNAL"))
    if type(raw["port"]) is not int or not 1024 <= raw["port"] <= 65535:
        probs.append(_p("PORT_INVALID", "port", "use an unprivileged port 1024-65535"))
    if type(raw["cookie_secure"]) is not bool:
        probs.append(_p("COOKIE_SECURE_INVALID", "cookie_secure", "true behind qualified HTTPS, false only for HTTP loopback"))
    if type(raw["build_id"]) is not str or not re.fullmatch(r"[0-9a-f]{16}|SOURCE_TREE", raw["build_id"]):
        probs.append(_p("BUILD_ID_INVALID", "build_id", "set the installed artifact build_id (or SOURCE_TREE in DEV)"))
    elif raw["build_id"] == "SOURCE_TREE" and env != "DEV":
        probs.append(_p("BUILD_ID_UNPINNED", "build_id", "pin the exact artifact build_id outside DEV"))
    paths = {}
    for k in ("db_path", "private_data_root", "public_assets_root", "runtime_root"):
        v = raw[k]
        if type(v) is not str or not v or "\x00" in v or not os.path.isabs(v):
            probs.append(_p("PATH_NOT_ABSOLUTE", k, "use an absolute path"))
        else:
            paths[k] = resolve_path(v)
    ttl = raw.get("session_ttl_seconds", 3600)
    if type(ttl) is not int or not 60 <= ttl <= 43200:
        probs.append(_p("SESSION_TTL_INVALID", "session_ttl_seconds", "use 60..43200 seconds"))
    limits = {**DEFAULT_LIMITS, **(raw.get("limits") or {})}
    if set(limits) != set(DEFAULT_LIMITS) or not all(type(v) is int and 1 <= v <= 1_048_576 for v in limits.values()):
        probs.append(_p("LIMITS_INVALID", "limits", "max_body_bytes/rate_per_minute/max_header_bytes ints 1..1048576"))
    # ---- bind / origin -----------------------------------------------------------------------------------------
    host = raw["bind_host"]
    try:
        ip = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        ip = None
        probs.append(_p("BIND_HOST_NOT_NUMERIC_IP", "bind_host", "use a numeric IP (127.0.0.1 or ::1 for staging)"))
    if ip is not None:
        if ip.is_unspecified:
            probs.append(_p("BIND_WILDCARD_REFUSED", "bind_host", "bind 127.0.0.1; public exposure only via Caddy"))
        elif raw["auth_mode"] == "SYNTHETIC" and not ip.is_loopback:
            probs.append(_p("SYNTHETIC_AUTH_PUBLIC_BIND_REFUSED", "bind_host", "bind a loopback IP for synthetic auth"))
    origin = raw["public_origin"]
    if type(origin) is not str or not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]{1,253}", origin):
        probs.append(_p("PUBLIC_ORIGIN_INVALID", "public_origin", "scheme://host[:port] without path"))
    elif raw["cookie_secure"] is True and not origin.startswith("https://"):
        probs.append(_p("SECURE_COOKIE_NEEDS_HTTPS_ORIGIN", "public_origin", "use https:// origin or cookie_secure=false on HTTP loopback"))
    elif raw["cookie_secure"] is False and env != "DEV" and not (
            ip is not None and ip.is_loopback and (origin.startswith("http://127.") or origin.startswith("http://[::1]"))):
        probs.append(_p("INSECURE_COOKIE_OUTSIDE_LOOPBACK", "cookie_secure", "set true behind qualified HTTPS"))
    # ---- environment-specific auth rules -----------------------------------------------------------------------
    if env == "PROD" and raw["auth_mode"] == "SYNTHETIC":
        probs.append(_p("PROD_SYNTHETIC_AUTH_REFUSED", "auth_mode", "PROD needs a qualified EXTERNAL AuthProvider"))
    # ---- root separation ---------------------------------------------------------------------------------------
    if len(paths) == 4:
        pub = paths["public_assets_root"]
        for k in ("db_path", "private_data_root", "runtime_root"):
            if overlaps(paths[k], pub):
                probs.append(_p("PRIVATE_PATH_IN_PUBLIC_ROOT", k, "move it outside public_assets_root"))
        if overlaps(paths["private_data_root"], paths["runtime_root"]):
            probs.append(_p("PRIVATE_ROOT_OVERLAPS_RUNTIME", "runtime_root", "use separate private data and runtime roots"))
        prod = raw.get("production_roots") or {}
        if env == "STAGING":
            if not prod:
                probs.append(_p("PRODUCTION_ROOTS_REQUIRED", "production_roots",
                                "declare the PROD db_path/private_data_root/runtime_root so staging can be checked"))
            for pk, pv in prod.items():
                if type(pv) is not str or not os.path.isabs(pv):
                    probs.append(_p("PRODUCTION_ROOT_INVALID", f"production_roots.{pk}", "absolute path"))
                    continue
                # Keep the production DB file identity as well as its directory boundary.
                # A hard link in a different staging directory still opens the same DB.
                if pk == "db_path" and is_within(paths["db_path"], Path(pv)):
                    probs.append(_p("STAGING_POINTS_INTO_PRODUCTION", "db_path",
                                    "use an isolated staging DB file, not a production file alias"))
                proot = Path(pv).parent if pk.endswith("db_path") else Path(pv)   # a DB file guards its directory
                for k in ("db_path", "private_data_root", "runtime_root"):
                    if overlaps(paths[k], proot):
                        probs.append(_p("STAGING_POINTS_INTO_PRODUCTION", k, f"use an isolated staging path, not {pk}"))
    # ---- secret refs / integrations ----------------------------------------------------------------------------
    secret_refs = raw.get("secret_refs") or {}
    if type(secret_refs) is not dict:
        probs.append(_p("SECRET_REFS_INVALID", "secret_refs", "object of name -> env:NAME|file:/abs"))
        secret_refs = {}
    for name, ref in secret_refs.items():
        if type(ref) is not str or not _SECRET_REF.match(ref):
            probs.append(_p("SECRET_REF_FORMAT", f"secret_refs.{name}", "use env:NAME or file:/abs/path (no inline value)"))
    integrations = raw.get("integrations") or {}
    if type(integrations) is not dict:
        probs.append(_p("INTEGRATIONS_INVALID", "integrations", "object"))
        integrations = {}
    for name, spec in integrations.items():
        if type(spec) is not dict or type(spec.get("enabled")) is not bool:
            probs.append(_p("INTEGRATION_INVALID", f"integrations.{name}", "{enabled: bool, secret_ref?: name}"))
            continue
        ref = spec.get("secret_ref")
        if ref is not None and ref not in secret_refs:
            probs.append(_p("INTEGRATION_SECRET_REF_UNDECLARED", f"integrations.{name}.secret_ref",
                            "declare it in secret_refs"))
    caps = raw.get("capabilities") or {"tools": {}}
    if type(caps) is not dict or type(caps.get("tools", {})) is not dict:
        probs.append(_p("CAPABILITIES_INVALID", "capabilities", "{tools: {tool_id: bool}}"))
        caps = {"tools": {}}
    mfb = raw.get("min_free_bytes", 64 * 1024 * 1024)
    if type(mfb) is not int or mfb < 0:
        probs.append(_p("MIN_FREE_BYTES_INVALID", "min_free_bytes", "non-negative int"))
    if probs:
        raise ConfigError(probs)
    return Config(raw=raw, environment=env, build_id=raw["build_id"], db_path=paths["db_path"],
                  private_data_root=paths["private_data_root"], public_assets_root=paths["public_assets_root"],
                  runtime_root=paths["runtime_root"], bind_host=host, port=raw["port"], public_origin=origin,
                  auth_mode=raw["auth_mode"], cookie_secure=raw["cookie_secure"], session_ttl_seconds=ttl,
                  secret_refs=secret_refs, capabilities=caps, integrations=integrations,
                  production_roots=raw.get("production_roots") or {}, limits=limits, min_free_bytes=mfb)


def resolve_secret(ref: str) -> str | None:
    """Returns the secret value or None if unresolved. Callers must never log or return the value."""
    if ref.startswith("env:"):
        return os.environ.get(ref[4:]) or None
    if ref.startswith("file:"):
        try:
            with open(ref[5:], "r", encoding="utf-8") as fh:
                v = fh.read(4097).strip()
            return (v or None) if len(v) <= 4096 else None
        except OSError:
            return None
    return None
