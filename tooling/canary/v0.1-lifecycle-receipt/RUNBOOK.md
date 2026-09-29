# Bounded canary runbook — signalvev-reference-v0.1 (`cerebro.v1.lifecycle.receipt`)

**STATUS: PREPARED, NOT YET EXECUTED. authority: NONE until live-start is explicitly granted.**

This runbook operationalizes the PRINCIPAL DECISION of 2026-09-29
(bounded canary track, GO on v0.1 first). It does not itself authorize
anything; it describes, exactly, what will happen if and when the Human
grants the scoped access described below.

## Scope (verbatim from PRINCIPAL DECISION)

- Isolated, non-production NATS context.
- One subject: `cerebro.v1.lifecycle.receipt`.
- One producer, one consumer, synthetic messages only.
- No JetStream, durable storage, production consumers, or authority/state
  effect.
- Max 60 minutes; close after the fixed test set completes.
- On failure: stop, HOLD, revoke credentials/ACL, preserve logs, hashes,
  and receipts.
- A PASS authorizes *evaluating* v0.14 next — it does not auto-start it.

## Roles (verbatim from PRINCIPAL DECISION)

| Role | Owner |
|---|---|
| Scope and verdict | PRINCIPAL |
| Implementation and execution lead | Claude |
| Grants bounded test access | Human (Andreas) |
| Independent (distinct) verification | a separate party — not Claude, not whoever ran the canary |

## What this canary actually proves

`signalvev-reference-v0.1`'s `ReceiptTrail`/`STAGE_PREDECESSORS` partial
order and `validate_envelope()` schema have been offline-verified
(18/18 tests) since this project began. What has never been tested is
whether that same logic survives one real trip over NATS — the X4
runtime-fagpass review named this gap explicitly: *"No single
transport-neutral delivery contract or end-to-end transport receipt."*

This canary sends one synthetic, golden-path receipt trail — the nine
legal stage transitions `PRODUCED → TRANSPORT_ACCEPTED → DELIVERED →
READ → WORK_CONSUMED → WORK_STARTED → EFFECT_ACCEPTED → TERMINAL →
RELEASED` for one fabricated attempt — as nine separate messages on
`cerebro.v1.lifecycle.receipt`, and has the consumer feed each one, in
the order actually received, into a fresh, **unmodified** v0.1
`ReceiptTrail`. If NATS delivers all nine, in order, unmutated,
undropped, and unduplicated, `ReceiptTrail.emit()` accepts every one and
the run is a PASS. If NATS reorders, drops, duplicates, or the payload
is mutated in transit, the consumer's own acceptance logic — the same
code already proven offline — rejects it, and the run is a FAIL. Nothing
new is being trusted; the same falsifiable logic is just now exposed to
real bytes over a real wire for the first time.

This is deliberately **not** a test of the full receipt-attribution
model (issuer signatures, `observed_at` binding, evidence references) —
that stays out of scope to keep this bounded and fast. See "Open
deviations" below.

## Fixed test set

Declared in `fixed_test_set.py`, in full, before any run. Nine messages,
one synthetic attempt (`canary-v0.1-attempt-0001`), one synthetic actor
(`CANARY-SYNTHETIC-ACTOR`), `authority_class: NONE`, `effect_class:
NONE` throughout — no real effect is ever claimed. Both `producer.py`
and `consumer.py` import this exact list; neither may invent a message
outside it, and the consumer independently confirms it received exactly
this set.

```
$ python3 tooling/canary/v0.1-lifecycle-receipt/fixed_test_set.py
9 fixed synthetic messages declared, all schema-valid:
  canary-v0.1-attempt-0001::STAGE::PRODUCED
  canary-v0.1-attempt-0001::STAGE::TRANSPORT_ACCEPTED
  canary-v0.1-attempt-0001::STAGE::DELIVERED
  canary-v0.1-attempt-0001::STAGE::READ
  canary-v0.1-attempt-0001::STAGE::WORK_CONSUMED
  canary-v0.1-attempt-0001::STAGE::WORK_STARTED
  canary-v0.1-attempt-0001::STAGE::EFFECT_ACCEPTED
  canary-v0.1-attempt-0001::STAGE::TERMINAL
  canary-v0.1-attempt-0001::STAGE::RELEASED
```

## Implementation

| File | Purpose |
|---|---|
| `fixed_test_set.py` | The pre-declared 9-message golden path. Self-validates against v0.1's real `validate_envelope()` at import time. |
| `transport.py` | `Transport` interface. `InMemoryTransport` (fake, for self-test) and `NatsTransport` (real, lazy-imports `nats`, never connects anywhere by default). |
| `evidence.py` | Append-only JSON-lines evidence logging, canonical byte-hashing, and the hard 60-minute deadline kill switch. |
| `producer.py` | Publishes the fixed set, in order, and stops. Refuses to run without an explicit `--nats-url`/`NATS_URL`. |
| `consumer.py` | Subscribes, validates, deep-equality-checks, and replays stages through a real, unmodified v0.1 `ReceiptTrail`. Refuses to run without an explicit `--nats-url`/`NATS_URL`. |
| `offline_selftest.py` | Proves all of the above is correct using an in-memory fake transport — no network, no broker, runnable right now. |

