#!/usr/bin/env python3
"""Producer side of the v0.1 bounded canary.

STATUS: canary preparation artifact. authority: NONE. Not yet executed
against real infrastructure.

Publishes exactly the fixed_test_set.build_fixed_test_set() messages, in
order, to one subject, and stops. Never invents a message. Never retries
indefinitely (a single retry budget only, logged either way). Aborts
hard on the 60-minute deadline. Requires an explicit --nats-url (or
NATS_URL env var) -- refuses to run with no target, and refuses to run
past the fixed set's length.

This script does not decide when it is allowed to run against a real
server; that decision is the Human-granted, scoped access described in
RUNBOOK.md. Running this file against a real NATS_URL IS the "live
start" -- do not invoke it outside the access window PRINCIPAL and the
Human have explicitly opened.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence import EvidenceWriter, Deadline, DeadlineExceeded, canonical_bytes, sha256_hex  # noqa: E402
from fixed_test_set import build_fixed_test_set, SUBJECT  # noqa: E402
from transport import NatsTransport, Transport  # noqa: E402


def run(nats_url: str, creds_path: str | None, evidence_dir: Path, max_seconds: int,
        transport: Transport | None = None, user: str | None = None,
        password: str | None = None, auth_token: str | None = None) -> int:
    """transport: injectable for offline_selftest.py (InMemoryTransport).
    Defaults to a real NatsTransport when not supplied -- production
    callers (main() below) never pass this argument. nats_url is
    'host:port' (no scheme) when transport is not supplied."""
    run_id = f"canary-v0.1-producer-{uuid.uuid4().hex[:12]}"
    deadline = Deadline(max_seconds=max_seconds)
    messages = build_fixed_test_set()

    if transport is None:
        transport = NatsTransport(nats_url, creds_path=creds_path, user=user,
                                   password=password, auth_token=auth_token)
    evidence_path = evidence_dir / f"{run_id}.jsonl"

    with EvidenceWriter(evidence_path, role="producer", run_id=run_id) as ev:
        ev.record("run_start", subject=SUBJECT, planned_message_count=len(messages),
                   max_seconds=max_seconds, nats_url_host=nats_url.split("@")[-1])
        try:
            deadline.check()
            transport.connect()
            ev.record("connected")

            for msg in messages:
                deadline.check()
                payload = canonical_bytes(msg)
                digest = sha256_hex(payload)
                transport.publish(SUBJECT, payload)
                ev.record("published", message_id=msg["message_id"],
                           sha256=digest, byte_length=len(payload))

            ev.record("run_complete", status="ALL_PUBLISHED", count=len(messages))
            print(f"producer: published {len(messages)}/{len(messages)} PASS")
            return 0

        except DeadlineExceeded as exc:
            ev.record("run_abort", reason="DEADLINE_EXCEEDED", detail=str(exc))
            print(f"producer: ABORT (deadline exceeded): {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 -- must still record evidence on any failure
            ev.record("run_abort", reason="EXCEPTION", detail=f"{type(exc).__name__}: {exc}")
            print(f"producer: ABORT (exception): {exc}", file=sys.stderr)
            return 1
        finally:
            transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", default=os.environ.get("NATS_URL"),
                         help="Required. 'host:port', no scheme. No default. "
                              "Also settable via NATS_URL env var.")
    parser.add_argument("--creds", default=os.environ.get("NATS_CREDS_PATH"),
                         help="NOT IMPLEMENTED (stdlib client has no .creds/JWT support). "
                              "Use --user/--password or --auth-token instead.")
    parser.add_argument("--user", default=os.environ.get("NATS_USER"))
    parser.add_argument("--password", default=os.environ.get("NATS_PASSWORD"))
    parser.add_argument("--auth-token", default=os.environ.get("NATS_AUTH_TOKEN"))
    parser.add_argument("--evidence-dir", default="tooling/canary/v0.1-lifecycle-receipt/evidence-log",
                         type=Path)
    parser.add_argument("--max-seconds", type=int, default=3600,
                         help="Hard wall-clock cap. PRINCIPAL DECISION: 3600s (60 min).")
    args = parser.parse_args()

    if not args.nats_url:
        print("producer: refusing to run -- no --nats-url / NATS_URL given. "
              "This is deliberate: there is no implicit target.", file=sys.stderr)
        return 2

    return run(args.nats_url, args.creds, args.evidence_dir, args.max_seconds,
               user=args.user, password=args.password, auth_token=args.auth_token)


if __name__ == "__main__":
    raise SystemExit(main())
