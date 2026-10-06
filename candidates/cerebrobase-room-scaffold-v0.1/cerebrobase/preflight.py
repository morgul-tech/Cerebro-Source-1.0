"""Dependency + config preflight (before migrate/start) and runtime readiness.

Status values: INSTALLED_TESTED | REQUIRED_NOT_QUALIFIED | NOT_REQUIRED | UNQUALIFIED (optional tool ports).
A blocking item carries {code, field, next}. Absent optional tool ports are UNQUALIFIED and never block the scaffold.
Secret values are resolved only to test presence; they are never returned, logged or written.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import sqlite3
import sys
import uuid

from . import SCHEMA_VERSION, SUPPORTED_PYTHON
from .buildinfo import runtime_build
from .config import Config, resolve_secret
from .db import SchemaError, connect, require_compatible
from .tools import REGISTRY, UNQUALIFIED
from .postkasse import Postkasse, PostkasseError

INSTALLED_TESTED, REQUIRED_NOT_QUALIFIED, NOT_REQUIRED = "INSTALLED_TESTED", "REQUIRED_NOT_QUALIFIED", "NOT_REQUIRED"


def _item(name, status, *, blocking=False, code=None, field=None, nxt=None, **info):
    d = {"name": name, "status": status, "blocking": blocking}
    if code:
        d.update(code=code, field=field, next=nxt)
    d.update(info)
    return d


def _sqlite_features() -> tuple[bool, str]:
    try:
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("CREATE TABLE t(a INTEGER)")
        con.execute("BEGIN")
        con.execute("ALTER TABLE t ADD COLUMN b TEXT NOT NULL DEFAULT 'x' CHECK (b IN ('x','y'))")
        con.execute("ROLLBACK")
        cols = [r[1] for r in con.execute("PRAGMA table_info(t)")]
        fk = con.execute("PRAGMA foreign_keys").fetchone()[0]
        con.execute("INSERT INTO t(a) VALUES (1) ON CONFLICT DO NOTHING")
        con.close()
        return cols == ["a"] and fk == 1, "transactional DDL, foreign keys, upsert"
    except sqlite3.Error as exc:
        return False, type(exc).__name__


def _writable(root) -> bool:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / f".preflight-{uuid.uuid4().hex}"
        with open(probe, "xb") as fh:
            fh.write(b"x")
        probe.unlink()
        return True
    except OSError:
        return False


def preflight(cfg: Config, *, phase: str = "pre-start", check_listener: bool = True) -> dict:
    items = []
    py = sys.version_info[:2]
    items.append(_item("python", INSTALLED_TESTED if py in SUPPORTED_PYTHON else REQUIRED_NOT_QUALIFIED,
                       blocking=py not in SUPPORTED_PYTHON, version=platform.python_version(),
                       implementation=platform.python_implementation(),
                       **({} if py in SUPPORTED_PYTHON else {"code": "PYTHON_UNSUPPORTED", "field": "<runtime>",
                                                              "next": "use CPython 3.12 or 3.13"})))
    ok, feat = _sqlite_features()
    ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    sq_ok = ok and ver >= (3, 35, 0)
    items.append(_item("sqlite", INSTALLED_TESTED if sq_ok else REQUIRED_NOT_QUALIFIED, blocking=not sq_ok,
                       version=sqlite3.sqlite_version, features=feat,
                       **({} if sq_ok else {"code": "SQLITE_UNQUALIFIED", "field": "<runtime>",
                                            "next": "SQLite >= 3.35 with transactional DDL"})))
    for key, root in (("private_data_root", cfg.private_data_root), ("runtime_root", cfg.runtime_root),
                      ("db_path", cfg.db_path.parent)):
        w = _writable(root)
        items.append(_item(f"root:{key}", INSTALLED_TESTED if w else REQUIRED_NOT_QUALIFIED, blocking=not w,
                           **({} if w else {"code": "ROOT_NOT_WRITABLE", "field": key,
                                            "next": "create the directory owned by the app identity"})))
    try:
        free = shutil.disk_usage(cfg.private_data_root if cfg.private_data_root.exists() else cfg.runtime_root).free
    except OSError:
        free = -1
    disk_ok = free >= cfg.min_free_bytes
    items.append(_item("disk", INSTALLED_TESTED if disk_ok else REQUIRED_NOT_QUALIFIED, blocking=not disk_ok,
                       free_bytes=free, min_free_bytes=cfg.min_free_bytes,
                       **({} if disk_ok else {"code": "DISK_SPACE_LOW", "field": "min_free_bytes",
                                              "next": "free space on the private data volume"})))
    # DB / schema
    if cfg.db_path.exists():
        try:
            con = connect(cfg.db_path)
            try:
                require_compatible(con)
                items.append(_item("db", INSTALLED_TESTED, schema_version=SCHEMA_VERSION))
            finally:
                con.close()
        except SchemaError as exc:
            blocking = phase == "pre-start" or exc.code == "SCHEMA_NEWER_THAN_APP"
            items.append(_item("db", REQUIRED_NOT_QUALIFIED, blocking=blocking, code=exc.code, field="db_path",
                               nxt="run: python -m cerebrobase migrate --config <cfg>" if
                               exc.code == "SCHEMA_MIGRATION_REQUIRED" else "use the matching release or a restored copy"))
        except sqlite3.Error:
            items.append(_item("db", REQUIRED_NOT_QUALIFIED, blocking=True, code="DB_UNREADABLE", field="db_path",
                               nxt="restore a snapshot into a new isolated root"))
    else:
        items.append(_item("db", REQUIRED_NOT_QUALIFIED, blocking=phase == "pre-start", code="DB_MISSING",
                           field="db_path", nxt="run: python -m cerebrobase migrate --config <cfg>"))
    # listener
    if check_listener:
        fam = socket.AF_INET6 if ":" in cfg.bind_host else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # same semantics as the server bind
        try:
            s.bind((cfg.bind_host, cfg.port))
            items.append(_item("listener", INSTALLED_TESTED, bind=f"{cfg.bind_host}:{cfg.port}"))
        except OSError:
            items.append(_item("listener", REQUIRED_NOT_QUALIFIED, blocking=True, code="PORT_IN_USE_OR_FORBIDDEN",
                               field="port", nxt="choose a free loopback port or stop the running instance"))
        finally:
            s.close()
    # build identity
    b = runtime_build()
    build_ok = b["integrity"] in ("MATCH", "SOURCE_TREE") and (cfg.build_id == "SOURCE_TREE" and b["origin"] ==
                                                                "SOURCE_TREE" or cfg.build_id == b["build_id"])
    items.append(_item("build", INSTALLED_TESTED if build_ok else REQUIRED_NOT_QUALIFIED, blocking=not build_ok,
                       build_id=b["build_id"], integrity=b["integrity"],
                       **({} if build_ok else {"code": "BUILD_ID_MISMATCH" if b["integrity"] != "BUILD_INTEGRITY_MISMATCH"
                                               else "BUILD_INTEGRITY_MISMATCH", "field": "build_id",
                                               "next": "set build_id to the installed artifact's BUILD_INFO build_id"})))
    # auth adapter
    if cfg.auth_mode == "SYNTHETIC":
        items.append(_item("auth_adapter", INSTALLED_TESTED, mode="SYNTHETIC_TEST_IDENTITIES_ONLY"))
    else:
        items.append(_item("auth_adapter", REQUIRED_NOT_QUALIFIED, blocking=True, code="AUTH_ADAPTER_NOT_QUALIFIED",
                           field="auth_mode", nxt="owner supplies a qualified AuthProvider + session/cookie/CSRF readback"))
    if cfg.environment == "PROD":
        items.append(_item("deployment_qualification", REQUIRED_NOT_QUALIFIED, blocking=True,
                           code="PROD_DEPLOYMENT_NOT_QUALIFIED", field="environment",
                           nxt="PM effect approval + A1 qualified EDGE/Caddy deployment readback"))
    # tools
    enabled = cfg.capabilities.get("tools", {})
    for t in REGISTRY.values():
        if t.status == UNQUALIFIED:
            items.append(_item(f"tool:{t.tool_id}", UNQUALIFIED, reason="NO_ATTESTED_PORT_SUPPLIED"))
        elif enabled.get(t.tool_id) is True and cfg.environment in t.environments:
            items.append(_item(f"tool:{t.tool_id}", INSTALLED_TESTED, kind=t.kind))
        else:
            items.append(_item(f"tool:{t.tool_id}", NOT_REQUIRED, reason="DISABLED_OR_NOT_ALLOWED_IN_ENVIRONMENT"))
    # secrets / integrations
    for name, spec in cfg.integrations.items():
        ref_name = spec.get("secret_ref")
        if not spec["enabled"]:
            items.append(_item(f"integration:{name}", NOT_REQUIRED, reason="DISABLED"))
            continue
        if name == "postkasse" and "credential_reader_ref" in spec:
            try:
                Postkasse(cfg).qualify()  # read-only current /me, /contacts, inbox + CB membership
            except PostkasseError as exc:
                items.append(_item(f"integration:{name}", REQUIRED_NOT_QUALIFIED, blocking=True,
                                   code=exc.code, field="integrations.postkasse",
                                   nxt="verify the current private reader, room mapping, membership and service readback"))
            else:
                items.append(_item(f"integration:{name}", INSTALLED_TESTED, mode="PRIVATE_READ_ONLY_QUALIFIED"))
            continue
        if ref_name is None:
            items.append(_item(f"integration:{name}", REQUIRED_NOT_QUALIFIED, blocking=True,
                               code="INTEGRATION_PORT_NOT_QUALIFIED", field=f"integrations.{name}",
                               nxt="owner supplies the attested port; keep disabled until then"))
            continue
        present = resolve_secret(cfg.secret_refs[ref_name]) is not None
        items.append(_item(f"integration:{name}", REQUIRED_NOT_QUALIFIED, blocking=True,
                           code="SECRET_REF_UNRESOLVED" if not present else "INTEGRATION_PORT_NOT_QUALIFIED",
                           field=f"secret_refs.{ref_name}" if not present else f"integrations.{name}",
                           nxt="provide the secret via the referenced env var or owner file" if not present else
                           "owner supplies the attested port"))
    blocking = [i for i in items if i["blocking"]]
    return {"phase": phase, "environment": cfg.environment, "ready": not blocking, "items": items,
            "blocking": [{k: i.get(k) for k in ("name", "code", "field", "next")} for i in blocking]}


def readiness(cfg: Config) -> dict:
    """Cheap runtime readiness for /ready. Distinguishes DB failure from private-root failure."""
    checks = {}
    try:
        con = connect(cfg.db_path)
        try:
            con.execute("SELECT 1").fetchone()
            require_compatible(con)
            checks["db"] = "OK"
        finally:
            con.close()
    except SchemaError as exc:
        checks["db"] = exc.code if exc.code != "DB_MISSING" else "DB_UNAVAILABLE"
    except sqlite3.Error:
        checks["db"] = "DB_UNAVAILABLE"
    root = cfg.private_data_root
    checks["private_root"] = "OK" if root.is_dir() and os.access(root, os.R_OK | os.W_OK | os.X_OK) \
        else "PRIVATE_ROOT_UNAVAILABLE"
    b = runtime_build()
    checks["build"] = "OK" if b["integrity"] in ("MATCH", "SOURCE_TREE") and (
        (cfg.build_id == "SOURCE_TREE" and b["origin"] == "SOURCE_TREE") or cfg.build_id == b["build_id"]) \
        else "BUILD_MISMATCH"
    checks["auth_adapter"] = "OK" if cfg.auth_mode == "SYNTHETIC" and cfg.environment != "PROD" \
        else "AUTH_ADAPTER_NOT_QUALIFIED"
    return {"ready": all(v == "OK" for v in checks.values()), "checks": checks, "build_id": b["build_id"]}
