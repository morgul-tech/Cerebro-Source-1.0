"""B. empty->head and earlier synthetic schema->head keep data; failed migration leaves the prior schema; an app
refuses an incompatible (newer/older/unversioned) DB."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (path setup)
from cerebrobase import SCHEMA_VERSION
from cerebrobase.config import validate_config
from cerebrobase.db import (MIGRATIONS_DIR, SchemaError, connect, migrate, require_compatible, restore_into_new_root,
                            schema_version, seed_fixtures, snapshot)
from cerebrobase.preflight import preflight
from support import config_dict


class B_Migrations(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="cb-mig-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.db = self.d / "rooms.sqlite3"

    def rows(self, sql):
        con = sqlite3.connect(str(self.db))
        try:
            return [tuple(r) for r in con.execute(sql)]
        finally:
            con.close()

    def test_empty_to_head(self):
        out = migrate(self.db)
        self.assertEqual((out["from"], out["to"], out["applied"]), (0, SCHEMA_VERSION, [1, 2]))
        tables = {r[0] for r in self.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"accounts", "rooms", "memberships", "capabilities", "sessions", "audit_events",
                         "p02_file_metadata_reserved", "p02_quota_reserved", "p03_mirror_metadata_reserved"} <= tables)
        self.assertEqual(migrate(self.db)["applied"], [])                       # idempotent

    def test_earlier_synthetic_schema_upgrades_preserving_ids_memberships_and_least_privilege(self):
        migrate(self.db, target=1)
        con = sqlite3.connect(str(self.db))
        con.executescript("""
            INSERT INTO accounts VALUES ('acc-old-1','old-one','Gammel En',1,100);
            INSERT INTO accounts VALUES ('acc-old-2','old-two','Gammel To',1,101);
            INSERT INTO rooms VALUES ('room-old-1','old-room-1','acc-old-1',102);
            INSERT INTO memberships VALUES ('acc-old-1','room-old-1',103);""")
        con.close()
        before = self.rows("SELECT id, handle FROM accounts ORDER BY id") + self.rows("SELECT * FROM memberships")
        out = migrate(self.db)
        self.assertEqual((out["from"], out["to"]), (1, 2))
        after = self.rows("SELECT id, handle FROM accounts ORDER BY id") + self.rows(
            "SELECT account_id, room_id, created_at FROM memberships")
        self.assertEqual(before, after)
        self.assertEqual(self.rows("SELECT DISTINCT app_role FROM accounts"), [("PILOT",)])   # never inferred ADMIN
        self.assertEqual(self.rows("SELECT status FROM memberships"), [("ACTIVE",)])

    def test_failed_migration_leaves_prior_usable_schema(self):
        migrate(self.db, target=1)
        con = sqlite3.connect(str(self.db))
        con.execute("INSERT INTO accounts VALUES ('acc-keep','keep','Behold',1,1)")
        con.commit()
        con.close()
        broken = self.d / "migrations"
        shutil.copytree(MIGRATIONS_DIR, broken)
        p = broken / "0002_roles_sessions_audit_reserved.sql"
        p.write_text(p.read_text() + "\nCREATE TABLE sessions (id TEXT);\n")      # fails late in the step
        with self.assertRaises(SchemaError) as cm:
            migrate(self.db, directory=broken)
        self.assertEqual(cm.exception.code, "MIGRATION_FAILED_ROLLED_BACK")
        con = connect(self.db)
        self.assertEqual(schema_version(con), 1)
        cols = [r[1] for r in con.execute("PRAGMA table_info(accounts)")]
        self.assertNotIn("app_role", cols)                                         # no half-applied DDL
        self.assertEqual(con.execute("SELECT handle FROM accounts").fetchall()[0][0], "keep")
        con.close()
        self.assertEqual(migrate(self.db)["to"], 2)                                # the real migration still works

    def test_incompatible_schema_refused(self):
        migrate(self.db)
        con = sqlite3.connect(str(self.db))
        con.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
        con.commit()
        con.close()
        con = connect(self.db)
        with self.assertRaises(SchemaError) as cm:
            require_compatible(con)
        con.close()
        self.assertEqual(cm.exception.code, "SCHEMA_NEWER_THAN_APP")
        with self.assertRaises(SchemaError):
            migrate(self.db)
        base = self.d / "cfg"
        (base / "public").mkdir(parents=True)
        cfg = validate_config(config_dict(base, db_path=str(self.db)))
        r = preflight(cfg, check_listener=False)
        self.assertFalse(r["ready"])
        self.assertIn("SCHEMA_NEWER_THAN_APP", str(r["blocking"]))
        other = self.d / "unversioned.sqlite3"
        con = sqlite3.connect(str(other))
        con.execute("CREATE TABLE stuff(a)")
        con.commit()
        con.close()
        with self.assertRaises(SchemaError) as cm:
            migrate(other)
        self.assertEqual(cm.exception.code, "UNVERSIONED_DB_REFUSED")

    def test_snapshot_restores_only_into_new_isolated_root(self):
        migrate(self.db)
        seed_fixtures(self.db, "DEV")
        snap = self.d / "snap.sqlite3"
        info = snapshot(self.db, snap)
        self.assertEqual(info["schema_version"], 2)
        with self.assertRaises(FileExistsError):
            snapshot(self.db, snap)
        target = restore_into_new_root(snap, self.d / "restored")
        con = connect(target)
        self.assertEqual(con.execute("SELECT count(*) FROM rooms").fetchone()[0], 2)
        con.close()
        with self.assertRaises(FileExistsError):
            restore_into_new_root(snap, self.d / "restored")
        with self.assertRaises(SchemaError):
            seed_fixtures(self.db, "PROD")


if __name__ == "__main__":
    unittest.main()
