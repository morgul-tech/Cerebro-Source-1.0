#!/usr/bin/env python3
"""Real-socket protocol self-test for nats_stdlib_client.py.

STATUS: canary preparation artifact. authority: NONE.

Unlike offline_selftest.py (which uses InMemoryTransport -- an
in-process fake with no socket at all), this test starts a real TCP
listener on 127.0.0.1 (fake_nats_server.py), connects two independent
NatsClient instances to it over actual loopback sockets, and drives the
full CONNECT/SUB/PUB/MSG wire protocol for real. This is the strongest
verification possible without a real nats-server binary or external
network access -- it exercises byte framing, header parsing, and
multi-connection routing exactly as nats_stdlib_client.py would see
them against a genuine NATS server, closing (for the client's own wire
handling, not for a real server's behavior) the gap noted in
RUNBOOK.md.

Loopback-only (127.0.0.1, OS-assigned ephemeral port) -- no external
network, no pip dependency, no relation to any real NATS_URL.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_nats_server import FakeNatsServer  # noqa: E402
from nats_stdlib_client import NatsClient  # noqa: E402
from fixed_test_set import build_fixed_test_set, SUBJECT  # noqa: E402
from evidence import canonical_bytes  # noqa: E402
import socket  # noqa: E402


def _connect(host: str, port: int, name: str) -> NatsClient:
    sock = socket.create_connection((host, port), timeout=5.0)
    return NatsClient(sock, timeout_seconds=5.0, name=name)


def run() -> bool:
    server = FakeNatsServer()
    server.start()
    time.sleep(0.05)  # let the accept loop start

    messages = build_fixed_test_set()
    expected_payloads = [canonical_bytes(m) for m in messages]

    consumer = _connect(server.host, server.port, "protocol-selftest-consumer")
    consumer.subscribe(SUBJECT, sid="1")
    time.sleep(0.05)  # ensure SUB is registered before PUB starts

    received: list[bytes] = []

    def consume() -> None:
        for _ in range(len(messages)):
            payload = consumer.next_msg(timeout_seconds=5.0)
            if payload is None:
                break
            received.append(payload)

    consumer_thread = threading.Thread(target=consume)
    consumer_thread.start()

    producer = _connect(server.host, server.port, "protocol-selftest-producer")
    for payload in expected_payloads:
        producer.publish(SUBJECT, payload)

    consumer_thread.join(timeout=10.0)
    producer.close()
    consumer.close()
    server.stop()

    ok = received == expected_payloads
    print(f"protocol_selftest: received {len(received)}/{len(expected_payloads)} messages, "
          f"byte-identical-in-order={ok}")
    if not ok:
        for i, (exp, got) in enumerate(zip(expected_payloads, received)):
            if exp != got:
                print(f"  mismatch at index {i}: expected {exp!r} got {got!r}")
        if len(received) != len(expected_payloads):
            print(f"  count mismatch: expected {len(expected_payloads)}, got {len(received)}")
    return ok


def selftest() -> int:
    ok = run()
    label = "real-socket (127.0.0.1) CONNECT/SUB/PUB/MSG round trip, 9/9 messages, byte-identical, in order"
    print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"\ncanary_v01_nats_stdlib_client_protocol_selftest: {1 if ok else 0}/1 PASS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
