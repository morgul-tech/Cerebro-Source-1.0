#!/usr/bin/env python3
"""Start ONE disposable loopback JetStream, run the real-broker integration tests, collect raw evidence, stop it.

usage (from the candidate root):
  python -B fixture/run_jetstream_tests.py --nats-server <bundled nats-server> --work <NEW private dir>
      [--gateway-root DIR --project-return-root DIR]   (or D1JS_GATEWAY_ROOT / D1JS_PROJECT_RETURN_ROOT)

* --work must not exist; it is created 0700. Only this run's subprocess/PID and paths under --work are touched.
* The server binds 127.0.0.1 on a free port with file storage under <work>/store; restarts reuse the SAME store.
* Evidence (server log, child logs, config/versions, unittest output, results.json) stays in <work>/logs and
  <work>/evidence; the server store and synthetic state under <work> are removed at the end (checked paths only).
* Exit 0 = all integration tests passed on the real broker; 1 = failures; 3 = UNRUN (no broker could be started).
  A missing binary/infrastructure is UNRUN with the exact reason -- never a stub PASS.
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

from fixture import runtime  # noqa: E402
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


def _roots(args) -> tuple[Path, Path]:
    gw = args.gateway_root or os.environ.get("D1JS_GATEWAY_ROOT")
    pr = args.project_return_root or os.environ.get("D1JS_PROJECT_RETURN_ROOT")
    local = CANDIDATE / "reference_roots.local.json"
    if (not gw or not pr) and local.is_file():
        doc = json.loads(local.read_text(encoding="utf-8"))
        gw, pr = gw or doc.get("gateway_root"), pr or doc.get("project_return_root")
    if not gw or not pr:
        raise SystemExit("REFERENCE_ROOT_NOT_CONFIGURED: pass --gateway-root/--project-return-root")
    return Path(gw).resolve(), Path(pr).resolve()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nats-server", required=True, type=Path)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--gateway-root", type=Path)
    ap.add_argument("--project-return-root", type=Path)
    ap.add_argument("--pattern", default="test_js*.py")
    args = ap.parse_args()
    gateway_root, project_return_root = _roots(args)
    work = check_new_private_dir(args.work)
    (work / "logs").mkdir()
    (work / "evidence").mkdir()
    (work / "state").mkdir()
    results: dict = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "python": sys.version.split()[0], "executable": sys.executable,
                     "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
                     "nats_server_binary": str(args.nats_server.resolve())}
    try:
        from importlib import metadata
        results["nats_py"] = metadata.version("nats-py")
    except Exception as exc:
        results["nats_py"] = f"UNAVAILABLE:{type(exc).__name__}"
    broker = None
    rc = 3
    try:
        from d1js.reference_root import check_project_return_root, load_gateway
        load_gateway(gateway_root)
        results["reference_roots"] = {"gateway": "PINNED_HASHES_OK", "project_return":
                                      check_project_return_root(project_return_root)}
        broker = BrokerProcess(args.nats_server, work)
        results["nats_server_version"] = broker.version()
        broker.start()
        results["broker"] = {"url": broker.url, "store": str(broker.store), "bind": "127.0.0.1"}
        runtime.BROKER, runtime.WORK = broker, work
        runtime.GATEWAY_ROOT, runtime.PROJECT_RETURN_ROOT = gateway_root, project_return_root
        runtime.PYTHON, runtime.CANDIDATE = sys.executable, CANDIDATE
        from d1js.config import D1Config
        results["fixture_defaults"] = D1Config().as_dict()
        out_path = work / "evidence" / "integration_unittest.txt"
        with open(out_path, "w", encoding="utf-8") as fh:
            suite = unittest.defaultTestLoader.discover(str(CANDIDATE / "tests" / "integration"), pattern=args.pattern,
                                                        top_level_dir=str(CANDIDATE / "tests" / "integration"))
            res = unittest.TextTestRunner(stream=_Tee(sys.stdout, fh), verbosity=2).run(suite)
        results["tests"] = {"run": res.testsRun, "failures": len(res.failures), "errors": len(res.errors),
                            "skipped": len(res.skipped),
                            "failed_ids": [t.id() for t, _ in res.failures + res.errors]}
        results["broker_starts"] = broker.starts
        rc = 0 if res.wasSuccessful() and res.testsRun > 0 and not res.skipped else 1
        results["result"] = "PASS_REAL_LOCAL_BROKER" if rc == 0 else "FAIL"
    except FixtureUnavailable as exc:
        results["result"] = f"UNRUN: {exc}"
        rc = 3
    finally:
        if broker is not None:
            broker.stop()
        results["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (work / "evidence" / "results.json").write_text(json.dumps(results, indent=1, default=str) + "\n",
                                                        encoding="utf-8")
        for sub in ("store", "state"):
            remove_checked(work / sub, work)
        print(json.dumps({"result": results.get("result"), "tests": results.get("tests"),
                          "evidence": str(work / "evidence"), "logs": str(work / "logs")}, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