## Offline self-test (already run, in this sandbox, 2026-09-29)

```
$ python3 tooling/canary/v0.1-lifecycle-receipt/offline_selftest.py
PASS: golden path (all 9, in order) -> PASS
PASS: dropped message -> reported FAIL, no crash
PASS: reordered message -> ReceiptTrail rejects, reported FAIL
PASS: mutated-in-transit message -> reported FAIL
canary_v01_lifecycle_receipt_offline_selftest: 4/4 PASS
```

Four scenarios: the golden path succeeds; a dropped message times out
and is correctly reported as FAIL (not a crash, not a silent PASS); a
reordered message is caught by `ReceiptTrail`'s own partial-order check;
a message mutated in transit is caught by the consumer's deep-equality
check. This is the maximum verification possible **without** a real
broker — it proves the logic; it cannot and does not prove anything
about real NATS delivery behavior itself.

## What could not be tested here, and why

`nats-py` is not installable in this sandbox — confirmed 2026-09-29
(`pip install nats-py` and `pip install --index-url https://pypi.org/simple/
nats-py` both fail to resolve, while `pip install requests` succeeds, so
this is specific to that package, not a general network outage). This
means the `NatsTransport` class in `transport.py` has never made a real
connection anywhere from this session — it has been written to the
documented `nats-py` API and is otherwise identical in shape to
`InMemoryTransport`, but its correctness against a real server is
unverified until the live run. This is stated plainly, not glossed over.

## Live-run procedure (does not happen until the Human grants access)

1. Human stands up the isolated, non-production NATS context and
   generates ACL/credentials scoped to **publish+subscribe on
   `cerebro.v1.lifecycle.receipt` only** — no other subject, no
   JetStream, no admin scope.
2. Human hands Claude the connection URL and credentials path through
   this session (env vars `NATS_URL`, `NATS_CREDS_PATH`, or the
   `--nats-url`/`--creds` flags) — this handoff **is** the live-start
   trigger. Claude does not seek these out proactively.
3. Claude runs, in order, from the repo root:
   ```
   python3 tooling/canary/v0.1-lifecycle-receipt/consumer.py &
   python3 tooling/canary/v0.1-lifecycle-receipt/producer.py
   ```
   (consumer started first so it is subscribed before the producer
   publishes; both share the same `--max-seconds 3600` default).
4. Both processes write evidence to
   `tooling/canary/v0.1-lifecycle-receipt/evidence-log/<run-id>.jsonl` —
   producer and consumer evidence are separate files by construction
   (see `evidence.py`), for the independent verifier to cross-check.
5. On PASS: stop, hand both evidence files to the independent verifier.
   Per PRINCIPAL DECISION, this authorizes moving to *evaluate* v0.14
   next — it does not itself start that canary.
6. On FAIL or the 60-minute deadline: both scripts already stop and log
   `run_abort`/`FAIL` on their own (see `Deadline` in `evidence.py`).
   Human then revokes the scoped credentials/ACL at the NATS server —
   Claude has no credential-revocation capability and does not attempt
   one; this step is explicitly the Human's per PRINCIPAL DECISION.
   Evidence files are preserved as-is (append-only; nothing here ever
   deletes or rewrites a log).

## Open deviations and assumptions

1. **The CEREBRO_MESSAGE_V1 draft schema has no `stage` field.** The
   receipt taxonomy (`PRODUCED`, `DELIVERED`, etc.) is a separate
   concept layered on top in the source document, presumably intended
   to travel via `payload_ref`/`payload_hash` pointing at a receipt
   object in some future artifact store. For this minimal, bounded,
   fast canary, the stage is instead encoded as a suffix on
   `message_id` (`<attempt_id>::STAGE::<stage>`) — a canary-only
   convention, not a schema extension. `validate_envelope()` accepts it
   unmodified because `message_id` only requires a non-empty string.
   This is worth flagging back to the source document's own maturation
   process as a real, small open question, separate from this canary.
2. **Full receipt attribution (issuer, `observed_at`, evidence
   reference, parent receipt) is out of scope.** Only the partial-order
   and transport-integrity claims are tested here, to keep the run
   inside 60 minutes and inside "synthetic messages only."
3. **One golden-path linearization only.** This canary does not attempt
   `NO_EFFECT`, `EFFECT_UNKNOWN`, or a second attempt/actor
   `RELEASED`-mismatch scenario over real transport — those remain
   offline-only falsifiers for now. A future, separate bounded canary
   could extend coverage if this one passes.
4. **`NatsTransport` is unverified against a real server** — see above.

## Explicitly not claimed

- Not production-readiness of v0.1 or any other candidate.
- Not a JetStream activation.
- Not a change to Boot, Context State, or any authority layer.
- Not durable/persistent messaging of any kind.
- Not a claim about `NatsTransport`'s correctness beyond "written to the
  documented API, untested against a real server."
- Not an autonomous live-start — this runbook executes only after the
  Human hands over the scoped `NATS_URL`/credentials described above.
