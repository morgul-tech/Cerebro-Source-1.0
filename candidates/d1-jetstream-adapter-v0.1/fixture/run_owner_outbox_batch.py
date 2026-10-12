#!/usr/bin/env python3
"""BK11 owner-outbox batch: ONE disposable PostgreSQL cluster + ONE loopback JetStream, run cases 1-9 + SQL policy.

usage (from the candidate root):
  python -B fixture/run_owner_outbox_batch.py --nats-server <nats-server> --context-root <Context checkout>
      --work <NEW private dir> --gateway-root DIR --project-return-root DIR
      [--pg-bin /usr/lib/postgresql/16/bin] [--pg-run-as postgres]

* --work must not exist (created 0700). Only this run's own PostgreSQL cluster/nats-server PIDs and paths under
  --work are touched; both bind 127.0.0.1 on free ports. No production endpoint, credential or EDGE broker.
* Each test gets a fresh database with the ACTUAL Context migrations (0001-0008) via the Context migration runner and
  the Context runtime-role provisioning; the stream is reset per test.
* Evidence (versions, server/PG logs, child logs, unittest output, per-case JSON, results.json) stays under
  <work>/logs and <work>/evidence. The broker store, PG cluster and synthetic state are removed at the end.
* Exit 0 = every case PASS on real PG + real broker; 1 = failures; 3 = UNRUN (infrastructure missing, exact reason).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import platform
import sys
import time
import unittest
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE))
sys.path.insert(0, str(CANDIDATE / "tests" / "integration_owner"))

from fixture import owner_runtime as rt  # noqa: E402
from fixture.broker import BrokerProcess, FixtureUnavailable, check_new_private_dir, remove_checked  # noqa: E402


class _Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            if not st.closed:
                st.write(s)
        return len(s)

    def flush(self):
        for st in self.streams:
            if not st.closed:
                st.flush()


class _Result(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.per_case: dict[str, dict] = {}
        self._t0: dict[str, float] = {}

    def startTest(self, test):
        self._t0[test.id()] = time.monotonic()
        super().startTest(test)

    def _mark(self, test, status, detail=None):
        self.per_case[test.id()] = {"status": status, "seconds": round(time.monotonic() -
                                                                         self._t0.get(test.id(), time.monotonic()), 3),
                                    **({"detail": detail[-3000:]} if detail else {})}

    def addSuccess(self, test):
        super().addSuccess(test)
        self._mark(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test, "FAIL", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._mark(test, "UNRUN" if str(reason).startswith("UNRUN") else "SKIP", str(reason))


def _versions(results: dict) -> None:
    from importlib import metadata
    for dist in ("nats-py", "PyJWT", "cryptography"):
        try:
            results.setdefault("python_packages", {})[dist] = metadata.version(dist)
        except Exception as exc:
            results.setdefault("python_packages", {})[dist] = f"UNAVAILABLE:{type(exc).__name__}"
    try:
        import psycopg
        results["python_packages"]["psycopg"] = {
            "module_file": psycopg.__file__, "reported___version__": psycopg.__version__,
            "libpq": psycopg.pq.version(), "impl": psycopg.pq.__impl__,
            "note": "pure-Python psycopg vendored from the upstream git tag (see RESOURCE notes); "
                    "__version__ is the unbuilt-source placeholder"}
    except Exception as exc:
        results["python_packages"]["psycopg"] = f"UNAVAILABLE:{type(exc).__name__}:{exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nats-server", required=True, type=Path)
    ap.add_argument("--context-root", required=True, type=Path)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--gateway-root", required=True, type=Path)
    ap.add_argument("--project-return-root", required=True, type=Path)
    ap.add_argument("--pg-bin", type=Path, default=Path(os.environ.get("D1JS_PG_BIN", "/usr/lib/postgresql/16/bin")))
    ap.add_argument("--pg-run-as", default=os.environ.get("D1JS_PG_RUN_AS") or None,
                    help="OS user owning the disposable cluster (required when running as root)")
    ap.add_argument("--pattern", default="test_owner*.py")
    ap.add_argument("-k", dest="only", default=None, help="substring filter on test ids (diagnostics only)")
    args = ap.parse_args()
    work = check_new_private_dir(args.work)
    for sub in ("logs", "evidence", "state"):
        (work / sub).mkdir()
    results: dict = {"batch": "BK11_OWNER_OUTBOX_INTEGRATION", "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                            time.gmtime()),
                     "python": sys.version.split()[0], "executable": sys.executable,
                     "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
                     "nats_server_binary": str(args.nats_server.resolve()), "context_root":
                     str(args.context_root.resolve()), "pg_bin": str(args.pg_bin)}
    _versions(results)
    broker = pg = None
    rc = 3
    try:
        if sys.version_info < (3, 12):
            raise FixtureUnavailable(f"PYTHON_TOO_OLD: {sys.version.split()[0]} (requires >=3.12)")
        try:
            import psycopg  # noqa: F401
        except Exception as exc:
            raise FixtureUnavailable(f"PSYCOPG_UNAVAILABLE: {exc}") from exc
        if hasattr(os, "geteuid") and os.geteuid() == 0 and not args.pg_run_as:
            raise FixtureUnavailable("PG_RUN_AS_REQUIRED_WHEN_ROOT")
        if not (args.pg_bin / ("initdb.exe" if os.name == "nt" else "initdb")).is_file():
            raise FixtureUnavailable(f"POSTGRES_BINARIES_MISSING: {args.pg_bin}")
        from d1js.reference_root import check_project_return_root, load_gateway
        load_gateway(args.gateway_root.resolve())
        results["reference_roots"] = {"gateway": "PINNED_HASHES_OK", "project_return":
                                      check_project_return_root(args.project_return_root.resolve())}
        from fixture.context_pg import ContextPostgres, PgUnavailable
        pg = ContextPostgres(work, args.context_root.resolve(), bin_dir=args.pg_bin, run_as=args.pg_run_as)
        results["postgres_version"] = pg.version()
        try:
            pg.start()
        except PgUnavailable as exc:
            raise FixtureUnavailable(str(exc)) from exc
        results["postgres"] = {"bind": "127.0.0.1", "port": pg.port, "auth": "scram-sha-256",
                               "roles": vars(pg.roles)}
        broker = BrokerProcess(args.nats_server, work)
        results["nats_server_version"] = broker.version()
        broker.start()
        results["broker"] = {"url": broker.url, "bind": "127.0.0.1", "store": str(broker.store)}
        rt.PG, rt.BROKER, rt.WORK = pg, broker, work
        rt.GATEWAY_ROOT, rt.PROJECT_RETURN_ROOT = args.gateway_root.resolve(), args.project_return_root.resolve()
        rt.PYTHON, rt.CONTEXT_ROOT = sys.executable, args.context_root.resolve()
        out_path = work / "evidence" / "owner_unittest.txt"
        with open(out_path, "w", encoding="utf-8") as fh:
            top = CANDIDATE / "tests" / "integration_owner"
            suite = unittest.defaultTestLoader.discover(str(top), pattern=args.pattern, top_level_dir=str(top))
            if args.only:
                flat, stack = [], [suite]
                while stack:
                    s = stack.pop()
                    for t in s:
                        (stack.append(t) if isinstance(t, unittest.TestSuite) else flat.append(t))
                suite = unittest.TestSuite(sorted((t for t in flat if args.only in t.id()), key=lambda t: t.id()))
            runner = unittest.TextTestRunner(stream=_Tee(sys.stdout, fh), verbosity=2, resultclass=_Result)
            res = runner.run(suite)
        results["tests"] = {"run": res.testsRun, "failures": len(res.failures), "errors": len(res.errors),
                            "skipped": len(res.skipped),
                            "failed_ids": [t.id() for t, _ in res.failures + res.errors]}
        results["per_case"] = res.per_case
        results["broker_starts"] = broker.starts
        rc = 0 if res.wasSuccessful() and res.testsRun > 0 and not res.skipped else 1
        results["result"] = "PASS_REAL_LOCAL_PG_AND_BROKER" if rc == 0 else "FAIL"
    except FixtureUnavailable as exc:
        results["result"] = f"UNRUN: {exc}"
        rc = 3
    finally:
        cleanup: dict = {}
        if broker is not None:
            broker.stop()
            cleanup["broker_process_stopped"] = broker.proc is None
        if pg is not None:
            pg.stop()
            cleanup["postgres_stopped"] = not pg.started
        for sub in ("store", "state", "pg"):
            target = work / sub
            try:
                remove_checked(target, work)
                cleanup[f"removed_{sub}"] = not target.exists()
            except Exception as exc:   # e.g. a cluster dir owned by the run-as user
                cleanup[f"removed_{sub}"] = f"FAILED:{type(exc).__name__}:{exc}"
        results["cleanup"] = cleanup
        results["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (work / "evidence" / "results.json").write_text(json.dumps(results, indent=1, default=str) + "\n",
                                                        encoding="utf-8")
        print(json.dumps({"result": results.get("result"), "tests": results.get("tests"),
                          "cleanup": cleanup, "evidence": str(work / "evidence")}, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
