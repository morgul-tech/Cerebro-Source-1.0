"""Guards that need no database: V0.1 pin, driver parameter discipline, harness refusals, no ambient discovery.
Runs against the source tree or the installed wheel (CEE_V02_USE_INSTALLED=1) - the imported packages are what is checked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
if os.environ.get("CEE_V02_USE_INSTALLED") != "1" and SRC not in sys.path:
    sys.path.insert(0, SRC)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

import controlled_effect_executor as v01  # noqa: E402
import controlled_effect_executor_durable as v02  # noqa: E402
from controlled_effect_executor_durable import pg_libpq, schema  # noqa: E402
import pg_harness as H  # noqa: E402


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class V01IsReusedUnchanged(unittest.TestCase):
    def test_every_v01_module_is_byte_identical_to_the_hash_pinned_input_return(self) -> None:
        pin = json.load(open(os.path.join(HERE, "V01_PIN.json"), encoding="utf-8"))
        pkg = os.path.dirname(v01.__file__)
        found = {}
        for root, dirs, files in os.walk(pkg):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    found[os.path.relpath(os.path.join(root, name), pkg).replace(os.sep, "/")] = sha(os.path.join(root, name))
        self.assertEqual(found, pin["files"])
        self.assertIn("f8ff8d16d4521a818e4e8fcec223df4fd8fea97e67f1236f9f02a75219478094", pin["source"])

    def test_the_v01_regression_originals_are_the_unchanged_v01_files(self) -> None:
        pin = json.load(open(os.path.join(HERE, "V01_PIN.json"), encoding="utf-8"))
        base = os.path.join(HERE, "v01_regression", "original")
        found = {}
        for root, _dirs, files in os.walk(base):
            for name in files:
                if not name.endswith(".pyc"):
                    found[os.path.relpath(os.path.join(root, name), base).replace(os.sep, "/")] = sha(os.path.join(root, name))
        self.assertEqual(found, pin["regression_originals"])


class DriverParameterDiscipline(unittest.TestCase):
    GOOD = {"host": "/x", "port": "1", "dbname": "cee_v02_test_db", "user": "u", "password": "p"}

    def test_unknown_and_missing_parameters_are_refused_so_nothing_is_discovered(self) -> None:
        for bad in ({**self.GOOD, "service": "prod"}, {**self.GOOD, "passfile": "~/.pgpass"},
                    {k: v for k, v in self.GOOD.items() if k != "password"},
                    {k: v for k, v in self.GOOD.items() if k != "host"}, {**self.GOOD, "host": ""}):
            with self.assertRaises(pg_libpq.PgError):
                pg_libpq.conninfo(bad)

    def test_values_are_quoted_not_interpolated(self) -> None:
        text = pg_libpq.conninfo({**self.GOOD, "password": "a b'c\\d"})
        self.assertIn("password='a b\\'c\\\\d'", text)

    def test_placeholder_translation_is_strict(self) -> None:
        self.assertEqual(pg_libpq._translate("a = %s AND b = %s AND c LIKE '100%%'"), "a = $1 AND b = $2 AND c LIKE '100%'")
        with self.assertRaises(pg_libpq.PgError):
            pg_libpq._translate("SELECT '%d'")
        with self.assertRaises(pg_libpq.PgError):
            pg_libpq._translate("trailing %")

    def test_unsupported_parameter_types_are_refused(self) -> None:
        for bad in (1.5, b"x", ["a"], {"a": 1}):
            with self.assertRaises(pg_libpq.PgError):
                pg_libpq._param_text(bad)

    def test_schema_names_cannot_inject_sql(self) -> None:
        for bad in ("a; DROP TABLE x", "a-b", "", "1abc", "pg_catalog", "a" * 80, None, 5, 'a"b'):
            with self.assertRaises(schema.SchemaNameError):
                schema.validate_schema(bad)  # type: ignore[arg-type]
        self.assertEqual(schema.validate_schema("cee_v02_test_ab12cd34"), "cee_v02_test_ab12cd34")

    def test_the_package_never_reads_the_environment_or_embeds_an_endpoint(self) -> None:
        pkg = os.path.dirname(v02.__file__)
        pat = re.compile(r"\bos\.environ|\bos\.getenv|\bgetenv\s*\(|from\s+os\s+import[^\n]*\b(environ|getenv)\b"
                         r"|postgres(ql)?://|\.pgpass|DATABASE_URL")
        hits = []
        for name in sorted(os.listdir(pkg)):
            if name.endswith(".py"):
                for n, line in enumerate(open(os.path.join(pkg, name), encoding="utf-8"), 1):
                    if pat.search(line.split("#", 1)[0]):
                        hits.append((name, n))
        self.assertEqual(hits, [])

    def test_nul_bytes_are_refused_instead_of_silently_truncating_a_connection_string(self) -> None:
        with self.assertRaises(pg_libpq.PgError):
            pg_libpq.conninfo({**self.GOOD, "user": "u\0password=other"})
        with self.assertRaises(pg_libpq.PgError):
            pg_libpq._param_text("a\0b")


class HarnessRefusals(unittest.TestCase):
    def test_ambient_endpoint_variables_are_scrubbed(self) -> None:
        saved = dict(os.environ)
        try:
            os.environ.update({"PGHOST": "prod.example", "PGPASSWORD": "x", "DATABASE_URL": "postgres://u:p@h/d",
                               "SOME_DSN": "x", "KEEP_ME": "1", "PATH": os.environ.get("PATH", "")})
            removed = H.scrub_ambient_environment()
            self.assertEqual(sorted(removed), ["DATABASE_URL", "PGHOST", "PGPASSWORD", "SOME_DSN"])
            self.assertEqual(os.environ.get("KEEP_ME"), "1")
            for key in ("PGHOST", "PGPASSWORD", "DATABASE_URL", "SOME_DSN"):
                self.assertNotIn(key, os.environ)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_an_external_endpoint_must_name_a_cee_v02_test_database(self) -> None:
        saved = os.environ.get(H.ENV_CONN)
        try:
            os.environ[H.ENV_CONN] = json.dumps({"dbname": "production"})
            with self.assertRaises(RuntimeError):
                H.external_params_from_env()
            os.environ[H.ENV_CONN] = json.dumps({"dbname": "cee_v02_test_x"})
            self.assertEqual(H.external_params_from_env()["dbname"], "cee_v02_test_x")
        finally:
            if saved is None:
                os.environ.pop(H.ENV_CONN, None)
            else:
                os.environ[H.ENV_CONN] = saved

    def test_only_test_prefixed_schemas_can_be_dropped(self) -> None:
        class Boom:
            def __call__(self):
                raise AssertionError("must refuse before connecting")
        for name in ("public", "cee_prod", "cee_v02_tes", "cee_v02_test_deadbeef", "cee_v02_test_x; DROP"):
            with self.assertRaises(RuntimeError):
                H.drop_schemas(Boom(), [name])

    def test_the_runner_refuses_without_the_confirmation_flag_and_with_a_foreign_variable_name(self) -> None:
        runner = os.path.join(HERE, "run_pg_tests.py")
        env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run([sys.executable, runner], capture_output=True, text=True, env=env, cwd=tempfile.gettempdir())
        self.assertEqual((r.returncode, "refused" in r.stderr), (2, True))
        r = subprocess.run([sys.executable, runner, "--confirm-disposable-test-cluster", "--external-conn-env",
                            "DATABASE_URL"], capture_output=True, text=True, env={**env, "DATABASE_URL": "x"},
                           cwd=tempfile.gettempdir())
        self.assertEqual((r.returncode, "CEE_V02_TEST_" in r.stderr), (2, True))

    def test_no_secret_or_endpoint_literal_is_embedded_in_the_tests_or_runner(self) -> None:
        pat = re.compile(r"postgres(ql)?://[^\s'\"]+@|password\s*=\s*['\"][^'\"]{4,}['\"]")
        for name in sorted(os.listdir(HERE)):
            if name.endswith(".py") and name != "test_guards_and_pin.py":
                text = open(os.path.join(HERE, name), encoding="utf-8").read()
                self.assertIsNone(pat.search(text), name)


if __name__ == "__main__":
    unittest.main()
