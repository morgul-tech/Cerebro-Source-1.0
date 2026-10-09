"""A. deterministic offline build, manifest check, exact build on /health and /ready, restart persistence,
DB / private-root unavailability makes /ready fail (never false readiness)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from support import ROOT, ServerCase, config_dict, jbody

PY = sys.executable


def cb(*args, cwd=ROOT):
    p = subprocess.run([PY, "-B", "-m", "cerebrobase", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    return p.returncode, (json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}), p


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http_json(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class A1_DeterministicBuild(unittest.TestCase):
    def test_two_clean_builds_identical_and_manifest_verifies(self):
        d = Path(tempfile.mkdtemp(prefix="cb-build-"))
        self.addCleanup(shutil.rmtree, d, True)
        # two clean copies of the source tree in different locations/times => same bytes
        outs = []
        for i in (1, 2):
            src = d / f"src{i}" / "candidate"
            shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns("__pycache__", "tests"))
            time.sleep(1.1)
            rc, man, _ = cb("build", "--out", str(d / f"dist{i}"), "--label", "release", cwd=src)
            self.assertEqual(rc, 0)
            outs.append((d / f"dist{i}" / man["artifact"], man))
        (a1, m1), (a2, m2) = outs
        self.assertEqual(a1.read_bytes(), a2.read_bytes())
        self.assertEqual((m1["artifact_sha256"], m1["build_id"]), (m2["artifact_sha256"], m2["build_id"]))
        rc, out, _ = cb("check-manifest", "--artifact", str(a1), "--manifest", f"{a1}.manifest.json")
        self.assertEqual((rc, out["check_manifest"]), (0, "PASS"))
        with zipfile.ZipFile(a1) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names)
            for info in z.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
        self.assertFalse(any(".git" in n or n.endswith(".pyc") or "/tests/" in n for n in names))
        self.assertNotIn(str(d).encode(), blob)                        # no absolute host/temp path
        self.assertNotIn(os.path.expanduser("~").encode() + b"/", blob)
        tampered = d / "tampered.zip"
        data = bytearray(a1.read_bytes())
        data[100] ^= 0xFF
        tampered.write_bytes(bytes(data))
        rc, out, _ = cb("check-manifest", "--artifact", str(tampered), "--manifest", f"{a1}.manifest.json")
        self.assertEqual((rc, out["check_manifest"]), (2, "FAIL"))


class A2_ServeHealthReadyRestart(unittest.TestCase):
    def test_installed_artifact_serves_exact_build_restart_persists_and_ready_fails_on_db_or_root(self):
        d = Path(tempfile.mkdtemp(prefix="cb-serve-"))
        self.addCleanup(shutil.rmtree, d, True)
        rc, man, _ = cb("build", "--out", str(d / "dist"), "--label", "t1")
        art = d / "dist" / man["artifact"]
        rc, out, _ = cb("install", "--artifact", str(art), "--manifest", f"{art}.manifest.json", "--releases",
                        str(d / "releases"))
        rel = d / "releases" / man["build_id"]
        port = free_port()
        (d / "public").mkdir()
        cfg = config_dict(d, environment="STAGING", build_id=man["build_id"], port=port,
                          public_origin=f"http://127.0.0.1:{port}",
                          production_roots={"db_path": str(d / "prod" / "db"), "private_data_root": str(d / "prod" / "p")})
        cp = d / "staging.json"
        cp.write_text(json.dumps(cfg))
        rc, out, _ = cb("preflight", "--config", str(cp), cwd=rel)
        self.assertEqual(rc, 3)                                          # DB missing before migrate: not ready
        self.assertIn("DB_MISSING", json.dumps(out["blocking"]))
        self.assertEqual(cb("migrate", "--config", str(cp), cwd=rel)[0], 0)
        self.assertEqual(cb("seed", "--config", str(cp), cwd=rel)[0], 0)

        def start():
            proc = subprocess.Popen([PY, "-B", "-m", "cerebrobase", "serve", "--config", str(cp)], cwd=rel,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(100):
                try:
                    if http_json(f"http://127.0.0.1:{port}/health")[0] == 200:
                        return proc
                except OSError:
                    time.sleep(0.1)
            proc.kill()
            self.fail("server did not start")

        proc = start()
        st, h = http_json(f"http://127.0.0.1:{port}/health")
        self.assertEqual((st, h["status"], h["build_id"]), (200, "alive", man["build_id"]))
        st, r = http_json(f"http://127.0.0.1:{port}/ready")
        self.assertEqual((st, r["status"], r["build_id"]), (200, "ready", man["build_id"]))
        self.assertEqual(set(r), {"status", "build_id", "request_id"})           # public: minimal only
        # private root unavailable => not ready (distinguished from DB)
        priv = Path(cfg["private_data_root"])
        priv.rename(priv.with_name("private.moved"))
        st, r = http_json(f"http://127.0.0.1:{port}/ready")
        self.assertEqual((st, r["status"]), (503, "not_ready"))
        priv.with_name("private.moved").rename(priv)
        # DB unavailable => not ready, health still alive
        db = Path(cfg["db_path"])
        db.rename(db.with_name("moved.sqlite3"))
        self.assertEqual(http_json(f"http://127.0.0.1:{port}/ready")[0], 503)
        self.assertEqual(http_json(f"http://127.0.0.1:{port}/health")[0], 200)
        db.with_name("moved.sqlite3").rename(db)
        self.assertEqual(cb("stop", "--config", str(cp), cwd=rel)[1]["stop"], "STOPPED")
        proc.wait(timeout=20)
        proc = start()                                                  # restart: schema + fixtures persisted
        self.assertEqual(http_json(f"http://127.0.0.1:{port}/ready")[1]["status"], "ready")
        import sqlite3
        con = sqlite3.connect(str(db))
        self.assertEqual(con.execute("SELECT count(*) FROM rooms").fetchone()[0], 2)
        con.close()
        cb("stop", "--config", str(cp), cwd=rel)
        proc.wait(timeout=20)
        # tampered install => integrity mismatch => serve refused
        (rel / "cerebrobase" / "ui.py").write_text((rel / "cerebrobase" / "ui.py").read_text() + "\n# tampered\n")
        rc, out, _ = cb("serve", "--config", str(cp), cwd=rel)
        self.assertEqual((rc, out["serve"]), (3, "REFUSED_NOT_READY"))
        self.assertIn("BUILD_INTEGRITY_MISMATCH", json.dumps(out["blocking"]))


class A3_InProcessReadiness(ServerCase):
    def test_ready_reports_db_failure_distinct_from_root_failure_in_admin_ops(self):
        from support import jbody as _j  # noqa: F401
        c = self.logged_in("fixture-andreas-admin")
        st, _, body = c.get("/admin/drift.json")
        self.assertEqual((st, jbody(body)["checks"]["db"], jbody(body)["checks"]["private_root"]), (200, "OK", "OK"))
        self.cfg.private_data_root.rename(self.cfg.private_data_root.with_name("gone"))
        st, _, body = c.get("/admin/drift.json")
        self.assertEqual((jbody(body)["checks"]["private_root"], jbody(body)["checks"]["db"]),
                         ("PRIVATE_ROOT_UNAVAILABLE", "OK"))


if __name__ == "__main__":
    unittest.main()
