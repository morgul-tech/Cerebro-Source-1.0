"""python -m cerebrobase <command>. One-shot commands; only `serve` runs until stopped.

  preflight  --config C [--phase pre-migrate|pre-start]   dependency/config report (exit 0 ready, 3 blocked)
  migrate    --config C [--target N]                      atomic forward migration to head (or N)
  seed       --config C                                   two synthetic fixtures; DEV/STAGING only; idempotent
  serve      --config C                                   loopback preview; refuses when preflight blocks
  stop       --config C [--wait S]                        graceful stop via STOP file (works on Windows and Linux)
  snapshot   --config C --out FILE                        consistent DB snapshot (new file)
  restore    --snapshot FILE --into NEW_DIR               restore into a NEW isolated root
  build      --out DIR [--label L]                        deterministic artifact + manifest
  check-manifest --artifact ZIP --manifest JSON
  install    --artifact ZIP --manifest JSON --releases DIR
  report                                                  runtime/version report (machine-specific, not in artifact)
Exit codes: 0 ok, 2 refused/failed (typed JSON on stdout), 3 not ready, 64 usage.
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
import time
from pathlib import Path

from . import CONFIG_SCHEMA, PACKAGE, SCHEMA_VERSION, SUPPORTED_PYTHON, VERSION
from .buildinfo import build_artifact, check_manifest, install_artifact, runtime_build
from .config import ConfigError, load_config
from .db import SchemaError, migrate, restore_into_new_root, seed_fixtures, snapshot
from .preflight import preflight


def _out(obj) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m cerebrobase")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "migrate", "seed", "serve", "stop", "snapshot"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "preflight":
            p.add_argument("--phase", choices=["pre-migrate", "pre-start"], default="pre-start")
        if name == "migrate":
            p.add_argument("--target", type=int, default=SCHEMA_VERSION)
        if name == "stop":
            p.add_argument("--wait", type=float, default=15.0)
        if name == "snapshot":
            p.add_argument("--out", required=True)
    p = sub.add_parser("restore")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--into", required=True)
    p = sub.add_parser("build")
    p.add_argument("--out", required=True)
    p.add_argument("--label", default="release")
    p = sub.add_parser("check-manifest")
    p.add_argument("--artifact", required=True)
    p.add_argument("--manifest", required=True)
    p = sub.add_parser("install")
    p.add_argument("--artifact", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--releases", required=True)
    sub.add_parser("report")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            man = build_artifact(Path(a.out), a.label)
            _out({k: man[k] for k in ("artifact", "artifact_sha256", "artifact_bytes", "build_id", "label")})
            return 0
        if a.cmd == "check-manifest":
            problems = check_manifest(Path(a.artifact), Path(a.manifest))
            _out({"check_manifest": "PASS" if not problems else "FAIL", "problems": problems})
            return 0 if not problems else 2
        if a.cmd == "install":
            dest = install_artifact(Path(a.artifact), Path(a.manifest), Path(a.releases))
            _out({"installed": dest.name})
            return 0
        if a.cmd == "restore":
            target = restore_into_new_root(Path(a.snapshot), Path(a.into))
            _out({"restored": target.name, "root": "NEW_ISOLATED_ROOT"})
            return 0
        if a.cmd == "report":
            b = runtime_build()
            _out({"package": PACKAGE, "version": VERSION, "build": b, "schema_version": SCHEMA_VERSION,
                  "config_schema": CONFIG_SCHEMA, "python": platform.python_version(),
                  "python_implementation": platform.python_implementation(), "platform": platform.platform(),
                  "sqlite": sqlite3.sqlite_version,
                  "supported_python": [".".join(map(str, v)) for v in SUPPORTED_PYTHON],
                  "third_party_requirements": []})
            return 0
        cfg = load_config(a.config)
        if a.cmd == "preflight":
            r = preflight(cfg, phase=a.phase)
            _out(r)
            return 0 if r["ready"] else 3
        if a.cmd == "migrate":
            _out({"migrate": migrate(cfg.db_path, target=a.target)})
            return 0
        if a.cmd == "seed":
            _out(seed_fixtures(cfg.db_path, cfg.environment))
            return 0
        if a.cmd == "snapshot":
            _out(snapshot(cfg.db_path, Path(a.out)))
            return 0
        if a.cmd == "stop":
            stop = cfg.runtime_root / "STOP"
            pid = cfg.runtime_root / "app.pid"
            if not pid.exists():
                _out({"stop": "NOT_RUNNING"})
                return 0
            stop.write_text("stop")
            deadline = time.monotonic() + a.wait
            while time.monotonic() < deadline and pid.exists():
                time.sleep(0.1)
            _out({"stop": "STOPPED" if not pid.exists() else "TIMEOUT"})
            return 0 if not pid.exists() else 2
        if a.cmd == "serve":
            r = preflight(cfg, phase="pre-start")
            if not r["ready"]:
                _out({"serve": "REFUSED_NOT_READY", "blocking": r["blocking"]})
                return 3
            from .web import App, serve
            app = App(cfg)
            _out({"serve": "STARTING", "bind": f"{cfg.bind_host}:{cfg.port}", "build_id": app.build["build_id"],
                  "environment": cfg.environment})
            return serve(app)
    except ConfigError as exc:
        _out({"error": "CONFIG_INVALID", "problems": exc.problems})
        return 2
    except SchemaError as exc:
        _out({"error": exc.code, "detail": str(exc)})
        return 2
    except (FileExistsError, ValueError) as exc:
        _out({"error": str(exc)})
        return 2
    return 64
