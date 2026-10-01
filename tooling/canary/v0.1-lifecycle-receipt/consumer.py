#!/usr/bin/env python3
"""Consumer side of the v0.1 bounded canary.

STATUS: canary preparation artifact. authority: NONE. Not yet executed
against real infrastructure.

For each message received on the subject, in receipt order:
  1. Schema-validate it with v0.1's own, unmodified validate_envelope().
  2. Reject anything whose message_id is not in the pre-declared fixed
     set, or that has already been seen (no unexpected/duplicate
     acceptance).
  3. Deep-equal it against the exact expected message content (proves
     NATS did not mutate any field in transit).
  4. Extract its declared stage and feed it into one fresh, unmodified
     v0.1 ReceiptTrail -- in the exact order actually received. If NATS
     reordered, dropped, or duplicated a stage such that the partial
     order breaks, ReceiptTrail.emit() raises ReceiptError here, and
     this canary run is a FAIL. This is the entire point of the canary:
     the same acceptance logic already proven offline in
     signalvev_reference_v01_validation.py, now exercised against real
     transport bytes for the first time.

Stops after receiving len(fixed_test_set) messages or hitting the
deadline / a per-message receive timeout, whichever comes first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence import EvidenceWriter, Deadline, DeadlineExceeded, canonical_bytes, sha256_hex  # noqa: E402
from fixed_test_set import build_fixed_test_set, stage_from_message_id, SUBJECT  # noqa: E402
from transport import NatsTransport, Transport  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "tooling" / "validator"))
import signalvev_reference_v01_validation as v01  # noqa: E402


def run(nats_url: str, creds_path: str | None, evidence_dir: Path,
        max_seconds: int, per_message_timeout: float,
        transport: Transport | None = None, user: str | None = None,
        password: str | None = None, auth_token: str | None = None) -> int:
    """transport: injectable for offline_selftest.py (InMemoryTransport).
    Defaults to a real NatsTransport when not supplied. nats_url is
    'host:port' (no scheme) when transport is not supplied."""
    run_id = f"canary-v0.1-consumer-{uuid.uuid4().hex[:12]}"
    deadline = Deadline(max_seconds=max_seconds)
    expected = {m["message_id"]: m for m in build_fixed_test_set()}
    seen: set[str] = set()

    if transport is None:
        transport = NatsTransport(nats_url, creds_path=creds_path, user=user,
                                   password=password, auth_token=auth_token)
    evidence_path = evidence_dir / f"{run_id}.jsonl"
    trail: v01.ReceiptTrail | None = None
    failures: list[str] = []

    with EvidenceWriter(evidence_path, role="consumer", run_id=run_id) as ev:
        ev.record("run_start", subject=SUBJECT, expected_message_count=len(expected),
                   max_seconds=max_seconds, nats_url_host=nats_url.split("@")[-1])
        try:
            transport.connect()
            ev.record("connected")

            while len(seen) < len(expected):
                deadline.check()
                raw = transport.next_message(SUBJECT, timeout_seconds=per_message_timeout)
                if raw is None:
                    failures.append(f"receive timeout after {len(seen)}/{len(expected)} messages")
                    ev.record("receive_timeout", received_so_far=len(seen))
                    break

                raw_sha = sha256_hex(raw)
                try:
                    parsed = json.loads(raw)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"undecodable payload (sha256={raw_sha}): {exc}")
                    ev.record("reject", reason="UNDECODABLE", raw_sha256=raw_sha)
                    continue

                mid = parsed.get("message_id")

                if mid not in expected:
                    failures.append(f"unexpected message_id {mid!r} (not in fixed set)")
                    ev.record("reject", reason="UNEXPECTED_MESSAGE_ID", message_id=mid)
                    continue
                if mid in seen:
                    failures.append(f"duplicate message_id {mid!r}")
                    ev.record("reject", reason="DUPLICATE", message_id=mid)
                    continue

                try:
                    v01.validate_envelope(parsed)
                except v01.SchemaError as exc:
                    failures.append(f"{mid}: schema invalid on receipt: {exc}")
                    ev.record("reject", reason="SCHEMA_INVALID", message_id=mid, detail=str(exc))
                    continue

                if parsed != expected[mid]:
                    failures.append(f"{mid}: content mutated in transit")
                    ev.record("reject", reason="CONTENT_MUTATED", message_id=mid,
                               expected_sha256=sha256_hex(canonical_bytes(expected[mid])),
                               received_sha256=sha256_hex(canonical_bytes(parsed)))
                    continue

                stage = stage_from_message_id(mid)
                if trail is None:
                    trail = v01.ReceiptTrail(
                        attempt_id=parsed["target"]["referent_id"],
                        actor_id=parsed["source"]["actor_id"],
                        generation=parsed["source"]["generation"],
                    )
                try:
                    trail.emit(
                        stage,
                        actor_id=parsed["source"]["actor_id"],
                        generation=parsed["source"]["generation"],
                    )
                except v01.ReceiptError as exc:
                    failures.append(f"{mid}: ReceiptTrail rejected stage {stage!r}: {exc}")
                    ev.record("reject", reason="RECEIPT_TRAIL_REJECTED", message_id=mid,
                               stage=stage, detail=str(exc))
                    continue

                seen.add(mid)
                ev.record("accepted", message_id=mid, stage=stage, raw_sha256=raw_sha,
                           stages_reached=list(trail.stages_reached))

            status = "PASS" if (not failures and len(seen) == len(expected)) else "FAIL"
            ev.record("run_complete", status=status, accepted=len(seen),
                       expected=len(expected), failures=failures)
            print(f"consumer: accepted {len(seen)}/{len(expected)}, status={status}")
            for f in failures:
                print(f"  FAIL: {f}", file=sys.stderr)
            return 0 if status == "PASS" else 1

        except DeadlineExceeded as exc:
            ev.record("run_abort", reason="DEADLINE_EXCEEDED", detail=str(exc))
            print(f"consumer: ABORT (deadline exceeded): {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            ev.record("run_abort", reason="EXCEPTION", detail=f"{type(exc).__name__}: {exc}")
            print(f"consumer: ABORT (exception): {exc}", file=sys.stderr)
            return 1
        finally:
            transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", default=os.environ.get("NATS_URL"),
                         help="Required. 'host:port', no scheme.")
    parser.add_argument("--creds", default=os.environ.get("NATS_CREDS_PATH"),
                         help="NOT IMPLEMENTED -- use --user/--password or --auth-token.")
    parser.add_argument("--user", default=os.environ.get("NATS_USER"))
    parser.add_argument("--password", default=os.environ.get("NATS_PASSWORD"))
    parser.add_argument("--auth-token", default=os.environ.get("NATS_AUTH_TOKEN"))
    parser.add_argument("--evidence-dir", default="tooling/canary/v0.1-lifecycle-receipt/evidence-log",
                         type=Path)
    parser.add_argument("--max-seconds", type=int, default=3600)
    parser.add_argument("--per-message-timeout", type=float, default=30.0)
    args = parser.parse_args()

    if not args.nats_url:
        print("consumer: refusing to run -- no --nats-url / NATS_URL given.", file=sys.stderr)
        return 2

    return run(args.nats_url, args.creds, args.evidence_dir, args.max_seconds,
               args.per_message_timeout, user=args.user, password=args.password,
               auth_token=args.auth_token)


if __name__ == "__main__":
    raise SystemExit(main())
