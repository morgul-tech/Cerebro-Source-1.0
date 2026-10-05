# Architecture and state-machine notes (V0.2, authority NONE)

## 1. Shape

```
caller ──admit(spec)──▶ PgAdmissionStore ──┐   ONE DB transaction: key lookup, FOR SHARE on owner rows,
                                           │   V0.1 admission checks, INSERT admission+event+progress, COMMIT
caller ──execute(receipt, spec)──▶ DurableControlledEffectExecutor
              1. verify receipt/spec == stored admission (scope-bound)
              2. begin_attempt: own short tx, FOR UPDATE progress row, FENCED→IN_FLIGHT, COMMIT   ◀── provider is NOT called before this commit is confirmed
              3. provider.apply(grant)            (no DB transaction of this store is open)
              4. record_provider_result (own tx) then read-only readback ➜ COMMITTED_READBACK | UNKNOWN_EFFECT | NO_COMMIT
fresh process ──list_unresolved()──▶ page of {admission_ref, attempt_ref, state, batch_digest, spec, receipt, integrity_ok}
              ──recover(admission_ref)──▶ read-only toward the provider; same ledger rules
```

Connections are opened per operation and closed before return. Nothing is pooled or shared.

## 2. Public API (all names importable from `controlled_effect_executor_durable`)

* `create_schema(connect, schema)` – DDL for a NEW disposable schema (refuses an existing one). Not a Cerebro migration.
* `PgAdmissionStore(connect, schema=, scope_ref=, owner=, clock=)` – `admit`, `progress_of`, `get_admission`,
  `provenance`, `verify_projection`, `list_unresolved(after_cursor=, limit<=500, work_order_ref=)`; ledger writers
  (`begin_attempt`, `record_provider_result`, `record_reconciliation`, `declare_in_flight_lost`) require a one-shot
  in-process writer key held by the executor.
* `DurableControlledEffectExecutor(store=, provider=, readback=, clock=)` – `execute`, `recover` (alias `reconcile`),
  `status`, `list_unresolved`, `declare_in_flight_lost` (owner-reason ledger entry, IN_FLIGHT→UNKNOWN_EFFECT only).
* `OwnerTransactionPort` – `lock_and_read_delegation(cur, ref)`, `lock_and_read_approval(cur, ref)` on the admission
  cursor, taking a lock that conflicts with the owner's own mutations.
* `ProviderSettlementReadbackPort.read_settlement(correlation_ref) -> SettlementEvidence(status, authoritative, evidence_ref)`
  with status `APPLIED | SETTLED_ABSENT | PENDING | UNKNOWN`. Read-only by construction.
* Errors: `StorageError`, `CommitNotConfirmed`, `ReservationNotConfirmed`, `StateMoved`, `PgError`.

## 3. States (V0.1's, unchanged)

`FENCED → IN_FLIGHT → {IN_FLIGHT (ack recorded), UNKNOWN_EFFECT, COMMITTED_READBACK}`, `UNKNOWN_EFFECT → {COMMITTED_READBACK, NO_COMMIT,
UNKNOWN_EFFECT}`. Generated into the DB trigger from V0.1's `TRANSITIONS` table, not retyped. `DENIED` never reaches
the store (nothing durable is written for a denial). There is no edge from UNKNOWN_EFFECT back to FENCED/IN_FLIGHT:
no automatic retry, no second attempt, no new idempotency key.

| Situation | Durable result |
|---|---|
| Crash before the reservation commit | admission stays `FENCED`; `execute` may start it (same admission) |
| Crash/kill after reservation, before/inside provider call | `IN_FLIGHT` with attempt ref; `recover` ⇒ `UNKNOWN_EFFECT` (or `COMMITTED_READBACK` if readback shows the effect) |
| Provider commits, response lost | `UNKNOWN_EFFECT`; readback finds the correlation ⇒ `COMMITTED_READBACK`, no second call |
| Provider queue commits later than an earlier empty read | stays `UNKNOWN_EFFECT` while settlement is `PENDING/UNKNOWN`; later `COMMITTED_READBACK` |
| Provider says `SETTLED_ABSENT` (authoritative) AND history is complete, authoritative and lacks the correlation AND no ack was recorded | `NO_COMMIT` (via IN_FLIGHT→UNKNOWN_EFFECT→NO_COMMIT, two ledger events) |
| Ack recorded but readback shows nothing | `UNKNOWN_EFFECT` `ACK_CONTRADICTED_BY_READBACK`, never NO_COMMIT |
| Reservation commit raises | no provider call; `ATTEMPT_RESERVATION_NOT_CONFIRMED`; state is whatever the database says |
| Admission commit raises | `DENIED ADMISSION_COMMIT_NOT_CONFIRMED`, nothing may execute; identical retry replays if it did commit |

## 4. Why the admission fence is atomic

