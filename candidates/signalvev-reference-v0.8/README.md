# Signalvev Reference Implementation Candidate v0.8 (stale revision admission guard)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass's own crosswalk table names a material L4 gap:
"Supported live provider atomic compare-and-bind/admit remains absent.
PM8290 and PM8306 keep live provider HOLD." Falsifier 7, verbatim: "A
stale snapshot or route revision admits work." v0.1's falsifier-7 test
has a passing check, but it is a single inline closure with one
hardcoded scenario (`current_revision=5, snapshot_revision=3`) -- it
proves the concept once, is not a reusable guard, does not track a
referent's revision over time, does not cover the "route revision" half
of the falsifier's own wording, and critically does not test the case
that matters operationally: a snapshot that was valid *when read* going
stale *before admission is attempted*. This candidate builds the real,
reusable compare-and-bind guard and closes the last of the twelve X4
falsifiers that did not yet have a dedicated reference implementation.

## Base commit
01a24d3 (main, after PR #5 + PR #8 + PR #9 + PR #7)


## Branch

`claude/signalvev-reference-v0.8`

## Changed files (all additions, nothing modified or deleted)

A candidates/signalvev-reference-v0.8/stale-revision-admission-guard-candidate.yaml
A candidates/signalvev-reference-v0.8/component.yaml
A candidates/signalvev-reference-v0.8/README.md (this file)
A tooling/validator/signalvev_reference_v08_validation.py


Nothing in any prior candidate directory is modified. The validator
**imports** (does not copy) `ReceiptTrail`, `ReceiptError` from v0.1. It
has **no dependency on v0.2, v0.3, v0.5, v0.6, or v0.7**.

## Why this is not a duplicate of v0.5 or v0.7

`signalvev-reference-v0.5`'s `DurableCommandOutbox` answers whether a
committed command survives signal loss and a restart.
`signalvev-reference-v0.7`'s `IdempotencyScope` answers whether two
attempts are "the same" attempt. This candidate answers a third,
independent question: is the caller's view of a referent's *current
revision* still current at the moment of admission, or has the world
moved on underneath it. None of the three import or duplicate each
other.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `stale-revision-admission-guard-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> Current runtime crosswalk (L4 row); 2. X4 runtime-fagpass -> Falsifiers (item 7); 1. Human text -> Viktige negative regler (EVENT != STATE_TRUTH) |

## Offline test results

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v08_validation.py selftest
signalvev_reference_v08_validation selftest: 9/9 PASS
$ echo $?
0


Regression checks -- all prior suites still pass unchanged:

signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
signalvev_reference_v07_validation selftest: 9/9 PASS

(v0.4, v0.5, and v0.6 are separate branches, not on this one; each independently regression-clean per its own README.)

9 checks total: one golden-path composition test (`RG0`), plus eight
falsifiers (`RG1`-`RG8`):

- **RG0** (golden path): a first caller admits successfully at the
  referent's current revision; another actor then changes state,
  bumping the revision; a second caller holding the now-stale snapshot
  is held, never admitted.
- **RG1**: an exact revision match is admitted.
- **RG2**: a snapshot behind the current revision is held.
- **RG3**: a snapshot *ahead* of the current revision is also held --
  the contract is exact match, not merely "not older."
- **RG4**: an unknown referent raises, never silently admits.
- **RG5**: the operationally important case -- a snapshot that was
  valid when read can still go stale before admission is attempted;
  currentness is checked live against the ledger, never cached from
  read time.
- **RG6**: independent referents never interfere with each other's
  admission outcomes.
- **RG7**: a `HOLD_STALE_REVISION` outcome is never paired with a
  `WORK_CONSUMED` receipt emission, and v0.1's own `STAGE_PREDECESSORS`
  still rejects skipping straight to `WORK_STARTED`.
- **RG8**: the guard covers `route` referents, not just `state`
  referents -- matching falsifier 7's own "snapshot or route revision"
  wording.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. No live provider integration exists here -- X4's own
PM8290/PM8306 live provider HOLD remains explicitly unaddressed.

## Mandatory pre-delivery gate

$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=25']


No new failures introduced. The one error is the same pre-existing,
PRINCIPAL-scoped `README_METADATA_COUNT_DRIFT` judgment call already
flagged in PR #9 and in every candidate since v0.4, grown by exactly one
for this candidate's own `component.yaml`.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
currentness-guard property of one reference primitive, a narrower claim
than "this works against a real live provider."

## Open deviations and assumptions

1. **This candidate does not integrate with a real live provider.** X4
   explicitly names "supported live provider atomic compare-and-bind/
   admit" as the remaining gap, and PM8290/PM8306 keep that HOLD. This
   candidate proves the *semantics* offline; wiring it to an actual
   provider is out of scope here, same as every other candidate in this
   project.
2. **`RevisionLedger` is in-memory only**, like v0.1's `ReceiptTrail`
   and v0.7's `DurableCommandAdmitter` -- it does not claim
   restart-safety. Combining it with v0.5's restart-safe outbox pattern
   is a reasonable future candidate, not done here.
3. **Ahead-of-current and behind-current snapshots both return the same
   `HOLD_STALE_REVISION` outcome** (RG3). A future candidate could split
   these into distinct outcomes if operationally useful; this candidate
   keeps the contract to the single "exact match or held" rule X4's
   text supports.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a live provider integration.
- Not a claim/occupancy grant mechanism.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.7`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
