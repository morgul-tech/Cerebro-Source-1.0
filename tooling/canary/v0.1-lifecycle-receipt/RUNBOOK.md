# Bounded canary runbook — signalvev-reference-v0.1 (`cerebro.v1.lifecycle.receipt`)

**STATUS (corrected 2026-10-01): EXECUTED (reported). Credential closure UNKNOWN. Verifier provenance UNKNOWN. authority: NONE.**

> **Status correction.** This runbook was written before the live run and the text below describes the *prepared*
> state. The canary has since been executed (two attempts on 2026-09-29: 0/9 execution failure, then 9/9 `PASS`,
> reported). Where later sections say "not yet executed", "never against a real `nats-server`" or "first time", read
> them as historical and see **`EXECUTION_RECORD.md`** for attempt history, evidence pointers, credential-closure
> status, verifier provenance, open deviations and the corrected statements. Raw live evidence is deliberately not in
> this branch. The original "authority: NONE until live-start is explicitly granted" applied to that start; it is not
> a grant for any further run, and a second canary needs its own decision.

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
| `nats_stdlib_client.py` | A minimal, hand-rolled core-NATS client using only `socket`+`json` from the stdlib — see "Why no `nats-py`" below. |
| `transport.py` | `Transport` interface. `InMemoryTransport` (fake, for logic self-test) and `NatsTransport` (real, wraps `nats_stdlib_client.py`, never connects anywhere by default). |
| `evidence.py` | Append-only JSON-lines evidence logging, canonical byte-hashing, and the hard 60-minute deadline kill switch. |
| `producer.py` | Publishes the fixed set, in order, and stops. Refuses to run without an explicit `--nats-url`/`NATS_URL` (`host:port`, no scheme). |
| `consumer.py` | Subscribes, validates, deep-equality-checks, and replays stages through a real, unmodified v0.1 `ReceiptTrail`. Refuses to run without an explicit `--nats-url`/`NATS_URL`. |
| `fake_nats_server.py` | TEST-ONLY minimal NATS server (loopback, ephemeral port) for the two self-tests below — never used in the live run. |
| `offline_selftest.py` | Proves producer/consumer logic (construction, validation, hashing, evidence) is correct using an in-memory fake transport — no socket at all. |
| `protocol_selftest.py` | Proves `nats_stdlib_client.py`'s wire framing is correct over a real (loopback) TCP socket against `fake_nats_server.py`. |
| `end_to_end_selftest.py` | Runs the actual `producer.run()`/`consumer.run()` entry points, with the real `NatsTransport`, over a real loopback socket against `fake_nats_server.py` — the strongest offline verification available: the exact code path the live run uses, with only the real NATS server itself swapped out. |

## Why no `nats-py`

`nats-py` could not be installed in either execution environment tried
for this canary: this project's cloud sandbox, and the isolated Linux
VM `device_bash` runs in on the linked Windows machine (`win-ang8i0nu5t8`)
— both proxy PyPI access and return `403 Forbidden` (confirmed
2026-09-29 against several unrelated packages too, e.g. `cowsay`,
`setuptools`, so this is a general PyPI-egress restriction in both
environments, not specific to `nats-py`). Rather than block on that,
`nats_stdlib_client.py` implements just enough of the NATS core wire
protocol (CONNECT/INFO handshake, PUB, SUB, MSG framing, PING/PONG) —
no JetStream, no TLS, no clustering — using only `socket` and `json`.
This also matches the project's own established convention (see
`signalvev_reference_v01_validation.py`'s own docstring): *"pure
Python, no third-party dependencies."*

## Self-tests (all run, in this sandbox, 2026-09-29 — 3 independent layers)

```
$ python3 tooling/canary/v0.1-lifecycle-receipt/offline_selftest.py
PASS: golden path (all 9, in order) -> PASS
PASS: dropped message -> reported FAIL, no crash
PASS: reordered message -> ReceiptTrail rejects, reported FAIL
PASS: mutated-in-transit message -> reported FAIL
canary_v01_lifecycle_receipt_offline_selftest: 4/4 PASS

$ python3 tooling/canary/v0.1-lifecycle-receipt/protocol_selftest.py
PASS: real-socket (127.0.0.1) CONNECT/SUB/PUB/MSG round trip, 9/9 messages, byte-identical, in order
canary_v01_nats_stdlib_client_protocol_selftest: 1/1 PASS

$ python3 tooling/canary/v0.1-lifecycle-receipt/end_to_end_selftest.py
PASS: real producer.run()/consumer.run() (real NatsTransport + nats_stdlib_client) over a real loopback TCP socket, golden path -> PASS
canary_v01_end_to_end_selftest: 1/1 PASS
```

Layer 1 (`offline_selftest.py`, 4 scenarios): proves the message
construction, schema validation, `ReceiptTrail` replay, and
evidence-logging logic is correct, including three injected failure
modes (dropped/reordered/mutated message) each correctly reported as
FAIL rather than crashing or silently passing. No socket at all.

Layer 2 (`protocol_selftest.py`): proves `nats_stdlib_client.py`'s own
wire-level framing — CONNECT handshake, PUB/SUB headers, MSG parsing —
is correct against a real TCP connection, not a mock.

