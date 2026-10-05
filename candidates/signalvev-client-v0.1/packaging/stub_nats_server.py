#!/usr/bin/env python3
"""DRY-RUN SHIM ONLY: accepts `-a HOST -p PORT` like nats-server and serves the test protocol STUB. NOT a broker.
Used to exercise disposable_roundtrip.py's own orchestration logic when no real nats-server exists. A PASS through this
shim is NOT a DISPOSABLE_NATS_ROUNDTRIP=PASS."""
import argparse
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from protocol_stub import ProtocolStub  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("-a", default="127.0.0.1")
ap.add_argument("-p", type=int, required=True)
args = ap.parse_args()
stub = ProtocolStub()
stub.start(args.p)
done = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: done.set())
while not done.wait(0.2):
    pass
stub.stop()
