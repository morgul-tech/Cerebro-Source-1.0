#!/usr/bin/env python3
"""ONE bounded real roundtrip between two INSTALLED client instances through a DISPOSABLE local nats-server.

Loopback only (127.0.0.1, random free port), no auth, throw-away temp state, never EDGE-01 and no production
credentials. Starts the broker, runs `listen` (node-a) and `send` (node-b) from the clean-venv installation, checks
the receiver's typed result, then ALWAYS terminates the broker and the listener and verifies they are gone.
Without a nats-server binary it prints DISPOSABLE_NATS_ROUNDTRIP=UNRUN (it does not try to obtain one).

usage: python3 disposable_roundtrip.py --venv DIR [--nats-server PATH]    (PATH defaults to `nats-server` on PATH)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def server_info(port: int) -> dict:
    """The server announces itself first (INFO). A protocol stub says so; a real nats-server does not."""
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        line = sock.makefile("rb").readline()
    try:
        return json.loads(line.split(b" ", 1)[1])
    except (IndexError, ValueError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--venv", required=True, type=Path)
    ap.add_argument("--nats-server", default="nats-server")
    args = ap.parse_args()
    server_bin = shutil.which(args.nats_server) or (args.nats_server if Path(args.nats_server).is_file() else None)
    if server_bin is None:
        print(json.dumps({"DISPOSABLE_NATS_ROUNDTRIP": "UNRUN", "reason": "no nats-server binary available"}))
        return 0
    bindir = args.venv / ("Scripts" if os.name == "nt" else "bin")
    exe = str(bindir / ("signalvev-client.exe" if os.name == "nt" else "signalvev-client"))
    port = free_port()
    work = Path(tempfile.mkdtemp(prefix="signalvev-roundtrip-"))
    report: dict = {"DISPOSABLE_NATS_ROUNDTRIP": "FAIL", "server": f"127.0.0.1:{port}"}
    server = listener = None
    try:
        for name in ("owner.synthetic.json", "event.example.json"):
            shutil.copyfile(CANDIDATE / "examples" / name, work / name)
        for src, dst, evdir in (("node-listen.example.toml", "a.toml", "state-a"), ("node-send.example.toml", "b.toml", "state-b")):
            text = (CANDIDATE / "examples" / src).read_text(encoding="utf-8")
            text = text.replace("nats://127.0.0.1:4222", f"nats://127.0.0.1:{port}").replace(f'dir = "{evdir}"', f'dir = "{evdir}"')
            (work / dst).write_text(text, encoding="utf-8")
        server = subprocess.Popen([server_bin, "-a", "127.0.0.1", "-p", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_port(port):
            report["reason"] = "broker did not start listening"
            return 1
        info = server_info(port)
        report["server_version"] = info.get("version")
        is_stub = "stub" in str(info.get("version", "")).lower() or info.get("server_name") == "protocol-stub"
        listener = subprocess.Popen([exe, "listen", "--config", str(work / "a.toml"), "--duration", "30", "--max-frames", "1"],
                                    cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first = listener.stdout.readline()
        if '"LISTEN_START"' not in first:
            report["reason"] = f"listener did not start: {first.strip()[:120]}"
            return 1
        sent = subprocess.run([exe, "send", "--config", str(work / "b.toml"), "--event", str(work / "event.example.json")],
                              cwd=work, capture_output=True, text=True, timeout=30)
        send_out = json.loads(sent.stdout.strip().splitlines()[-1]) if sent.stdout.strip() else {}
        rest, _ = listener.communicate(timeout=40)
        lines = [json.loads(x) for x in rest.splitlines() if x.strip()]
        ingress = next((x for x in lines if x.get("event") == "INGRESS"), {})
        ok = sent.returncode == 0 and send_out.get("state") == "ACCEPTED" and listener.returncode == 0 and ingress.get("disposition") == "ACK_READ"
        verdict = ("DRYRUN_WITH_PROTOCOL_STUB_NOT_A_BROKER" if ok else "FAIL") if is_stub else ("PASS" if ok else "FAIL")
        report.update({"DISPOSABLE_NATS_ROUNDTRIP": verdict, "send": {"exit": sent.returncode, "state": send_out.get("state")},
                       "listen": {"exit": listener.returncode, "disposition": ingress.get("disposition"), "reason": ingress.get("reason"),
                                  "resolver": "SYNTHETIC_FIXTURE_NOT_OWNER_TRUTH"},
                       "claim_ceiling": "transport roundtrip through a disposable broker; ACK_READ against a SYNTHETIC owner fixture only"})
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        report["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return 1
    finally:
        for proc in (listener, server):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)
        report["processes_stopped"] = all(p is None or p.poll() is not None for p in (listener, server))
        shutil.rmtree(work, ignore_errors=True)
        print(json.dumps(report, indent=1))


if __name__ == "__main__":
    sys.exit(main())
