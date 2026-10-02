#!/usr/bin/env python3
"""Offline selftest runner for candidates/signalvev-sensing-runtime-v0.1 (SIGNALVEV_SENSING_RUNTIME_V01).

STATUS: isolated implementation candidate. authority: NONE.
This runner only discovers and runs the candidate's own stdlib `unittest` suite (falsifiers F01-F13, hardening,
guards). It opens no socket, starts no process, and writes only to a throw-away temp dir used by the tests.
The runtime package itself IMPORTS (never copies) the v0.1/v0.9/v0.15/v0.16 reference modules from this directory.
"""
from __future__ import annotations

import argparse
import io
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = REPO_ROOT / "candidates" / "signalvev-sensing-runtime-v0.1"


def selftest() -> int:
    tests_dir = CANDIDATE / "tests"
    sys.path.insert(0, str(tests_dir))
    sys.path.insert(0, str(CANDIDATE / "src"))
    sys.dont_write_bytecode = True
    suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test_*.py", top_level_dir=str(tests_dir))
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    bad = result.failures + result.errors
    total = result.testsRun
    print(f"signalvev_sensing_runtime_v01_validation selftest: {total - len(bad)}/{total} PASS")
    for case, trace in bad:
        print(f"  FAIL {case.id()}: {trace.strip().splitlines()[-1]}")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selftest"])
    return selftest() if parser.parse_args().command == "selftest" else 2


if __name__ == "__main__":
    sys.exit(main())
