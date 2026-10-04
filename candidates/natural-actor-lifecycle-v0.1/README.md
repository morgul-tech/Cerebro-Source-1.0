# NAL-01 · Natural Actor Lifecycle v0.1 candidate

**State:** isolated, default-off candidate. **Authority:** none. This directory is
not wired into MCP, PM, Context, C9, C10, A7, a real provider, a production DB,
or a dispatcher. A local commit of this branch does not release or deploy it.

The candidate joins an existing role-null birth to one bounded worker task:

1. A trusted role-attach readback proves `READY_UNBOUND` was born with null role,
   then attached `WORKER` with zero claims. This code does not create a birth or
   change the Context-owned pre-role aggregate.
2. A trusted owner readback admits an exact task/attempt/claim/packet/queue with
   Source revision and role projection. Only then can a trusted owner binding
   for that task be issued/read back. Supersession is append-only and possible
   before consume. A revoked, stale, wrong-role or changed binding holds.
3. Separate trusted worker readbacks establish **consume**, then **START**, then
   a `TerminalOccurrenceV1`. Provider send alone has no transition. The reader
   is an injected interface and defaults to unavailable; a caller-supplied ref
   or SHA-256 is not authentication.
4. A separate trusted PM admission readback binds the terminal digest and first
   actor/generation/claim/packet/queue/binding. `admit_release_once` closes the
   task and creates one immutable receipt plus one versioned OwnerEvent **intent**
   in the same local transaction. A new close requires an **owner-provided
   commit fence** that serializes binding revocation/supersession with the actor
   transaction. The actor lock is acquired first, then the owner fence; the
   fence remains held until local commit or rollback. Currentness is asserted
   at entry and again at the commit edge while fenced. Without that trusted
   capability close returns `OWNER_COMMIT_FENCE_UNAVAILABLE` with zero effect.
   An exact completed close can replay its original receipt without reopening
   owner authority. This code never sends a chat or broker event.
   Correct `REFINE`/`HOLD` may close a task when policy admits it; that does not
   turn the project result into PASS.
5. The old terminal remains first-use bound across frontiers. Same terminal and
   binding return the original receipt. A changed digest/binder/lineage conflicts.
   Closing the task leaves the actor **not reusable**. A separate trusted reset
   readback proves capabilities closed and advances epoch/frontier before reuse.

`lifecycle.py` holds the transition contract and a per-process memory store for
falsifiers. `postgres_store.py` is a transactional PostgreSQL adapter. Its
connection factory is absent and its enable flag is false by default. It locks
the actor head (`FOR UPDATE`), appends binding/terminal/receipt/outbox ledgers
with unique keys, and updates actor state in one transaction. The supplied
owner fence is entered after `FOR UPDATE` and held past PostgreSQL COMMIT;
this is a contract for a future trusted owner adapter, not an implemented
provider integration. After commit it
reopens the state and, for closure, reads the independent terminal→receipt→outbox
chain. A commit/readback failure is typed unknown. `schema_v1.sql` is a **candidate
migration** requiring a separately provisioned `cerebro_nal_owner` role; the
`nal` schema grants nothing to PUBLIC or Context. It was not applied here.

The committed outbox row is **not** a `COMMITTED_READBACK` claim. After independent
store readback, `project_owner_event` emits a producer-side raw event compatible
with the current `signalvev_sensing.owner_event.accept_owner_event` parser. The
local test parses it against that existing interface. The event is still only
`CANDIDATE_LOCAL_ONLY`: the A7 trusted owner receipt reader must authenticate
the NAL commit before learning. A7 owns the consumer adapter, relevance check,
Context learning write/readback, cursor and delivery semantics. This branch
does not alter `owner_event.py`, `receiver.py`, `return_sink.py`, or Context tables.

Run local tests from the Source root:

```text
python -B -m unittest discover -s candidates/natural-actor-lifecycle-v0.1 -p 'test_*.py' -v
```

The default test command exercises the in-memory model. A portable PostgreSQL
17.11 binary was later found and used only for isolated C990/C995 disposable
proofs; the PG tests require explicit local DSNs and are skipped otherwise.
Real provider evidence, owner issuer custody,
cross-provider currentness, PM policy, host cutover, external outbox delivery,
reset and production effects remain unproven. A pilot would additionally need
one authoritative writer/epoch and history-preserving rollback under existing
release gates.

## C995 same-PG PM owner-binding candidate

`schema_owner_binding_local_v1.sql` adds a **disposable** PM-owned row and
immutable operation receipts alongside NAL in the same local database. It
requires separately provisioned `cerebro_pm_binding_owner` (NOLOGIN),
`cerebro_pm_binding_issuer`, and `cerebro_nal_owner` roles. The issuer can
reserve a provider revision and call issue/revoke/supersede functions; the NAL
closer can only read and call `acquire_close_fence`. Neither role has direct
table-write rights. Each mutation uses the same owner row lock, and the
closer's `PgOwnerFence` calls the lock function through NAL's **own SQL cursor**.
The lock therefore survives through the NAL close transaction's commit or
rollback. The local issuer computes the existing canonical fingerprint from
the owner-provided binding fields; the closer cannot submit a hash as
authority. Missing or mismatched row/revision/actor/lineage holds before close.

