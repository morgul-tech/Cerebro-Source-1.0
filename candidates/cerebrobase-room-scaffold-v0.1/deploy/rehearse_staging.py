"""Private-staging install/upgrade/rollback REHEARSAL on synthetic isolated local roots (no host mutation, no SSH,
no service install, loopback only). Real EDGE install is NOT performed and never inferred from this run.

usage: python deploy/rehearse_staging.py --source <candidate root> --work <NEW dir> [--python <exe>]
Writes <work>/rehearsal.json and prints it. Exit 0 when every step reads back as expected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def run(py, cwd, *args, check=True):
    p = subprocess.run([py, "-B", "-m", "cerebrobase", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    if check and p.returncode != 0:
        raise SystemExit(f"STEP_FAILED {args[0]} rc={p.returncode} {p.stdout} {p.stderr}")
    return p.returncode, out


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def start(py, release: Path, cfg: Path, port: int):
    proc = subprocess.Popen([py, "-B", "-m", "cerebrobase", "serve", "--config", str(cfg)], cwd=release,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        try:
            st, body = get(f"http://127.0.0.1:{port}/health")
            if st == 200:
                return proc
        except OSError:
            pass
        time.sleep(0.1)
    proc.kill()
    raise SystemExit("SERVER_DID_NOT_START " + (proc.stderr.read() if proc.stderr else ""))


def stop(py, release, cfg, proc):
    run(py, release, "stop", "--config", str(cfg))
    proc.wait(timeout=20)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()
    src, work, py = Path(a.source).resolve(), Path(a.work).resolve(), a.python
    work.mkdir(parents=True)                                   # refuses an existing dir
    rec: dict = {"schema": "cerebrobase.staging_rehearsal/v1", "synthetic_isolated_local_roots": True,
                 "edge_install": "UNRUN_NOT_INFERRED", "steps": []}

    def step(name, **kw):
        rec["steps"].append({"step": name, **kw})

    # public Main stand-in: must stay byte-identical
    main_root = work / "srv" / "cerebro" / "www"
    main_root.mkdir(parents=True)
    (main_root / "index.html").write_text("<!doctype html><title>Main</title>static public release stand-in\n")
    main_hash = tree_hash(main_root)
    # two releases from the same source: r1 and r2 (different labels => different build ids)
    dist = work / "dist"
    _, m1 = run(py, src, "build", "--out", str(dist), "--label", "r1")
    _, m2 = run(py, src, "build", "--out", str(dist), "--label", "r2")
    rel = work / "srv" / "cerebrobase-staging" / "releases"
    rel.mkdir(parents=True)
    paths = {}
    for m in (m1, m2):
        art = dist / m["artifact"]
        run(py, src, "check-manifest", "--artifact", str(art), "--manifest", f"{art}.manifest.json")
        run(py, src, "install", "--artifact", str(art), "--manifest", f"{art}.manifest.json", "--releases", str(rel))
        paths[m["build_id"]] = rel / m["build_id"]
    step("build_install", r1=m1["build_id"], r2=m2["build_id"], r1_sha256=m1["artifact_sha256"],
         r2_sha256=m2["artifact_sha256"])
    base = work / "srv" / "cerebrobase-staging"
    port = free_port()

    def config(name, build_id, data_root):
        c = {"config_schema": "cerebrobase.config/v1", "environment": "STAGING", "build_id": build_id,
             "db_path": str(data_root / "db" / "rooms.sqlite3"), "private_data_root": str(data_root / "private"),
             "public_assets_root": str(base / "public"), "runtime_root": str(base / "run"), "bind_host": "127.0.0.1",
             "port": port, "public_origin": f"http://127.0.0.1:{port}", "auth_mode": "SYNTHETIC",
             "cookie_secure": False, "capabilities": {"tools": {"fixture.echo": True}},
             "production_roots": {"db_path": str(work / "srv" / "cerebrobase-prod" / "data" / "db" / "rooms.sqlite3"),
                                  "private_data_root": str(work / "srv" / "cerebrobase-prod" / "data" / "private"),
                                  "runtime_root": str(work / "srv" / "cerebrobase-prod" / "run"),
                                  "public_main_root": str(main_root)},
             "min_free_bytes": 1048576}
        p = base / "etc" / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(c, indent=1))
        return p

    def current(build_id, cfg):
        (rel / "CURRENT.json").write_text(json.dumps({"build_id": build_id, "config": cfg.name}))

    def readback(expect_build):
        h = get(f"http://127.0.0.1:{port}/health")
        r = get(f"http://127.0.0.1:{port}/ready")
        ok = h[1].get("build_id") == expect_build and r[0] == 200 and r[1].get("build_id") == expect_build
        return {"health": h[1].get("status"), "ready": r[1].get("status"), "build_id": h[1].get("build_id"),
                "match": ok}

    data = base / "data"
    r1, r2 = m1["build_id"], m2["build_id"]
    # ---- R1 first install ----
    c1 = config("r1", r1, data)
    rc, pf = run(py, paths[r1], "preflight", "--config", str(c1), "--phase", "pre-migrate", check=False)
    step("r1_preflight_pre_migrate", ready=pf.get("ready"), rc=rc)
    run(py, paths[r1], "migrate", "--config", str(c1))
    run(py, paths[r1], "seed", "--config", str(c1))
    current(r1, c1)
    proc = start(py, paths[r1], c1, port)
    rb = readback(r1)
    stop(py, paths[r1], c1, proc)
    step("r1_start_readback_stop", **rb)
    # ---- snapshot before upgrade, then R2 ----
    snap = base / "snapshots" / f"pre-{r2}.sqlite3"
    _, sn = run(py, paths[r1], "snapshot", "--config", str(c1), "--out", str(snap))
    step("snapshot_before_upgrade", schema_version=sn["schema_version"], sha256=sn["sha256"])
    c2 = config("r2", r2, data)
    run(py, paths[r2], "preflight", "--config", str(c2))
    _, mg = run(py, paths[r2], "migrate", "--config", str(c2))
    current(r2, c2)
    proc = start(py, paths[r2], c2, port)
    rb = readback(r2)
    stop(py, paths[r2], c2, proc)
    step("r2_upgrade_readback_stop", migrate=mg["migrate"], **rb)
    # ---- rollback A: compatible schema -> switch back to R1 on the same DB ----
    current(r1, c1)
    proc = start(py, paths[r1], c1, port)
    rb = readback(r1)
    stop(py, paths[r1], c1, proc)
    step("rollback_switch_back_compatible_schema", **rb)
    # ---- rollback B: restore snapshot into NEW isolated root, run R1 there ----
    restored = base / "restored" / f"from-{r2}"
    run(py, paths[r1], "restore", "--snapshot", str(snap), "--into", str(restored / "db"))
    c3 = config("r1-restored", r1, restored)
    current(r1, c3)
    proc = start(py, paths[r1], c3, port)
    rb = readback(r1)
    stop(py, paths[r1], c3, proc)
    import sqlite3
    con = sqlite3.connect(str(restored / "db" / "rooms.sqlite3"))
    rooms = [r[0] for r in con.execute("SELECT id FROM rooms ORDER BY id")]
    con.close()
    step("rollback_restore_isolated_root", rooms=rooms, **rb)
    # ---- negative: a staging config pointing into PROD is refused ----
    bad = config("bad", r1, work / "srv" / "cerebrobase-prod" / "data")
    rc, out = run(py, paths[r1], "preflight", "--config", str(bad), check=False)
    step("staging_into_prod_refused", rc=rc, codes=sorted({p["code"] for p in out.get("problems", [])}))
    rec["public_main_unchanged"] = tree_hash(main_root) == main_hash
    rec["retained"] = {"artifacts": [m1["artifact"], m2["artifact"]], "configs": ["r1.json", "r2.json", "r1-restored.json"],
                       "snapshot": snap.name, "snapshot_sha256": sn["sha256"]}
    rec["result"] = "PASS" if (rec["public_main_unchanged"] and all(
        s.get("match", True) for s in rec["steps"]) and rec["steps"][-1]["rc"] == 2) else "FAIL"
    (work / "rehearsal.json").write_text(json.dumps(rec, indent=1, sort_keys=True))
    print(json.dumps(rec, indent=1, sort_keys=True))
    return 0 if rec["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
