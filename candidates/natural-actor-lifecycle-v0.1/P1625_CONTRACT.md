# P1625 local NAL minimum integration

State: candidate, default OFF, no live or production connection.

## Exact call path

An already issued WORKER task and actor/child binding enter
`WorkerEpisodeRunner.run(IssuedEpisode)`. The runner authenticates the
current Human rights, PM actor/generation delegation and exact task revision
and SHA through `BoundPmAuthorityV02`. It binds a private parent fence to
`PgOwnerBindingEvidence` for that episode, then calls NAL consume, START,
terminal and owner admission/close. Its only substantive work is a bounded
SHA256 read of an explicitly mapped file under an explicitly mapped local root.
The digest and byte count are passed to an injected terminal owner. That owner
and the admission owner must publish trusted facts into the existing evidence
reader; the runner does not seed either. The close result is checked against
persisted terminal/receipt/outbox evidence.

No boot, worker registration, claim issuance, provider write, shell, network,
NATS, Drive, scheduler, daemon or automatic wake is introduced.

## Parent authority

`schema_parent_authority_local_v1.sql` is disposable PostgreSQL schema.
It requires five separately provisioned roles: NOLOGIN
`cerebro_pm_authority_owner`, Human-rights issuer, PM administrative
delegation issuer, PM decision issuer and existing `cerebro_nal_owner`.
The worker role only receives read and parent-fence EXECUTE. Direct table
writes and issuer/revoke/retire calls are not granted to it.

The DB stores owner-issued Human rights, PM delegation and an exact PM task
decision with revision/SHA, claim/packet/queue, task provenance and immutable
basis. The Python adapter recomputes basis SHA, then the owner function
compares the full JSON basis to the stored decision; a caller-provided hash
alone is never authority. `BoundPmAuthorityV02` checks current rights and
delegation before a transition.

For each NAL mutation, the same PostgreSQL cursor/transaction holds actor
`FOR UPDATE`, then rights, delegation, decision and child binding in that
order. Owner revoke and explicit SUPERSEDED/EXPIRED retirement take those same
row locks. The parent reservation is acquired before NAL drafts state or
ledgers and rechecked at the commit guard. Revoke/retire first rejects the
transition. Commit first permits one committed transition/receipt, then revoke
waits. No START grace is granted for a later terminal. A non-NULL
`expires_at` on rights, delegation or decision is rejected at the trusted
owner fence as `PARENT_TIMED_EXPIRY_UNSUPPORTED`, even if its timestamp is
still in the future. This is deliberate: a clock crossing between final
guard and COMMIT cannot be serialized by row locks. Explicit EXPIRED owner
retirement remains a separate serialized event, never a substitute for
wall-clock expiry. The v1 child-binding schema has no timed-expiry field or
supported finite child profile; a future one must add its own commit-order
control or fail closed before authorizing progress.

`PgParentAuthorityIssuer` is only a local issuance adapter behind the
distinct roles. It does not authenticate a real Human, PM or provider.
Those source facts and credentials must be lawfully supplied before any
production binding. All adapters remain default OFF.

## Unknown and restart

An ambiguous consume/START stops for actor readback and never retries. For
terminal and close the runner emits a serializable outcome key and performs
fresh owner-evidence and persisted actor readback. It compares exact actor,
generation, task revision/SHA, child binding, terminal result/material and
admission identity. Close additionally requires the exact immutable terminal,
receipt and outbox ledgers and final closed actor state. Only a complete match
returns `COMMITTED_EXACT_READBACK`; absent, partial, stale, mismatched or
unreadable evidence remains `HOLD_UNKNOWN` with a typed reason. The same key
can be reread after restart without redoing material work or issuing another
terminal/close. Ordinary restart remains `HOLD_RESTART_REQUALIFICATION`.
Historical post-revoke reporting remains the pre-existing non-authorizing
NAL path; this runner grants it no new effect or outbox.

## Local proof and remaining gate

The local runner test executes a seven-byte file read, consumes its SHA256
in a trusted terminal fixture and verifies one persisted close receipt.
Negative tests cover default OFF, unbound/path-escape, revoke before consume,
unsupported finite parent before NAL draft, terminal/close lost acknowledgments
before and after commit, exact repeated readback, wrong persisted
result/material/admission/receipt/outbox and forged caller evidence. Existing
PM v0.2 and P1613 revocation suites remain green. The executable disposable
PostgreSQL oracle covers revoke/supersede/explicit-retirement-first,
future-finite decision rejection, wrong generation/revision, commit-first
waiter and role separation, but is UNRUN
without four P1625 DSNs and this migration installed in an isolated DB.
No live/production credential or database is requested by this candidate.

This is a local integration candidate, not proof of production Human custody,
host bootstrap, live worker wake, or native PostgreSQL serialization.
