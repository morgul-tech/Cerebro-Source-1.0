# Signalvev Reference Implementation Candidate v0.9 (first-broken-edge flight recorder)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass's own crosswalk table names a material L6 gap:
"No common trace/correlation/causation across PM -> transport -> worker
-> effect. First broken edge still requires manual reconstruction."
Falsifier 11, verbatim: "Trace reports a continuous chain where a
receipt edge is absent." The Human-supplied text's own COMMUNICATION
FLIGHT RECORDER section names the same tool: "Hva skjedde med melding
X? ... Skal vise forste brutte edge og UNKNOWN uten a gjette." v0.1's
`test_falsifier_11_no_skipped_edge_in_trace` has a passing check, but
it is a single inline closure with one hardcoded observed-set -- it
proves the concept once, is not a reusable reconstructor, does not
distinguish a chain that has simply not yet progressed from one with a
real gap and later evidence, does not handle the `EFFECT` stage's
three-way branch, and does not test that it never fabricates the
missing receipt it reports. This candidate builds the real, reusable,
read-only reconstructor.

## Base commit

```
01a24d3 (main, after PR #5 + PR #8 + PR #9 + PR #7)
```

## Branch

`claude/signalvev-reference-v0.9`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.9/first-broken-edge-flight-recorder-candidate.yaml
A  candidates/signalvev-reference-v0.9/component.yaml
A  candidates/signalvev-reference-v0.9/README.md   (this file)
A  tooling/validator/signalvev_reference_v09_validation.py
```

Nothing in any prior candidate directory is modified. The validator
**imports** (does not copy) `ReceiptTrail`, `ReceiptError` from v0.1. It
has **no dependency on v0.2, v0.3, v0.5, v0.6, v0.7, or v0.8**.

## Why this is not a duplicate of v0.7 or v0.8

`signalvev-reference-v0.7`'s `IdempotencyScope` answers whether two
attempts are "the same" attempt. `signalvev-reference-v0.8`'s
`RevisionGuardedAdmitter` answers whether a caller's view of a
referent's revision is still current. This candidate answers a third,
independent, purely read-only question: given the receipts one attempt
actually has, where -- if anywhere -- is the chain broken, without
inventing evidence for what is simply not yet reached. It makes no
admission or idempotency decision of its own, and none of the three
import or duplicate each other.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `first-broken-edge-flight-recorder-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> Current runtime crosswalk (L6 row); 2. X4 runtime-fagpass -> Falsifiers (item 11); 1. Human text -> 2. Felles primitive verktoy -> COMMUNICATION FLIGHT RECORDER |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v09_validation.py selftest
signalvev_reference_v09_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```
(v0.4 through v0.8 are separate branches, not on this one; each independently regression-clean per its own README.)

9 checks total: one golden-path composition test (`FR0`), plus eight
falsifiers (`FR1`-`FR8`):

- **FR0** (golden path): every stage of the ladder observed, including
  one `EFFECT` branch, reports `COMPLETE`.
- **FR1**: a gap in the middle (`DELIVERED` missing, `READ` present)
  reports `first_broken_edge = DELIVERED`, and the missing stage never
  appears in `reached_stages`.
- **FR2**: a sequence that has simply not progressed past
  `WORK_CONSUMED` yet -- nothing later observed -- reports
  `IN_PROGRESS_NO_BREAK`, never a false `BROKEN`.
- **FR3**: any of the three `EFFECT` branch outcomes
  (`EFFECT_ACCEPTED` / `EFFECT_UNKNOWN` / `NO_EFFECT`) satisfies that
  ladder position.
- **FR4**: a large gap is reported at its earliest missing stage, and no
  stage inside the gap is fabricated into `reached_stages`.
- **FR5**: an empty observed set reports `IN_PROGRESS_NO_BREAK` -- never
  `COMPLETE`, and never an invented `BROKEN` edge from nothing.
- **FR6**: with two separate gaps, only the earliest one is reported,
  matching the Human text's own "forste brutte edge" wording.
- **FR7**: `RELEASED` present without `TERMINAL` is reported `BROKEN` at
  `TERMINAL` -- a later stage's presence never gets read as proof the
  earlier one silently happened.
- **FR8**: `reconstruct()` never mutates the caller's input set, an
  unrecognized stage name raises rather than being silently ignored,
  and v0.1's own `ReceiptTrail`/`STAGE_PREDECESSORS` enforcement is
  confirmed still intact and un-bypassed.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. This candidate generates no trace/correlation/causation
IDs of its own -- it only reconstructs from receipts a caller already
holds, and is scoped to one attempt's ladder, not a multi-child
causation tree.

## Mandatory pre-delivery gate

```
$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=23']
```

No new failures introduced. The one error is the same pre-existing,
PRINCIPAL-scoped `README_METADATA_COUNT_DRIFT` judgment call already
flagged in PR #9 and in every candidate since v0.4, grown by exactly one
for this candidate's own `component.yaml`. (This branch's own tree only
contains v0.1/v0.2/v0.3/v0.9 as candidates -- v0.4 through v0.8 are
separate un-merged branches -- so `DERIVED=23` here, not the higher
count a fully-merged state would eventually show.)

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
reconstruction property over receipts a caller already holds, a
narrower claim than "this works against a real live provider" or "this
generates trace data from a live system."

## Open deviations and assumptions

1. **This candidate does not generate trace/correlation/causation IDs.**
   It assumes a caller already has a set of observed receipt stages for
   one attempt and reconstructs read-only from that. Wiring it to an
   actual trace store or live receipt stream is out of scope here, same
   as every other candidate in this project.
2. **Scoped to one attempt's single ladder**, not a fan-out causation
   tree. The Human text notes "Fan-out ma tillate flere barn uten a
   oppfinne en total order" -- extending this recorder to multi-child
   causation trees is a reasonable future candidate, not done here.
3. **The `EFFECT` branch is treated as a single ladder slot** satisfied
   by any of its three outcomes. This candidate does not itself judge
   whether `EFFECT_UNKNOWN` vs `EFFECT_ACCEPTED` vs `NO_EFFECT` was the
   *correct* outcome -- that judgment belongs to the effect authority,
   not to this read-only reconstructor.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a trace/correlation/causation ID generator.
- Not a canonical-state override -- state-owner remains the truth
  source per the Human text's own grunnlov-kandidat.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.8`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
