#!/usr/bin/env python3
"""Offline self-test for the v0.1 bounded canary -- NO real NATS server,
NO network, NO subprocess. Runs producer.run() and consumer.run() against
a shared InMemoryTransport standing in for one subject.

STATUS: canary preparation artifact. authority: NONE.

This is the "prepared and self-tested now" half of PRINCIPAL DECISION
("Claude kan forberede hele runbooken og implementasjonen na"). It
proves the message construction, schema validation, ReceiptTrail replay,
hashing and evidence-logging logic is correct. It CANNOT and does not
claim to prove anything about real NATS delivery semantics (ordering,
at-least-once, drop behavior under network partition, etc.) -- that
claim can only be established by the live run described in RUNBOOK.md,
which this script does not perform and has no ability to perform (see
RUNBOOK.md: nats-py is not installable in this sandbox).

Four scenarios:
  1. golden path -- all 9 messages delivered in order -> PASS
  2. dropped message -- consumer times out waiting -> reported FAIL,
     not a crash
  3. reordered message -- ReceiptTrail rejects the broken partial order
     -> reported FAIL, not a crash (this is the core falsifier this
     canary exists to exercise against real transport later)
  4. mutated-in-transit message -- consumer's deep-equality check
     rejects it -> reported FAIL, not a crash
"""
from __future__ import annotations

import json
import queue
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import producer  # noqa: E402
import consumer  # noqa: E402
from fixed_test_set import build_fixed_test_set, SUBJECT  # noqa: E402
from evidence import canonical_bytes  # noqa: E402
from transport import InMemoryTransport  # noqa: E402


def _run_scenario(name: str, corrupt=None) -> tuple[int, int]:
    """Returns (producer_exit_code, consumer_exit_code). `corrupt`, if
    given, is a function(list[bytes]) -> list[bytes] applied to the
    published payloads before the consumer drains them, simulating a
    transport-layer failure mode without needing a real broker."""
    tmpdir = Path(tempfile.mkdtemp(prefix="canary-selftest-"))
    shared_q: "queue.Queue[bytes]" = queue.Queue()

    prod_transport = InMemoryTransport(shared_q)
    prod_rc = producer.run(
        nats_url="in-memory://offline-selftest",
        creds_path=None,
        evidence_dir=tmpdir / "producer-evidence",
        max_seconds=60,
        transport=prod_transport,
    )

    if corrupt is not None:
        drained = []
        while not shared_q.empty():
            drained.append(shared_q.get())
        for payload in corrupt(drained):
            shared_q.put(payload)

    cons_transport = InMemoryTransport(shared_q)
    cons_rc = consumer.run(
        nats_url="in-memory://offline-selftest",
        creds_path=None,
        evidence_dir=tmpdir / "consumer-evidence",
        max_seconds=60,
        per_message_timeout=1.0,
        transport=cons_transport,
    )

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"  [{name}] producer_exit={prod_rc} consumer_exit={cons_rc}")
    return prod_rc, cons_rc


def scenario_golden_path() -> bool:
    prod_rc, cons_rc = _run_scenario("golden_path")
    return prod_rc == 0 and cons_rc == 0


def scenario_dropped_message() -> bool:
    def drop_one(payloads: list[bytes]) -> list[bytes]:
        return payloads[:-1]  # drop the last (RELEASED)
    prod_rc, cons_rc = _run_scenario("dropped_message", corrupt=drop_one)
    # producer still succeeds (it sent everything); consumer must FAIL
    # (timeout), not crash and not silently report PASS.
    return prod_rc == 0 and cons_rc == 1


def scenario_reordered_message() -> bool:
    def swap_adjacent(payloads: list[bytes]) -> list[bytes]:
        # Swap WORK_STARTED and EFFECT_ACCEPTED (indices 5, 6) -- both
        # individually schema-valid, but out of legal partial order.
        payloads = list(payloads)
        payloads[5], payloads[6] = payloads[6], payloads[5]
        return payloads
    prod_rc, cons_rc = _run_scenario("reordered_message", corrupt=swap_adjacent)
    return prod_rc == 0 and cons_rc == 1


def scenario_mutated_message() -> bool:
    def mutate_one(payloads: list[bytes]) -> list[bytes]:
        payloads = list(payloads)
        msg = json.loads(payloads[2])  # DELIVERED
        msg["target"]["referent_id"] = "TAMPERED"
        payloads[2] = canonical_bytes(msg)
        return payloads
    prod_rc, cons_rc = _run_scenario("mutated_message", corrupt=mutate_one)
    return prod_rc == 0 and cons_rc == 1


def selftest() -> int:
    scenarios = [
        ("golden path (all 9, in order) -> PASS", scenario_golden_path),
        ("dropped message -> reported FAIL, no crash", scenario_dropped_message),
        ("reordered message -> ReceiptTrail rejects, reported FAIL", scenario_reordered_message),
        ("mutated-in-transit message -> reported FAIL", scenario_mutated_message),
    ]
    results = []
    for label, fn in scenarios:
        ok = fn()
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}: {label}")

    passed = sum(results)
    total = len(results)
    print(f"\ncanary_v01_lifecycle_receipt_offline_selftest: {passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
