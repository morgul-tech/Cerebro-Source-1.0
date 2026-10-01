# Evidence log schema — v0.1 bounded canary

Both `producer.py` and `consumer.py` append one JSON object per line
(JSON-lines) to their own evidence file under
`evidence-log/<run-id>.jsonl`. Producer and consumer evidence are always
separate files, written by separate processes, so a third-party
verifier can compare them independently rather than trusting either
side's self-report alone.

Every line has these common fields, plus event-specific fields listed
below:

| Field | Meaning |
|---|---|
| `run_id` | Unique per process invocation, e.g. `canary-v0.1-producer-<hex>`. |
| `role` | `"producer"` or `"consumer"`. |
| `event` | One of the event names below. |
| `wall_clock_utc` | `strftime` UTC timestamp at the moment this line was written. |
| `monotonic_ns` | `time.monotonic_ns()` at the moment this line was written — for computing durations without wall-clock skew. |

## Producer events

- `run_start` — `subject`, `planned_message_count` (always 9), `max_seconds`, `nats_url_host` (host only, never the full URL/credentials).
- `connected` — no extra fields; marks a successful `transport.connect()`.
- `published` — `message_id`, `sha256` (of the exact canonical bytes published), `byte_length`.
- `run_complete` — `status` (`"ALL_PUBLISHED"`), `count`.
- `run_abort` — `reason` (`"DEADLINE_EXCEEDED"` or `"EXCEPTION"`), `detail`.

## Consumer events

- `run_start` — `subject`, `expected_message_count` (always 9), `max_seconds`, `nats_url_host`.
- `connected`
- `accepted` — `message_id`, `stage`, `raw_sha256` (of the exact bytes received), `stages_reached` (the `ReceiptTrail`'s cumulative state at this point).
- `reject` — `reason` (one of `UNDECODABLE`, `UNEXPECTED_MESSAGE_ID`, `DUPLICATE`, `SCHEMA_INVALID`, `CONTENT_MUTATED`, `RECEIPT_TRAIL_REJECTED`), plus reason-specific fields (`message_id`, `detail`, or for `CONTENT_MUTATED` both `expected_sha256` and `received_sha256`).
- `receive_timeout` — `received_so_far`.
- `run_complete` — `status` (`"PASS"` or `"FAIL"`), `accepted`, `expected`, `failures` (list of human-readable strings).
- `run_abort` — same shape as producer's.

## What the independent verifier checks

1. Producer's `run_complete.status == "ALL_PUBLISHED"` and
   `count == 9`.
2. Consumer's `run_complete.status == "PASS"`.
3. For every `published` entry in the producer log, there is exactly
   one `accepted` entry in the consumer log with the same `message_id`.
4. No `reject` entries at all (any reject means the run is a FAIL by
   definition — a canary reject is evidence of the exact failure mode
   this run exists to catch, not noise to explain away).
5. The consumer's `stages_reached` sequence, read across its `accepted`
   entries in order, is exactly the golden path declared in
   `fixed_test_set.STAGE_SEQUENCE`.

Any deviation from all five is a FAIL, full stop — per PRINCIPAL
DECISION scope, a FAIL means stop, HOLD, and credential revocation, not
a retry.
