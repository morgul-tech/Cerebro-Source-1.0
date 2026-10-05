#!/usr/bin/env python3
"""Re-runs the ORIGINAL, UNCHANGED V0.1 tests (byte-identical copies under ./original, hashes in ../V01_PIN.json's source
zip) against the V0.1 package that the V0.2 candidate carries.

  python run_v01_regression.py --mode vendored    # the package in ../../src (source tree)
  python run_v01_regression.py --mode installed   # the package imported from site-packages (the installed wheel)

It builds a throw-away tree that has V0.1's own layout (candidates/controlled-effect-executor-v0.1/{src,tests,README.md},
tooling/validator/<selftest>), because V0.1's tests locate their source relative to themselves:
* vendored: ``src`` is a copy of the vendored package -> V0.1's own selftest (76 checks) runs against it.
* installed: ``src`` is EMPTY so V0.1's own sys.path insertion finds nothing and the import resolves to site-packages.
  V0.1 checks that read source FILES (the import-boundary / no-bypass scans in test_boundary) have no source to read there
  and are reported as INAPPLICABLE-to-an-installed-artifact; every behavioural check still runs against the wheel.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ORIG = os.path.join(HERE, "original")
VENDORED = os.path.join(HERE, "..", "..", "src", "controlled_effect_executor")


def _child_exit_code(returncode: int) -> int:
    """Installed source-only scan failures remain visible; never turn a failing child green."""
    return returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("vendored", "installed"), required=True)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="cee_v01_regression_")
    try:
        cand = os.path.join(tmp, "candidates", "controlled-effect-executor-v0.1")
        os.makedirs(os.path.join(tmp, "tooling", "validator"))
        shutil.copytree(os.path.join(ORIG, "tests"), os.path.join(cand, "tests"))
        for doc in ("README.md", "ARCHITECTURE.md"):  # V0.1 doc-guards read its own documents
            shutil.copy(os.path.join(ORIG, doc), cand)
        shutil.copy(os.path.join(ORIG, "controlled_effect_executor_v01_validation.py"),
                    os.path.join(tmp, "tooling", "validator"))
        os.makedirs(os.path.join(cand, "src"))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        if args.mode == "vendored":
            shutil.copytree(VENDORED, os.path.join(cand, "src", "controlled_effect_executor"),
                            ignore=shutil.ignore_patterns("__pycache__"))
        report = {"mode": args.mode, "python": sys.version.split()[0], "interpreter": sys.executable}
        if args.mode == "installed":  # an interpreter with NO source on its path: the import can only come from site-packages
            probe = subprocess.run([sys.executable, "-c", "import controlled_effect_executor as m; print(m.__file__)"],
                                   capture_output=True, text=True, env=env, cwd=tmp)
            report["v01_package_imported_from"] = probe.stdout.strip() or ("NOT IMPORTABLE: " + probe.stderr.strip()[-120:])
        else:
            report["v01_package_imported_from"] = "scratch copy of ../../src/controlled_effect_executor (byte-identical)"
        run = subprocess.run([sys.executable, os.path.join("tooling", "validator", "controlled_effect_executor_v01_validation.py"),
                              "selftest"], capture_output=True, text=True, env=env, cwd=tmp)
        out = (run.stdout + run.stderr).strip().splitlines()
        report["selftest_summary"] = next((l for l in out if "selftest:" in l), out[-1] if out else "")
        report["failing_checks"] = [l.strip()[:200] for l in out if l.strip().startswith("FAIL")]
        report["returncode"] = run.returncode
        if args.mode == "installed":
            report["note"] = ("source-file guards cannot run without source; every FAIL listed above must be a source-only "
                              "guard (see RETURN.md ASSUMPTIONS_UNKNOWN), none may be a behavioural check")
        print(json.dumps(report, indent=1))
        sys.stdout.write("\n".join(out[-15:]) + "\n")
        return _child_exit_code(run.returncode)
    finally:
        target = os.path.realpath(tmp)
        parent = os.path.realpath(tempfile.gettempdir())
        if os.path.dirname(target) != parent or not os.path.basename(target).startswith("cee_v01_regression_"):
            raise RuntimeError("unsafe-regression-scratch-path")
        shutil.rmtree(target)


if __name__ == "__main__":
    sys.exit(main())
