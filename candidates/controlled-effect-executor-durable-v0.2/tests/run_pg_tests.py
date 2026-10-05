#!/usr/bin/env python3
"""Runner for the real-PostgreSQL tests of the V0.2 candidate.

REQUIRES an explicit confirmation flag. By default it provisions its OWN throw-away cluster (generated password, Unix
socket in a private mkdtemp directory, no TCP listener), runs the tests against it and removes only what it created.
``--external-conn-env NAME`` instead reads ONE operator-named variable (``NAME`` must start with CEE_V02_TEST_) holding
explicit connection JSON for a disposable test database whose name starts with ``cee_v02_test``; it never reads
PG*/DATABASE_URL/*_DSN (they are scrubbed first) and never touches schemas it did not create.

  python run_pg_tests.py --confirm-disposable-test-cluster [--pattern 'test_*.py'] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if os.environ.get("CEE_V02_USE_INSTALLED") != "1":  # source-tree run; an installed-wheel run imports site-packages
    sys.path.insert(0, os.path.join(HERE, "..", "src"))

import pg_harness as H  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--confirm-disposable-test-cluster", action="store_true",
                    help="REQUIRED: I confirm this run may create and destroy a disposable test cluster/schemas")
    ap.add_argument("--external-conn-env", default=None, metavar="NAME")
    ap.add_argument("--pattern", default="test_*.py")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if not args.confirm_disposable_test_cluster:
        print("refused: pass --confirm-disposable-test-cluster (this runner never runs implicitly)", file=sys.stderr)
        return 2
    scrubbed = None
    external_raw = None
    if args.external_conn_env:
        if not args.external_conn_env.startswith("CEE_V02_TEST_"):
            print("refused: --external-conn-env must name a CEE_V02_TEST_* variable", file=sys.stderr)
            return 2
        external_raw = os.environ.get(args.external_conn_env)
        if not external_raw:
            print("refused: the named variable is not set", file=sys.stderr)
            return 2
    scrubbed = H.scrub_ambient_environment()
    if external_raw is not None:
        os.environ[H.ENV_CONN] = external_raw
    cluster = None
    try:
        if external_raw is None:
            cluster = H.DisposableCluster()
            cluster.start()
            H.ACTIVE_CLUSTER = cluster
            os.environ[H.ENV_CONN] = json.dumps(cluster.conn_params())
        params = H.external_params_from_env()
        assert params is not None
        import controlled_effect_executor as v01
        import controlled_effect_executor_durable as v02
        from controlled_effect_executor_durable.pg_libpq import make_connection_factory
        conn = make_connection_factory(params)()
        try:
            cur = conn.cursor()
            cur.execute("SHOW server_version")
            version = cur.fetchone()["server_version"]
        finally:
            conn.close()
        loader = unittest.defaultTestLoader.discover(HERE, pattern=args.pattern, top_level_dir=HERE)
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2 if args.verbose else 1).run(loader)
        skipped = sorted({reason for _, reason in result.skipped})
        summary = {
            "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "skip_reasons": skipped,
            "endpoint": "disposable-local-cluster-created-by-this-run" if cluster else "explicit-external-test-endpoint",
            "postgres_version": version, "python": platform.python_version(), "platform": platform.platform(),
            "driver": "stdlib-ctypes-libpq", "scrubbed_env_var_count": len(scrubbed),
            "controlled_effect_executor_durable": os.path.realpath(v02.__file__),
            "controlled_effect_executor": os.path.realpath(v01.__file__),
            "use_installed_flag": os.environ.get("CEE_V02_USE_INSTALLED") == "1",
        }
        print("PG_RUN_SUMMARY " + json.dumps(summary, sort_keys=True))
        return 0 if result.wasSuccessful() else 1
    finally:
        try:
            if H.CREATED_SCHEMAS and external_raw is not None:
                from controlled_effect_executor_durable.pg_libpq import make_connection_factory
                H.drop_schemas(make_connection_factory(H.external_params_from_env()), list(H.CREATED_SCHEMAS))
        finally:
            if cluster is not None:
                cluster.cleanup()
            try:
                import _pgworld
                scratch = getattr(_pgworld, "_SCRATCH", None)
                if scratch:
                    shutil.rmtree(scratch, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
