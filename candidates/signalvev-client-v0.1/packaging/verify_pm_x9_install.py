#!/usr/bin/env python3
"""BK07: prove the PM-to-X9 binding from the INSTALLED wheel, in a clean venv OUTSIDE the source checkout.

Dev/verification tool, not runtime. Installs the given signalvev_client wheel with --no-index --no-deps (nats-py is
NOT needed: the self-test injects the SYNTHETIC_TEST_ONLY transport; a real broker roundtrip is out of scope here),
then, with `python -I` from a cwd outside the checkout, checks:
  * signalvev_client / signalvev_sensing / signalvev_adapters import FROM the venv, adapters origin INSTALLED_BUNDLE;
  * every pinned file (core, references, client, both adapters) is byte-identical to its source at HEAD;
  * `signalvev-pm-x9 diagnose` with a default-off config exits 3 before any provider/network action;
  * `signalvev-pm-x9 diagnose` in PRODUCTION mode with the synthetic factory refuses (exit 3, SYNTHETIC_IN_PRODUCTION);
  * `signalvev-pm-x9 selftest` runs the whole flow to MATERIAL_ROUTE (exit 0);
  * the BK07 + guard suites pass against the INSTALLED code (SIGNALVEV_CLIENT_TEST_TARGET=installed);
  * a tampered installed adapter fails closed at import.
Writes a JSON report and removes the venv afterwards unless --keep.

usage: python3 verify_pm_x9_install.py --wheel FILE --work DIR [--report FILE] [--keep]
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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--wheel", required=True, type=Path)
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    work = args.work.resolve()
    if REPO in work.parents or work == REPO:
        raise SystemExit("--work must be OUTSIDE the source checkout")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    wheel = args.wheel.resolve()
    rep: dict[str, object] = {"platform": f"{platform.system()} {platform.release()} / Python {platform.python_version()}",
                              "wheel": {"file": wheel.name, "sha256": sha256(wheel)}}
    env_dir = work / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
    bindir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    py = bindir / ("python.exe" if sys.platform == "win32" else "python")
    inst = run([str(py), "-m", "pip", "install", "--no-index", "--no-deps", "--no-compile", str(wheel)])
    rep["install"] = {"returncode": inst.returncode, "tail": inst.stdout.strip().splitlines()[-1:] or inst.stderr[-300:]}
    outside = work / "cwd"
    outside.mkdir()
    probe = r"""
import json, hashlib, pathlib
import signalvev_client, signalvev_sensing, signalvev_adapters
from signalvev_client import pm_x9, _bootstrap, _adapters
m = json.loads((pathlib.Path(signalvev_client.__file__).parent / "_reference_resources/RESOURCE_MANIFEST.json").read_text())
print(json.dumps({"client": signalvev_client.__file__, "core": signalvev_sensing.__file__,
                  "adapters": signalvev_adapters.__file__, "adapter_module": pm_x9.pm.__file__,
                  "origin": _adapters.origin(), "bootstrap": _bootstrap.BOOTSTRAP_RESULT["mode"],
                  "pinned": m["files"], "sources": m["sources"]}))
