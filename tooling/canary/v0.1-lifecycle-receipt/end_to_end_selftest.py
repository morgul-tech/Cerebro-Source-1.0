#!/usr/bin/env python3
"""End-to-end self-test: the REAL producer.run()/consumer.run() (with
the real NatsTransport + nats_stdlib_client, not InMemoryTransport)
against fake_nats_server.py over an actual loopback TCP socket.

STATUS: canary preparation artifact. authority: NONE.

This is the strongest offline verification this canary has: it is the
exact code path that will run against the real NATS server (same
producer.py/consumer.py entry points, same NatsTransport, same
nats_stdlib_client wire framing), with only the far end swapped for a
minimal fake server instead of a real `nats-server` binary or a real
NATS_URL. What remains genuinely untested until the live run is real
`nats-server`'s own behavior (its exact ordering/delivery guarantees,
any auth enforcement details, real network conditions) -- not this
client's or this canary's own logic, which this test exercises in
full, including the CLI-facing run() functions themselves.

Loopback-only (127.0.0.1, OS-assigned ephemeral port). No external
network, no pip dependency.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_nats_server import FakeNatsServer  # noqa: E402
import producer  # noqa: E402
import consumer  # noqa: E402


def run() -> bool:
    server = FakeNatsServer()
    server.start()
    time.sleep(0.05)

    tmpdir = Path(tempfile.mkdtemp(prefix="canary-e2e-selftest-"))
    nats_url = f"{server.host}:{server.port}"
    results: dict[str, int] = {}

    def run_consumer() -> None:
        results["consumer_rc"] = consumer.run(
            nats_url=nats_url, creds_path=None,
            evidence_dir=tmpdir / "consumer-evidence",
            max_seconds=60, per_message_timeout=5.0,
        )

    consumer_thread = threading.Thread(target=run_consumer)
    consumer_thread.start()
    time.sleep(0.2)  # let the consumer connect + subscribe before publishing

    producer_rc = producer.run(
        nats_url=nats_url, creds_path=None,
        evidence_dir=tmpdir / "producer-evidence",
        max_seconds=60,
    )
    consumer_thread.join(timeout=10.0)
    server.stop()

    consumer_rc = results.get("consumer_rc")
    ok = producer_rc == 0 and consumer_rc == 0
    shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"end_to_end_selftest: producer_rc={producer_rc} consumer_rc={consumer_rc}")
    return ok


def selftest() -> int:
    ok = run()
    label = ("real producer.run()/consumer.run() (real NatsTransport + "
              "nats_stdlib_client) over a real loopback TCP socket, golden path -> PASS")
    print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"\ncanary_v01_end_to_end_selftest: {1 if ok else 0}/1 PASS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
