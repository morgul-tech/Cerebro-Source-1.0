"""D. DEV/STAGING/PROD separation incl. symlink/alias cases, synthetic public bind / PROD activation refused,
static allowlist never returns DB/private/config bytes, secret refs for enabled vs disabled integrations."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from support import CANARY_SECRET, ServerCase, config_dict
from cerebrobase.config import ConfigError, validate_config
from cerebrobase.db import migrate
from cerebrobase.preflight import preflight


def codes(fn) -> set[str]:
    try:
        fn()
    except ConfigError as exc:
        return {p["code"] for p in exc.problems}
    return set()


class D1_ConfigSeparation(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="cb-cfg-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "public").mkdir()
        self.prod = self.d / "prod"
        (self.prod / "data" / "private").mkdir(parents=True)

    def staging(self, **over):
        c = config_dict(self.d / "stg", environment="STAGING", build_id="0123456789abcdef",
                        public_assets_root=str(self.d / "public"),
                        production_roots={"db_path": str(self.prod / "data" / "rooms.sqlite3"),
                                          "private_data_root": str(self.prod / "data" / "private"),
                                          "runtime_root": str(self.prod / "run")})
        c.update(over)
        return c

    def test_valid_staging_passes(self):
        self.assertEqual(codes(lambda: validate_config(self.staging())), set())

    def test_staging_into_prod_db_data_and_aliases_refused(self):
        self.assertIn("STAGING_POINTS_INTO_PRODUCTION",
                      codes(lambda: validate_config(self.staging(db_path=str(self.prod / "data" / "x.sqlite3")))))
        self.assertIn("STAGING_POINTS_INTO_PRODUCTION", codes(lambda: validate_config(
            self.staging(private_data_root=str(self.prod / "data" / "private" / "stg")))))
        alias = self.d / "innocent-looking"
        os.symlink(self.prod / "data", alias)                       # path alias via symlink
        self.assertIn("STAGING_POINTS_INTO_PRODUCTION", codes(lambda: validate_config(
            self.staging(private_data_root=str(alias / "private")))))
        dotted = str(self.d / "stg" / ".." / "prod" / "data" / "private")   # traversal spelling
        self.assertIn("STAGING_POINTS_INTO_PRODUCTION", codes(lambda: validate_config(
            self.staging(private_data_root=dotted))))
        parent_alias = self.d / "alias-parent"
        os.symlink(self.d, parent_alias)                            # shared-inode ancestor alias
        self.assertIn("STAGING_POINTS_INTO_PRODUCTION", codes(lambda: validate_config(
            self.staging(db_path=str(parent_alias / "prod" / "data" / "rooms.sqlite3")))))
        self.assertIn("PRODUCTION_ROOTS_REQUIRED", codes(lambda: validate_config(self.staging(production_roots={}))))

    def test_private_paths_never_in_public_root(self):
        self.assertIn("PRIVATE_PATH_IN_PUBLIC_ROOT", codes(lambda: validate_config(
            self.staging(db_path=str(self.d / "public" / "rooms.sqlite3")))))
        self.assertIn("PRIVATE_PATH_IN_PUBLIC_ROOT", codes(lambda: validate_config(
            self.staging(public_assets_root=str(self.d / "stg" / "data")))))      # public root over private data

    def test_bind_and_environment_rules(self):
        for host, code in (("0.0.0.0", "BIND_WILDCARD_REFUSED"), ("::", "BIND_WILDCARD_REFUSED"),
                           ("10.77.0.5", "SYNTHETIC_AUTH_PUBLIC_BIND_REFUSED"),
                           ("localhost", "BIND_HOST_NOT_NUMERIC_IP")):
            self.assertIn(code, codes(lambda h=host: validate_config(self.staging(bind_host=h))), host)
        prod = config_dict(self.d / "p2", environment="PROD", build_id="0123456789abcdef",
                           public_assets_root=str(self.d / "public"))
        self.assertIn("PROD_SYNTHETIC_AUTH_REFUSED", codes(lambda: validate_config(prod)))
        self.assertIn("BUILD_ID_UNPINNED", codes(lambda: validate_config(self.staging(build_id="SOURCE_TREE"))))
        self.assertIn("SECURE_COOKIE_NEEDS_HTTPS_ORIGIN", codes(lambda: validate_config(self.staging(cookie_secure=True))))
        self.assertIn("SECRET_REF_FORMAT", codes(lambda: validate_config(
            self.staging(secret_refs={"x": CANARY_SECRET}))))                     # inline secret value refused
        self.assertIn("CONFIG_FIELD_UNKNOWN", codes(lambda: validate_config(self.staging(debug=True))))

    def test_prod_external_never_ready_and_actionable(self):
        base = self.d / "p3"
        cfg = validate_config(config_dict(base, environment="PROD", build_id="0123456789abcdef", auth_mode="EXTERNAL",
                                          cookie_secure=True, public_origin="https://cerebrobase.net",
                                          public_assets_root=str(self.d / "public")))
        migrate(cfg.db_path)
        r = preflight(cfg, check_listener=False)
        got = {b["code"]: b for b in r["blocking"]}
        self.assertFalse(r["ready"])
        self.assertIn("AUTH_ADAPTER_NOT_QUALIFIED", got)
        self.assertIn("PROD_DEPLOYMENT_NOT_QUALIFIED", got)
        self.assertEqual(got["AUTH_ADAPTER_NOT_QUALIFIED"]["field"], "auth_mode")
        self.assertTrue(got["AUTH_ADAPTER_NOT_QUALIFIED"]["next"])

    def test_secret_refs_enabled_vs_disabled(self):
        base = self.d / "s"
        disabled = validate_config(config_dict(base, integrations={"postkasse": {"enabled": False, "secret_ref": "pk"}},
                                               secret_refs={"pk": "env:CB_TEST_UNSET_SECRET_REF"}))
        migrate(disabled.db_path)
        r = preflight(disabled, check_listener=False)
        self.assertTrue(r["ready"])                                               # scaffold usable
        self.assertIn({"name": "integration:postkasse", "status": "NOT_REQUIRED"},
                      [{k: i[k] for k in ("name", "status")} for i in r["items"]])
        enabled = validate_config(config_dict(base, integrations={"postkasse": {"enabled": True, "secret_ref": "pk"}},
                                              secret_refs={"pk": "env:CB_TEST_UNSET_SECRET_REF"}))
        r = preflight(enabled, check_listener=False)
        b = {x["code"]: x for x in r["blocking"]}
        self.assertEqual(b["SECRET_REF_UNRESOLVED"]["field"], "secret_refs.pk")
        os.environ["CB_TEST_SET_SECRET_REF"] = CANARY_SECRET
        self.addCleanup(os.environ.pop, "CB_TEST_SET_SECRET_REF", None)
        present = validate_config(config_dict(base, integrations={"postkasse": {"enabled": True, "secret_ref": "pk"}},
                                              secret_refs={"pk": "env:CB_TEST_SET_SECRET_REF"}))
        r = preflight(present, check_listener=False)
        self.assertIn("INTEGRATION_PORT_NOT_QUALIFIED", {x["code"] for x in r["blocking"]})   # secret != port
        self.assertNotIn(CANARY_SECRET, json.dumps(r))


class D2_StaticAllowlist(ServerCase):
    def test_static_paths_never_return_db_private_or_config(self):
        (self.cfg.private_data_root / "privat.txt").write_text("PRIVAT")
        c = self.client()
        st, h, body = c.get("/static/app.css")
        self.assertEqual((st, h["Content-Type"]), (200, "text/css; charset=utf-8"))
        db_rel = os.path.relpath(self.cfg.db_path, self.base)
        for path in ("/static/../data/db/rooms.sqlite3", "/static/%2e%2e/%2e%2e/data/db/rooms.sqlite3",
                     "/static/app.css/../../config.json", f"/{db_rel}", "/data/private/privat.txt",
                     "/rooms.sqlite3", "/static/", "/static//etc/passwd", "/.git/config", "/static/app.css%00.py"):
            st, _, body = c.get(path)
            self.assertIn(st, (303, 401, 404), path)
            self.assertNotIn(b"SQLite format 3", body)
            self.assertNotIn(b"PRIVAT", body)
            self.assertNotIn(b"config_schema", body)


if __name__ == "__main__":
    unittest.main()
