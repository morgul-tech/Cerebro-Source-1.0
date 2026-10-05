#!/usr/bin/env python3
"""Offline selftest runner for candidates/controlled-effect-executor-v0.1 (CONTROLLED_EFFECT_EXECUTOR_V01).

STATUS: isolated implementation candidate. authority: NONE.
Discovers and runs the candidate's own stdlib `unittest` suite (T1-T14 oracles, race/CAS tests, guards). It opens no
socket, starts no process, contacts no provider and writes nothing outside the interpreter.
"""
from __future__ import annotations

import argparse
import io
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = REPO_ROOT / "candidates" / "controlled-effect-executor-v0.1"


def selftest() -> int:
    tests_dir = CANDIDATE / "tests"
    sys.path.insert(0, str(tests_dir))
    sys.path.insert(0, str(CANDIDATE / "src"))
    sys.dont_write_bytecode = True
    suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test_*.py", top_level_dir=str(tests_dir))
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    bad = result.failures + result.errors
    total = result.testsRun
    print(f"controlled_effect_executor_v01_validation selftest: {total - len(bad)}/{total} PASS")
    for case, trace in bad:
        print(f"  FAIL {case.id()}: {trace.strip().splitlines()[-1]}")
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selftest"])
    return selftest() if parser.parse_args().command == "selftest" else 2


if __name__ == "__main__":
    sys.exit(main())
