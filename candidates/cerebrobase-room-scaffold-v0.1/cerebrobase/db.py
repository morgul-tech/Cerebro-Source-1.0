"""Room metadata DB (SQLite, one app identity; never the Postkasse DB). Versioned forward migrations, exact schema
gate, snapshot + restore-into-new-isolated-root, explicit DEV/STAGING fixture seed.

Policy: every migration step runs in ONE transaction (BEGIN IMMEDIATE ... COMMIT) and records schema_version in the
same transaction; a failing step rolls back and leaves the prior schema usable. There is NO automatic destructive
down-migration: rollback = take a snapshot before upgrading, restore it into a NEW isolated root and point the older
release at that copy. An app refuses a DB whose schema_version differs from the one it was built for.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

from . import SCHEMA_VERSION

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class SchemaError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code + (f": {detail}" if detail else ""))
        self.code = code


def connect(db_path: Path, *, create: bool = False) -> sqlite3.Connection:
    if not create and not Path(db_path).exists():
        raise SchemaError("DB_MISSING")
    con = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    out = []
    for p in sorted(directory.iterdir()):
        m = _NAME.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    versions = [v for v, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaError("MIGRATION_SEQUENCE_BROKEN")
    return out


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def schema_version(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone()
    if row is None:
        tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if tables:
            raise SchemaError("UNVERSIONED_DB_REFUSED")
        return 0
    v = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if v is None or not str(v[0]).isdigit():
        raise SchemaError("SCHEMA_VERSION_UNREADABLE")
    return int(v[0])


def migrate(db_path: Path, *, target: int = SCHEMA_VERSION, directory: Path = MIGRATIONS_DIR) -> dict:
    steps = migrations(directory)
    if not 0 <= target <= len(steps):
        raise SchemaError("MIGRATION_TARGET_INVALID")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db_path, create=True)
    applied = []
    try:
        current = schema_version(con)
        if current > len(steps):
            raise SchemaError("SCHEMA_NEWER_THAN_APP", f"db={current} app_head={len(steps)}")
        for version, path in steps:
            if version <= current or version > target:
                continue
            sql = path.read_text(encoding="utf-8")
            con.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _statements(sql):
                    con.execute(stmt)
                con.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
                con.execute("INSERT INTO schema_meta(key, value) VALUES ('migrated_at_v' || ?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version), str(int(time.time()))))
                con.execute("COMMIT")
            except Exception as exc:
                con.execute("ROLLBACK")
                raise SchemaError("MIGRATION_FAILED_ROLLED_BACK", f"step={version} {type(exc).__name__}") from None
            applied.append(version)
        return {"from": current, "to": schema_version(con), "applied": applied}
    finally:
        con.close()


def require_compatible(con: sqlite3.Connection) -> int:
    v = schema_version(con)
    if v > SCHEMA_VERSION:
        raise SchemaError("SCHEMA_NEWER_THAN_APP", f"db={v} app={SCHEMA_VERSION}")
    if v < SCHEMA_VERSION:
        raise SchemaError("SCHEMA_MIGRATION_REQUIRED", f"db={v} app={SCHEMA_VERSION}")
    return v


# ---------------------------------------------------------------------------------------------------- fixtures
ANDREAS, MARIANNE = "acc-fixture-andreas-admin", "acc-fixture-marianne-pilot"
ADMIN_ROOM, PILOT_ROOM = "room-fixture-andreas-admin", "room-fixture-marianne-pilot"
FIXTURE_ACCOUNTS = (
    (ANDREAS, "fixture-andreas-admin", "Andreas (testidentitet)", "ADMIN"),
    (MARIANNE, "fixture-marianne-pilot", "Marianne (testidentitet)", "PILOT"),
)
FIXTURE_ROOMS = (
    (ADMIN_ROOM, "andreas-adminrom", ANDREAS, "ADMIN_PRIVATE"),
    (PILOT_ROOM, "marianne-pilotrom", MARIANNE, "PILOT_PRIVATE"),
)
FIXTURE_CAPABILITIES = ((ANDREAS, ADMIN_ROOM, "ops:read"), (ANDREAS, ADMIN_ROOM, "tool:fixture.echo"))


def seed_fixtures(db_path: Path, environment: str) -> dict:
    """Exactly two synthetic accounts and two isolated rooms. Idempotent. DEV/STAGING only; never PROD."""
    if environment not in ("DEV", "STAGING"):
        raise SchemaError("SEED_REFUSED_OUTSIDE_DEV_STAGING")
    con = connect(db_path)
    try:
        require_compatible(con)
        now = int(time.time())
        con.execute("BEGIN IMMEDIATE")
        try:
            for aid, handle, name, role in FIXTURE_ACCOUNTS:
                con.execute("INSERT OR IGNORE INTO accounts(id, handle, display_name, synthetic, created_at, app_role) "
                            "VALUES (?,?,?,1,?,?)", (aid, handle, name, now, role))
            for rid, slug, owner, kind in FIXTURE_ROOMS:
                con.execute("INSERT OR IGNORE INTO rooms(id, slug, owner_account_id, created_at, kind) VALUES (?,?,?,?,?)",
                            (rid, slug, owner, now, kind))
                con.execute("INSERT OR IGNORE INTO memberships(account_id, room_id, created_at, member_role, status) "
                            "VALUES (?,?,?,'OWNER','ACTIVE')", (owner, rid, now))
            for aid, rid, cap in FIXTURE_CAPABILITIES:
                con.execute("INSERT OR IGNORE INTO capabilities(account_id, room_id, capability, granted_at) "
                            "VALUES (?,?,?,?)", (aid, rid, cap, now))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("accounts", "rooms", "memberships", "capabilities")}
        return {"seeded": "SYNTHETIC_FIXTURES", "counts": counts}
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------------- snapshots
def snapshot(db_path: Path, dest: Path) -> dict:
    """Consistent copy via the SQLite online backup API to a NEW file (never overwrites)."""
    if dest.exists():
        raise FileExistsError("SNAPSHOT_EXISTS")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = connect(db_path)
    try:
        version = schema_version(src)
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return {"snapshot": dest.name, "schema_version": version,
            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(), "bytes": dest.stat().st_size}


def restore_into_new_root(snapshot_path: Path, new_root: Path, db_name: str = "rooms.sqlite3") -> Path:
    """Restore into a NEW isolated root (must not exist). Never writes over the live DB."""
    if new_root.exists():
        raise FileExistsError("RESTORE_ROOT_EXISTS")
    new_root.mkdir(parents=True)
    target = new_root / db_name
    if not Path(snapshot_path).is_file():
        raise SchemaError("SNAPSHOT_MISSING")
    src = sqlite3.connect(str(snapshot_path))
    try:
        out = sqlite3.connect(str(target))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    con = connect(target)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SchemaError("RESTORED_DB_INTEGRITY_FAILED")
        schema_version(con)
    finally:
        con.close()
    return target