Revoke/amend of the owner's delegation is an `UPDATE` of a row that the admission transaction has locked with
`SELECT … FOR SHARE` *inside the same transaction as the INSERT*. The UPDATE therefore waits (revoke ordered strictly
after the fence) or has already committed (the read sees REVOKED and denies). No Python lock participates. Evidence:
test 1b (revoke provably blocked on the row lock while the fence transaction is open, via `pg_stat_activity`), test 1c
(negative control: with the lock stripped the revoke is NOT blocked and a stale admission gets through). Stress test 1d
only checks that every outcome is strictly ordered (smoke, not proof).

One admission per key: `UNIQUE(idempotency_key)` + `INSERT … ON CONFLICT DO NOTHING`; the loser reads the winner (same
digest ⇒ `EXACT_REPLAY`; different digest ⇒ `IDEMPOTENCY_KEY_CONFLICT`; other scope ⇒ refused). Attempt reservation
is a database compare-and-set (`FOR UPDATE` + state check), proven across processes (2d, and a deterministic version
where the loser is observed blocked on the lock, then returns `ATTEMPT_ALREADY_STARTED` with 1 provider call).

## 5. Settlement evidence (the "smallest fencing readback")

An empty applied-history is only a point-in-time absence. The executor never writes a fence; it **reads** the provider's
own statement. Settlement is read **before** the observation (finality is irrevocable, so a stale-empty observation can
never be paired with a later settlement). A readback lacking `read_settlement`, unavailable, non-authoritative,
incomplete or `PENDING` evidence keeps `UNKNOWN_EFFECT`. Every non-initial reconciliation records the settlement
status/digest in the ledger event; the database refuses a `NO_COMMIT` event without `settlement_status=SETTLED_ABSENT`
and a digest.

## 6. Database-enforced integrity (schema.py) and its limits

Enforced by triggers/constraints (also against a writer that can INSERT/UPDATE but not disable triggers): immutable
admission rows, `sha256(spec_canonical)=batch_digest`, append-only events with PK `(admission_ref, seq)`, hash chain from
the receipt fingerprint, legal transitions, event columns equal to the stored canonical JSON, NO_COMMIT settlement rule,
projection (`cee_progress`) that moves one seq at a time only to a state an existing event backs.

NOT provided: roles/grants (a writer with INSERT on `cee_admission` can fabricate a self-consistent admission; the
receipt fingerprint is a hash, not a signature), SQL recomputation of event fingerprints, protection from a superuser
(detected by `verify_projection` / `verify_provenance_durable`, not prevented). Provenance proves ledger consistency,
not readback authenticity. A deployment must separate roles so only the executor's account writes these tables.

## 7. Review dispositions (independent adversarial review, read-only agent)

Fixed: (1) trigger bound to canonical JSON + NO_COMMIT rule in DB, docstring weakened to the true claim; (2) detection
tools and listing no longer crash on malformed rows (`integrity_ok=False`, `PROVENANCE_UNPARSEABLE:<seq>`); (3) scope now
binds every store read/write, foreign scope refused; (5) identical UNKNOWN→UNKNOWN verdicts are not re-appended
(a changed observation/settlement still is); synthetic provider fence made atomic (advisory lock per correlation); NUL
bytes rejected in connection values/params; runner default pattern now includes the guard/pin tests; env-scan and
negative-control tests strengthened; the earlier chain tests now use well-formed rows so they cannot pass vacuously.
Documented, not changed: (4) `list_unresolved` cursor can step past a late-committing lower `admit_seq` ⇒ always
re-sweep from `None`; (6) a `FENCED` admission past `delegation_expiry` and an `UNKNOWN_EFFECT` without settlement are
**stuck states with no executor-owned exit** (frozen V0.1 semantics, no expiry/grace chosen here; they stay listed
until an owner decision outside this package); libpq still honours `PGOPTIONS/PGSSLMODE/PGCLIENTENCODING` from the
process environment (the test runner scrubs all `PG*`; the driver forces only host/port/dbname/user/password).

## 8. Known limits (state them, do not hide them)

Owner and executor tables must share one database for the shared-transaction fence. No signatures. `recover` cannot
know whether a peer process is still inside its provider call, so it answers conservatively `UNKNOWN_EFFECT`; the peer's
later ack is then refused by the ledger and reconciled instead (test: `test_a_second_process_cannot_call…`). A
reservation whose provider call never happened also ends `UNKNOWN_EFFECT` and needs an owner decision (no retry).
`recover` is per admission and needs a store of the same scope; there is no global Cerebro owner index. READ COMMITTED
only. Not tested: psycopg driver, Windows, other PostgreSQL versions, TLS/network connections, pooled connections,
real providers/owners, load.

## 9. Smaller alternative

If settlement evidence from providers is not obtainable soon, drop final NO_COMMIT entirely (UNKNOWN_EFFECT is the
terminal "needs owner decision" state) — about 60 fewer lines and one fewer evidence port, at the cost of never closing
a provably-absent attempt automatically.
