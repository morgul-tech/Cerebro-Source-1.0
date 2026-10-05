#!/usr/bin/env python3
"""Offline selftest runner for candidates/signalvev-client-v0.1 (SIGNALVEV_CLIENT_V01).

STATUS: isolated implementation candidate. authority: NONE.
Discovers and runs the candidate's own stdlib `unittest` suite against the SOURCE TREE (config, nats-py binding mapping,
send/listen routing into the existing receiver, C1/C2, lifecycle, CLI, guards). It opens no connection to any real
Cerebro/NATS endpoint. If nats-py is importable, `test_real_nats_py` additionally runs the real client over loopback
against a protocol STUB (a test double, not a broker); otherwise those tests are skipped and the runner says so.
The installed-package proof and the disposable-broker roundtrip are separate scripts under the candidate's packaging/.
"""
from __future__ import annotations

import argparse
import io
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = REPO_ROOT / "candidates" / "signalvev-client-v0.1"


def selftest() -> int:
    tests_dir = CANDIDATE / "tests"
    sys.dont_write_bytecode = True
    for path in (tests_dir, CANDIDATE / "src", REPO_ROOT / "candidates" / "signalvev-sensing-runtime-v0.1" / "src"):
        sys.path.insert(0, str(path))
    suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test_*.py", top_level_dir=str(tests_dir))
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    bad = result.failures + result.errors
    total = result.testsRun
    print(f"signalvev_client_v01_validation selftest: {total - len(bad)}/{total} PASS, {len(result.skipped)} skipped")
    for case, reason in result.skipped:
        print(f"  SKIP {case.id()}: {reason}")
    for case, trace in bad:
        print(f"  FAIL {case.id()}: {trace.strip().splitlines()[-1]}")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["selftest"])
    return selftest() if parser.parse_args().command == "selftest" else 2


if __name__ == "__main__":
    sys.exit(main())