"""
    p = run([str(py), "-I", "-B", "-c", probe], cwd=outside)
    rep["import_probe_returncode"] = p.returncode
    if p.returncode == 0:
        info = json.loads(p.stdout)
        venv_root = str(env_dir.resolve())
        rep["imported_from_venv"] = all(info[k].startswith(venv_root) for k in ("client", "core", "adapters", "adapter_module"))
        rep["adapters_origin"] = info["origin"]
        mismatches = []
        for rel, digest in info["pinned"].items():
            src = info["sources"][rel]
            if src.startswith("GENERATED"):
                continue
            blob = subprocess.run(["git", "-C", str(REPO), "show", f"HEAD:{src}"], capture_output=True).stdout
            if hashlib.sha256(blob).hexdigest() != digest:
                mismatches.append(rel)
        rep["pinned_files"] = len(info["pinned"])
        rep["pinned_equal_to_source_head"] = not mismatches
        rep["pinned_mismatches"] = mismatches
    else:
        rep["import_probe_stderr"] = p.stderr[-800:]
    script = bindir / ("signalvev-pm-x9.exe" if sys.platform == "win32" else "signalvev-pm-x9")
    off = outside / "off.toml"
    off.write_text("[pm_x9]\n", encoding="utf-8")
    d = run([str(script), "diagnose", "--config", str(off)], cwd=outside)
    rep["diagnose_default_off"] = {"returncode": d.returncode, "result": (json.loads(d.stdout).get("result") if d.stdout else None)}
    prod = outside / "prod.toml"
    prod.write_text('[pm_x9]\nmode = "PRODUCTION"\nowner_ref = "CURRENT_PM"\nclaim_ref = "WORK_CLAIMS:1"\n'
                    'packet_ref = "WORK_PACKETS:1"\nqueue_ref = "READY_QUEUE:1"\npacket_sha256 = "' + "a" * 64 + '"\n'
                    'producer_principal = "pm:producer"\nx9_principal = "x9:consumer"\nx9_session_ref = "x9:session"\n'
                    'attempt_ref = "attempt:1"\nports_factory = "signalvev_client.pm_x9_synthetic:make_ports"\n',
                    encoding="utf-8")
    d = run([str(script), "diagnose", "--config", str(prod)], cwd=outside)
    out = json.loads(d.stdout) if d.stdout else {}
    rep["diagnose_production_with_synthetic_factory"] = {
        "returncode": d.returncode, "result": out.get("result"),
        "statuses": sorted({x["status"] for x in out.get("diagnostics", [])})}
    s = run([str(script), "selftest"], cwd=outside)
    sout = json.loads(s.stdout) if s.stdout else {}
    rep["selftest"] = {"returncode": s.returncode, "result": sout.get("result"), "adapters": sout.get("adapters"),
                       "consume": sout.get("consume"), "counts": sout.get("counts")}
    tests = outside / "tests"
    shutil.copytree(CANDIDATE / "tests", tests, ignore=shutil.ignore_patterns("__pycache__"))
    env = {"SIGNALVEV_CLIENT_TEST_TARGET": "installed", "PYTHONDONTWRITEBYTECODE": "1", "PATH": str(bindir)}
    if sys.platform == "win32" and "SystemRoot" in os.environ:
        env["SystemRoot"] = os.environ["SystemRoot"]
    suites = {}
    for pattern in ("test_pm_x9.py", "test_guards.py"):
        t = run([str(py), "-B", "-m", "unittest", "discover", "-s", str(tests), "-p", pattern], cwd=outside, env=env)
        suites[pattern] = {"returncode": t.returncode, "tail": t.stderr.strip().splitlines()[-3:]}
    rep["installed_suites"] = suites
    site = Path(json.loads(run([str(py), "-I", "-c", "import json,sysconfig;print(json.dumps(sysconfig.get_paths()['purelib']))"]).stdout))
    target = site / "signalvev_adapters" / "x9_channel_ingress.py"
    original = target.read_bytes()
    target.write_bytes(original + b"\n# tampered\n")
    t = run([str(py), "-I", "-B", "-c", "import signalvev_client.pm_x9"], cwd=outside)
    target.write_bytes(original)
    rep["tampered_adapter_fails_closed"] = t.returncode != 0 and "ReferenceIntegrityError" in t.stderr
    ok = (rep.get("install", {}).get("returncode") == 0 and rep.get("imported_from_venv") is True
          and rep.get("adapters_origin") == "INSTALLED_BUNDLE" and rep.get("pinned_equal_to_source_head") is True
          and rep["diagnose_default_off"]["returncode"] == 3
          and rep["diagnose_production_with_synthetic_factory"]["returncode"] == 3
          and rep["selftest"]["returncode"] == 0 and all(v["returncode"] == 0 for v in suites.values())
          and rep["tampered_adapter_fails_closed"] is True)
    rep["result"] = "PASS" if ok else "FAIL"
    text = json.dumps(rep, indent=1, sort_keys=True, default=str)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
