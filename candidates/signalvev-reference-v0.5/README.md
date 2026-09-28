# Signalvev Reference Implementation Candidate v0.5 (durable command outbox)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass's own **"One sequencing correction"** section names a
specific, concrete gap and recommends fixing it: "the first reference
stack cannot prove autonomous work consumption if its only wake edge is
an ephemeral signal. A tiny local, restart-safe command/outbox fixture
is enough to test semantics without activating JetStream." It lists five
exact steps.

That gap was never actually closed. v0.1's `receipt-taxonomy-candidate.yaml`
lists this as falsifier 3 ("Ephemeral wake is lost and the committed
durable command becomes unreachable") and has a corresponding test, but
on inspection that test only asserts that a Python `dict` copy still has
the same `idempotency_key` field -- it never models signal loss,
redelivery, or a restart. This candidate builds the actual fixture X4
asked for.

## Base commit

```
b1109b8 (main, after PR #5 + #8 + #9 + #7 + #10-#15 -- second rebase)
```

Originally prepared against `4e756e7` (before PR #7/PR #9 merged) and
delivered on branch `claude/signalvev-reference-v0.5`. PRINCIPAL's
review returned **HOLD: rebase after PR #9 and document that the
canonical validator runs without error.** First rebased onto `01a24d3`
(PR #9 and PR #7 both merged) -- see the "original rebase" gate run
below. **Rebased a second time** onto `b1109b8` (after PR #10-#15
merged v0.8-v0.13) via another clean cherry-pick of the same original
commit -- file content is again unchanged, only the base moved forward.
See "Mandatory pre-delivery gate" below for the current, honest result.

## Branch

`claude/signalvev-reference-v0.5` (re-delivered; replaces the prior
upload of this branch, which was based on the pre-PR#7/PR#9 `main`)

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.5/durable-command-outbox-candidate.yaml
A  candidates/signalvev-reference-v0.5/component.yaml
A  candidates/signalvev-reference-v0.5/README.md   (this file)
A  tooling/validator/signalvev_reference_v05_validation.py
```

Nothing in any prior candidate directory is modified. The validator
**imports** (does not copy) `validate_envelope`, `STAGE_PREDECESSORS`,
`ReceiptTrail`, `ReceiptError`, `_base_msg` from v0.1. It has **no
dependency on v0.2 or v0.3** -- see "Why this is not a duplicate" below
and `component.yaml`'s `explicitly_no_dependency_on`.

## Why this is not a duplicate of v0.2's selective-durable-inbox-candidate

`signalvev-reference-v0.2/selective-durable-inbox-candidate.yaml`
answers *which messages qualify for durable storage* (an admission
policy: `should_store`/`admit`). This candidate assumes a message has
already qualified and answers a different question: does a committed
command *survive* losing its wake signal and a simulated restart, and is
*redelivery* of that wake safe (does it ever create a second effect).
The two are complementary. This candidate does not import or duplicate
`DurableInboxPolicy`.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `durable-command-outbox-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> One sequencing correction; 2. X4 runtime-fagpass -> Falsifiers for one offline reference arc (item 3); 1. Human text -> L2 SIGNAL / REFLEX |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v05_validation.py selftest
signalvev_reference_v05_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v04_validation selftest: 8/8 PASS
```

9 checks total: one golden-path composition test through the full
sequencing correction (`OB0`), plus eight falsifiers (`OB1`-`OB8`) each
targeting one specific liveness or idempotency property:

- **OB0** (golden path): commit -> wake (pointer only) -> reread ->
  `WORK_CONSUMED` -> `WORK_STARTED` -> `EFFECT_ACCEPTED` -> `TERMINAL` ->
  `RELEASED`, all five sequencing steps in order, ending in a released
  v0.1 `ReceiptTrail`.
- **OB1**: a command is still reachable and consumable via
  `all_pending_identities()` poll-based discovery when its wake signal
  is entirely lost (`wake=None`) -- the direct proof for falsifier 3.
- **OB2**: a command survives a simulated restart -- the outbox is
  serialized to a snapshot, the original instance is discarded, and a
  fresh instance rebuilt purely from that snapshot can still consume it.
- **OB3**: redelivering the same wake twice results in exactly one
  `WORK_CONSUMED` receipt -- the second call is an idempotent no-op
  (`WORK_CONSUMED_ALREADY`), never a second admission or effect.
- **OB4**: a wake pointing at a command_identity absent from the outbox
  is never admitted -- `NO_SUCH_COMMAND`, no receipt advances. A wake's
  own payload is never sufficient authority on its own.
- **OB5**: `commit()` rejects an empty `claim_ref` -- the state owner's
  claim reference must be explicit at commit time (sequencing step 1),
  never invented later by the consumer.
- **OB6**: this candidate does not bypass v0.1's stage machine --
  calling the consume path on a trail that never reached
  `TRANSPORT_ACCEPTED` still raises `ReceiptError` from v0.1's own
  `STAGE_PREDECESSORS` check, proving real reuse, not a parallel
  implementation.
- **OB7**: consuming one committed command does not consume, hide, or
  otherwise affect an unrelated command in the same outbox.
- **OB8**: `WakeSignal` structurally carries no `authority_ref`,
  `authority_class`, `payload`, `claim_ref`, or `effect_class` field --
  a signal is a pointer only, never work authority (X4 crosswalk, L2
  row).

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. "Restart-safe" is proven via an in-memory
serialize/deserialize snapshot round-trip (`to_snapshot`/
`from_snapshot`), not real disk I/O or a real process restart -- see
`explicitly_not` in `durable-command-outbox-candidate.yaml`.

## Mandatory pre-delivery gate

**Original run** (against base `4e756e7`, before PR #7/PR #9): this
candidate's own files introduced no new YAML failures. The scan showed
only the already-documented, pre-existing crash in
`candidates/signalvev-reference-v0.2/request-reply-candidate.yaml`
(PR #9, not yet merged at the time).

**Original rebase run**, onto `01a24d3` (PR #9 and PR #7 both merged),
per PRINCIPAL's HOLD instruction to document the result honestly:

```
$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=23']
```

**The crash is gone.** Full-repo YAML scan was 0 failures, confirming PR
#9's fix. `canonical_foundation.py` no longer raised an unhandled
`yaml.scanner.ScannerError` -- it completed and returned a structured
result.

**Second rebase run**, onto `b1109b8` (after PR #10-#15 merged
v0.8-v0.13), same YAML scan and gate re-run against the actually
fetched remote content:

```
$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=29']
```

Still no crash, still no new failure introduced by this candidate.

**One error remains, disclosed rather than hidden:**
`README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=29`. This is
the same pre-existing drift first surfaced during the PR #9 work,
grown with each subsequent merge (PR #7: +1 for v0.3; PR #10-#15: +6
for v0.8-v0.13) and now by one more for this candidate's own
`component.yaml` -- independently verified via
`find . -name component.yaml | wc -l` = 29, not just trusted from the
validator's own count. Whether `candidates/*/component.yaml` files
should count toward the root README's "Komponenter" total, or whether
`canonical_foundation.py` should exclude `candidates/` from that count,
is an architecture decision left for PRINCIPAL -- the same judgment call
already deferred once in PR #9's delivery note, not something this
candidate decides on its own. See `candidates/signalvev-reference-v0.4/README.md`
for the identical finding (this drift is repo-wide, not specific to
either candidate).

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from v0.1/v0.2/v0.4: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
liveness/idempotency property of one reference primitive, a narrower
claim than "this works on real Signalvev transport."

## Open deviations and assumptions

1. **"Restart-safe" is simulated, not real.** The snapshot round-trip
   proves the outbox's state does not depend on any live in-process
   reference surviving, which is the property that matters for the
   *semantics* X4 asked to test -- but a real deployment would still
   need to choose actual persistence (a file, a database, JetStream, or
   something else). This candidate deliberately stays silent on that
   choice; see `explicitly_not`.
2. **This candidate does not import v0.2 or (unmerged) v0.3 at all.**
   It is narrower in scope than either by design, not because they are
   incompatible with it. A future candidate could combine this outbox
   with v0.2's presence/request-reply primitives or v0.3's
   FlightRecorder once useful, but doing so here would have widened
   scope beyond the one gap this candidate targets.
3. **v0.1's own falsifier-3 test is not modified or removed.** This
   candidate supersedes it in spirit (it is a much stronger proof of the
   same claim) but leaves v0.1 untouched, consistent with every prior
   candidate's practice of not modifying already-merged files. Whether
   v0.1's thinner test should eventually be replaced by a pointer to
   this candidate is a PRINCIPAL-scoped judgment call, not made here.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a scheduler, and not a claim that the outbox decides when or
  whether work runs.
- Not a deployment recommendation for how a real outbox should persist.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.4`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