Layer 3 (`end_to_end_selftest.py`): runs the literal `producer.py`/
`consumer.py` CLI entry points, unmodified, with the real `NatsTransport`
(not `InMemoryTransport`), against a real loopback socket. This is the
exact code path the live run will execute, with only the far end
(`fake_nats_server.py` instead of a real `nats-server`) different.

## What remains genuinely untested until the live run, and why

The one thing these three layers cannot verify is **a real `nats-server`
binary's own behavior** — its exact delivery/ordering guarantees under
real network conditions, and its own auth enforcement. `fake_nats_server.py`
implements exact-subject PUB/SUB routing and CONNECT/+OK handshaking
only, faithfully enough to validate this canary's own logic and wire
framing, but it is not a NATS reimplementation and was never intended
to substitute for testing against the genuine server. This is the
honest boundary of offline preparation; closing it is what the live run
is for.

## Live-run procedure (does not happen until the Human grants access)

1. Human stands up the isolated, non-production NATS context and
   generates a scope limited to **publish+subscribe on
   `cerebro.v1.lifecycle.receipt` only** — no other subject, no
   JetStream, no admin scope. (The stdlib client supports NATS
   username/password or a bearer auth_token; it does not support
   `.creds`/decentralized-JWT auth — see `nats_stdlib_client.py`.)
2. Human hands Claude the connection address (`host:port`, no scheme)
   and, if the server requires it, `NATS_USER`/`NATS_PASSWORD` or
   `NATS_AUTH_TOKEN` — this handoff **is** the live-start trigger.
   Claude does not seek these out proactively. Note: a server bound only
   to `127.0.0.1`/`localhost` on the target machine is not reachable
   from `device_bash`'s isolated VM as "localhost" — it needs a real
   network-visible bind (the machine's LAN address, or `0.0.0.0`).
3. Claude runs, in order, from the repo root (wherever the run
   actually executes — the cloud sandbox, or `device_bash` on a linked
   machine, whichever can reach the given address; see "Execution
   environment" below):
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
   *(Disposition 2026-10-01, PRINCIPAL P22: stays an open local schema
   observation in this branch, `LOCAL_DEVIATION_NE_GLOBAL_SCHEMA_REQUIREMENT`;
   not raised as a `CEREBRO_MESSAGE_V1` schema programme by this work.)*
2. **Full receipt attribution (issuer, `observed_at`, evidence
   reference, parent receipt) is out of scope.** Only the partial-order
   and transport-integrity claims are tested here, to keep the run
   inside 60 minutes and inside "synthetic messages only."
3. **One golden-path linearization only.** This canary does not attempt
   `NO_EFFECT`, `EFFECT_UNKNOWN`, or a second attempt/actor
   `RELEASED`-mismatch scenario over real transport — those remain
   offline-only falsifiers for now. A future, separate bounded canary
   could extend coverage if this one passes.
4. **`NatsTransport`/`nats_stdlib_client.py` is verified against a
   hand-rolled fake server (three self-test layers, see above), but
   never against a real `nats-server` binary.** *(Corrected 2026-10-01: the live run, reported in
   `EXECUTION_RECORD.md`, was the first time it spoke to genuine NATS server software, once, on one isolated server.
   Not shown: other server versions, TLS, JetStream, clustering, adverse network conditions.)* Its wire framing
   follows the documented core NATS protocol and is simple by
   construction (no JetStream, no TLS, no clustering), which bounds
   the risk.
5. **Execution environment.** This canary can run from wherever a
   Python 3.10+ process can reach the given `host:port`: the cloud
   sandbox, or `device_bash` on a linked computer. Both proxy PyPI
   through an allowlist that returned `403 Forbidden` for `nats-py`
   (confirmed 2026-09-29, not package-specific — unrelated packages
   failed the same way) — which is exactly why `nats_stdlib_client.py`
   has no third-party dependency at all and needs nothing installed.
   What is NOT yet confirmed is whether either environment's egress
   allowlist permits a raw TCP connection to an arbitrary `host:port`
   at all (`device_bash`'s own tool description warns that "a plain
   ssh, a database client, or any other direct TCP connection fails at
   once" unless the linked account allows all domains) — this is
   untested until a real target address is given and a plain
   connectivity probe is run against it, before any canary message is
   sent.

## Explicitly not claimed

- Not production-readiness of v0.1 or any other candidate.
- Not a JetStream activation.
- Not a change to Boot, Context State, or any authority layer.
- Not durable/persistent messaging of any kind.
- Not a claim about `NatsTransport`/`nats_stdlib_client.py`'s
  correctness beyond "verified against a hand-rolled fake server across
  three self-test layers, plus one reported live round trip against a
  real `nats-server` (see `EXECUTION_RECORD.md`)."
- Not a claim that this environment's network can even reach an
  arbitrary `host:port`. *(2026-10-01: the run is reported to have executed on the Human's machine; reachability from
  the cloud sandbox or the `device_bash` VM is still untested.)*
- Not an autonomous live-start — this runbook executes only after the
  Human hands over the connection address and, if required, credentials
  as described above.
