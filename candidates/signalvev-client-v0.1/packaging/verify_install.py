#!/usr/bin/env python3
"""Prove INSTALLATION in a clean environment OUTSIDE the source checkout. Dev/verification tool, not runtime.

Creates a fresh venv in a work dir, installs the given wheels with --no-index --no-deps (a local wheelhouse: the
dependency wheel(s), e.g. nats-py, are supplied by the caller), then -- from a cwd outside the checkout, with
`python -I` (no PYTHONPATH, no user site, no cwd on sys.path) -- checks that:
  * signalvev_client / signalvev_sensing / nats are imported FROM the venv, never from the checkout;
  * every file pinned in RESOURCE_MANIFEST.json is byte-identical to its repository source (core + references);
  * the console script works (--version, check-config, health without network);
  * a tampered copy of the installed core fails closed at import (REFERENCE_INTEGRITY);
  * the client unit suite passes against the INSTALLED code (SIGNALVEV_CLIENT_TEST_TARGET=installed).
It writes a JSON report. Platform tested = the platform it runs on (venv layout handled for POSIX and Windows).

usage: python3 verify_install.py --wheelhouse DIR --work DIR [--report FILE] [--expect-sha256 HASH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
REPO = CANDIDATE.parents[1]


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--wheelhouse", required=True, type=Path)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--expect-sha256", help="expected SHA-256 of the signalvev_client wheel (the delivered one)")
    args = ap.parse_args()
    work = args.work.resolve()
    if REPO in work.parents or work == REPO:
        raise SystemExit("--work must be OUTSIDE the source checkout")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    checks: dict[str, object] = {"platform": f"{platform.system()} {platform.release()} / Python {platform.python_version()}"}

    wheels = sorted(args.wheelhouse.glob("*.whl"))
    client_wheel = next(w for w in wheels if w.name.startswith("signalvev_client-"))
    checks["client_wheel"] = {"file": client_wheel.name, "sha256": sha256(client_wheel)}
    if args.expect_sha256:
        checks["client_wheel"]["matches_expected"] = sha256(client_wheel) == args.expect_sha256
    venv_dir = work / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    py = bindir / ("python.exe" if os.name == "nt" else "python")
    pip = run([str(py), "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", *map(str, wheels)])
    checks["pip_install"] = {"returncode": pip.returncode, "tail": pip.stdout.strip().splitlines()[-1:] or pip.stderr.strip().splitlines()[-1:]}
    if pip.returncode:
        print(json.dumps({"RESULT": "FAIL", "checks": checks}, indent=1))
        return 1

    probe = ("import json,sys,signalvev_client,signalvev_sensing,nats,importlib.metadata as m;"
             "from signalvev_client import _bootstrap as b;"
             "print(json.dumps({'client':signalvev_client.__file__,'core':signalvev_sensing.__file__,'nats':nats.__file__,"
             "'prefix':sys.prefix,'bootstrap':b.BOOTSTRAP_RESULT,'client_version':m.version('signalvev-client'),"
             "'nats_py_version':m.version('nats-py'),'python':sys.version.split()[0]}))")
    out = run([str(py), "-I", "-c", probe], cwd=work)
    info = json.loads(out.stdout) if out.returncode == 0 else {"error": out.stderr[-800:]}
    from_venv = out.returncode == 0 and all(Path(info[k]).resolve().is_relative_to(venv_dir.resolve()) for k in ("client", "core", "nats"))
    checks["imports"] = {**info, "all_from_clean_venv_not_checkout": from_venv}

    manifest = json.loads(next((venv_dir.rglob("RESOURCE_MANIFEST.json"))).read_text())
    site_root = next(venv_dir.rglob("signalvev_client")).parent
    mismatches = []
    for rel, digest in manifest["files"].items():
        installed, source = site_root / rel, REPO / manifest["sources"][rel]
        if not installed.is_file() or sha256(installed) != digest or not source.is_file() or sha256(source) != digest:
            mismatches.append(rel)
    checks["pinned_files"] = {"count": len(manifest["files"]), "installed_equals_repo_source": not mismatches, "mismatches": mismatches,
                              "source_commit": manifest["source_commit"]}

    cfgdir = work / "node"
    cfgdir.mkdir()
    for name in ("node-listen.example.toml", "node-send.example.toml", "owner.synthetic.json", "event.example.json"):
        shutil.copyfile(CANDIDATE / "examples" / name, cfgdir / name)
    exe = bindir / ("signalvev-client.exe" if os.name == "nt" else "signalvev-client")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON") and k != "SIGNALVEV_VALIDATOR_DIR"}
    cli = {}
    for label, argv in (("version", ["--version"]),
                        ("check_config_listen", ["check-config", "--config", str(cfgdir / "node-listen.example.toml")]),
                        ("check_config_send", ["check-config", "--config", str(cfgdir / "node-send.example.toml")]),
                        ("health_offline", ["health", "--config", str(cfgdir / "node-send.example.toml")])):
        r = run([str(exe), *argv], cwd=work, env=env)
        cli[label] = {"returncode": r.returncode, "ok": r.returncode == 0, "stdout_head": r.stdout.strip()[:160]}
    checks["console_script"] = cli

    tamper = work / "tamper"
    shutil.copytree(site_root / "signalvev_client", tamper / "signalvev_client")
    shutil.copytree(site_root / "signalvev_sensing", tamper / "signalvev_sensing")
    victim = tamper / "signalvev_sensing" / "receiver.py"
    victim.write_bytes(victim.read_bytes() + b"\n# tampered\n")
    t = run([str(py), "-I", "-c", f"import sys; sys.path.insert(0, {str(tamper)!r}); import signalvev_client"], cwd=work)
    checks["tamper_detection"] = {"import_failed_closed": t.returncode != 0 and "ReferenceIntegrityError" in t.stderr}

    tests = run([str(py), "-I", "-m", "unittest", "discover", "-s", str(CANDIDATE / "tests"), "-p", "test_*.py"], cwd=work,
                env={**env, "SIGNALVEV_CLIENT_TEST_TARGET": "installed", "PYTHONDONTWRITEBYTECODE": "1"})
    last = [ln for ln in tests.stderr.strip().splitlines() if ln.strip()][-3:]
    checks["installed_unit_suite"] = {"returncode": tests.returncode, "tail": last}

    ok = (checks["pip_install"]["returncode"] == 0 and from_venv and not mismatches and all(v["ok"] for v in cli.values())
          and checks["tamper_detection"]["import_failed_closed"] and tests.returncode == 0
          and checks["client_wheel"].get("matches_expected", True))
    report = {"RESULT": "PASS" if ok else "FAIL", "INSTALLABLE_LOCALLY": "YES" if ok else "NO", "checks": checks}
    text = json.dumps(report, indent=1)
    print(text)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