`owner_binding_pg.py` supplies the local issuer and evidence/fence adapters;
both are default-off and require an explicit local test connection factory.
`test_owner_binding_pg.py` is skipped unless `NAL_C995_CLOSER_DSN` and
`NAL_C995_ISSUER_DSN` point at an isolated disposable database with both
migrations applied. The local proof covers revisioned issue/current/revoke/
supersede receipts, role grants, revoke-first and close-first, concurrent
serialization, wrong actor/revision/role, direct-write bypass denial, and
rollback/ambiguous postcommit recovery. The earlier synthetic tests remain
valid. A disposable PG execution is evidence of mechanics and local role
separation only: it does **not** establish a real authenticated PM issuer,
production custodian, database deployment, or live provider integration.

Basis: P22-BUILDDAY-20261003/v1 `BEGIN_P22_BUILDDAY_EXECUTION_V1`;
`PROSJEKTMANN_CHANNEL:664`; `PM_PRINCIPAL_CHANNEL:9312,9316`;
`WORK_CLAIMS:1762`, `WORK_PACKETS:2434`, `READY_QUEUE:738`;
Source main `59aa4e72025c98edc6ec5dbbe88bb610d0ea6232`.
The C978 in-memory semantics fixture and C965/C947 C10 SQLite shadow informed
the contract shape only. Neither contributes authority or code imports.


## P1613 · full-chain authority at NAL transition commit

P1613 closes one local read-before-commit gap for the integrated
WORKER/NAL/PM-authority candidate. The owner policy is
\`REVOCATION_CUTS_FUTURE_AUTHORITY_PRESERVES_HISTORY\`.

For v0.2-provenance tasks, a new \`WORK_CONSUME\`, \`WORK_START\`,
\`WORK_TERMINAL\`, or authority-bearing normal task close is no longer admitted
from an earlier ACTIVE binding read alone. The NAL transaction asks its injected
evidence port for a \`transition_authority_fence\`. The PostgreSQL adapter
requires a separately injected parent-authority fence and composes it with the
existing child binding row fence while the actor row transaction is open.

The local lock order is:

\`\`\`text
actor row -> parent authority reservation/fence -> child task-binding fence
\`\`\`

The parent fence is supplied by the existing default-off PM-authority v0.2
surface. It must link the current Human mandate, PM actor/generation delegation,
exact issued task/decision and the NAL task provenance. The child fence still
checks actor/generation/task/claim/packet/queue and binding currentness. Missing
parent proof fails closed. A production parent owner is not bound by this
candidate.

The commit guard rechecks the chain after the transition draft and immediately
before local commit. Therefore a controlled revoke after the currentness read
but before commit cannot persist new authorized consume/start/progress. A
transition that committed first remains an immutable fact/replay; later revoke
blocks later new transitions. Ambiguous commit is \`HOLD_UNKNOWN\`; a separate
authoritative actor-state readback distinguishes committed from proven no
commit before any retry, and a retry must reacquire the full authority fence.

The old C995/C999 child-binding-only close mechanics remain valid for their
legacy task shape and proof scope. A task carrying the new v0.2 provenance
fields (\`task_revision\`, \`decision_ref\`, \`provenance_ref\`) requires the
full parent+child chain for the normal close as well, because that close creates
the ordinary close receipt and owner-event outbox.

### Historical reporting after revoke

\`report_historical_terminal_after_revoke\` is a separate, narrow path. It
requires trusted readback of the actual parent revoke plus exact task revision,
task SHA-256, actor/generation, claim/packet/queue, decision, provenance and
binding. It also requires a prior valid START or independently verified
pre-revoke execution.

- \`HISTORICAL_AFTER_REVOKE\` additionally requires independent evidence that
  substantive work/result was already complete before the revoke frontier.
- \`ABORTED_REVOKED\` records administrative truth without claiming success.
- Missing completion proof cannot be silently upgraded to historical success.

Historical receipts have \`authority=NONE\`,
\`new_work_authorized=false\`, \`new_effect_authorized=false\`,
\`owner_obligation_created=false\`, and \`outbox_created=false\`. They are
stored only with actor state for exact idempotent reporting/administrative
close; they do not create a NAL terminal occurrence, owner obligation,
successor work or authority-bearing outbox.

Focused local proof:

\`\`\`text
python -B tooling/validator/nal_authority_revocation_validation.py
\`\`\`

The proof covers parent and child revoke-after-read/before-commit barriers,
no-grace terminal semantics, normal close denial after revoke, close-first
single receipt/replay, historical success versus unproven success, aborted
administrative close, ambiguous commit reconciliation, exact duplicate
reporting, parent/child lock order and PostgreSQL-adapter fail-closed behavior
when the parent fence is unbound.

This is local/default-off contract evidence only. It does not prove or install
the production Human mandate reader, PM delegation/decision owner, parent
reservation/fence, provider executor, PostgreSQL DSNs, deployment, live Worker,
Boot, A7, C4, or Room B.
