# Signalvev Reference Implementation Candidate v0.4 (end-to-end reference stack)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

Communication Stack v0.1's own **WAY HOME** section says: build one small
reference stack chaining `MESSAGE -> SIGNAL -> REQUEST/REPLY -> RECEIPT
-> TRACE -> ARTIFACT POINTER`, and only once that works end-to-end
should durability, service discovery, or workflow be widened. v0.1, v0.2
and v0.3 each delivered and offline-tested exactly one primitive in
isolation. None of them, individually or together, proved the chain
*composes*. This candidate is that proof: it imports the already-merged
v0.1/v0.2 reference code unmodified and runs it through one shared
scenario, plus falsifiers that specifically target composition failures
(one primitive's output leaking into another's authority) rather than
re-testing any single primitive's own internal correctness again.

## Base commit

```
01a24d3 (main, after PR #5 + PR #8 + PR #9 + PR #7 -- rebased per PRINCIPAL HOLD)
```

Originally prepared against `4e756e7` (before PR #7/PR #9 merged) and
delivered on branch `claude/signalvev-reference-v0.4`. PRINCIPAL's
review returned **HOLD: rebase after PR #9 and document that the
canonical validator runs without error.** Rebased here onto `01a24d3`
(PR #9 and PR #7 both merged) via a clean cherry-pick of the original
commit -- file content is unchanged, only the base moved forward. See
"Mandatory pre-delivery gate" below for the updated, honest result of
that re-verification.

## Branch

`claude/signalvev-reference-v0.4` (re-delivered; replaces the prior
upload of this branch, which was based on the pre-PR#7/PR#9 `main`)

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.4/end-to-end-scenario-candidate.yaml
A  candidates/signalvev-reference-v0.4/component.yaml
A  candidates/signalvev-reference-v0.4/README.md   (this file)
A  tooling/validator/signalvev_reference_v04_validation.py
```

Nothing in `candidates/signalvev-reference-v0.1/` or
`candidates/signalvev-reference-v0.2/` is modified. The validator
**imports** (does not copy) `validate_envelope`, `STAGE_PREDECESSORS`,
`ReceiptTrail`, `_base_msg` from v0.1 and `RequestReplyMatcher`,
`make_pointer`, `verify_and_consume` from v0.2.

## Why this branches from `main`, not from v0.3 (PR #7)

The chain's TRACE link is exactly what v0.3's `FlightRecorder` already
implements. This candidate does **not** import it, because PR #7 was
still open and unmerged when this candidate was prepared, and every
candidate in this project has kept itself independent of other unmerged
candidate branches (v0.3 did the same toward v0.2). TRACE is instead
reimplemented locally (`EndToEndTrace.first_broken_edge()`) against
v0.1's merged `STAGE_PREDECESSORS` -- same algorithm, independent code.
See `end-to-end-scenario-candidate.yaml`'s `chain_links` entry for TRACE
for the full reasoning, including a note that a follow-up could point
this candidate at v0.3's `FlightRecorder` once PR #7 merges.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `end-to-end-scenario-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> WAY HOME; 1. Human text -> 4. Grunnlov-kandidat; 1. Human text -> 7. Naavaerende modningsrekkefolge |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v04_validation.py selftest
signalvev_reference_v04_validation selftest: 8/8 PASS
$ echo $?
0
```

Regression checks -- both prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
```

8 checks total, one golden-path composition test plus seven
composition-specific falsifiers (`E2E1`-`E2E8`):

- **E2E1** (golden path): the full chain succeeds end-to-end in one run.
- **E2E2**: a SIGNAL alone, with no explicit `ReceiptTrail.emit()` call,
  never advances a receipt trail.
- **E2E3**: a `TIMEOUT_UNKNOWN` request/reply outcome cannot be used to
  reach `WORK_STARTED`.
- **E2E4**: a request/reply outcome string is not a valid receipt stage
  at all -- `ReceiptTrail.emit()` rejects it outright.
- **E2E5**: TRACE correctly finds the first broken edge on a trail built
  through the real composed pipeline (not a synthetic fixture), proving
  the local reimplementation is faithful.
- **E2E6**: an artifact pointer is not consumed before its attempt's
  trail reaches `RELEASED`.
- **E2E7**: every message_type used across the chain independently
  passes `validate_envelope` -- schema conformance holds at every hop.
- **E2E8**: a schema-valid, plausible-looking `source` claim never, by
  itself, advances a receipt trail (claim vs. evidence stays separate
  across the whole chain, not just within one primitive).

All tests run fully offline: no socket, subprocess, filesystem write, or
network call.

## Mandatory pre-delivery gate (this candidate's own dogfooding)

**Original run** (against base `4e756e7`, before PR #7/PR #9): a
full-repo YAML scan and `canonical_foundation.py validate --source-root .`
caught and fixed a real bug this candidate had introduced -- a `(PR #7,
...)` citation in `component.yaml`'s `explicitly_no_dependency_on` list
broke YAML parsing (a space before `#` starts a comment outside a block
scalar, truncating the item). Fixed by rewording to "PR nr. 7" and
wrapping in a folded block scalar (`>-`). At that time,
`canonical_foundation.py` still crashed on the branch, but only from the
already-known, separately-fixed `request-reply-candidate.yaml` bug (PR
#9, not yet merged) -- not from anything in this candidate.

**Re-run after rebase onto `01a24d3`** (PR #9 and PR #7 both merged),
per PRINCIPAL's HOLD instruction to document the result honestly:

```
$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=23']
```

**The crash is gone.** The full-repo YAML scan is completely clean (0
failures) -- PR #9's fix resolved it, confirmed independently here.
`canonical_foundation.py` itself no longer raises an unhandled
`yaml.scanner.ScannerError`; it now runs to completion and returns a
structured result.

**One error remains, and this candidate is transparent that it is not
fully clean:** `README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=23`.
This is the *same* pre-existing drift first surfaced during the PR #9
work (then `DECLARED=19:DERIVED=21`), which was deliberately left as a
separate, PRINCIPAL-scoped judgment call rather than silently fixed --
matching this project's own established precedent for the original
421->422 rule-count issue. The count has grown by exactly one for each
of PR #9's merge (no change), PR #7's merge (+1, v0.3's own
`component.yaml`), and now this candidate's own `component.yaml` (+1
more, from 22 to 23 on this branch). Independently verified by counting
`component.yaml` files directly (`find . -name component.yaml | wc -l`),
not just trusting the validator's derived number.

The open question -- whether `candidates/*/component.yaml` files should
count toward the root README's "Komponenter" total at all, or whether
`canonical_foundation.py` should exclude the `candidates/` subtree from
that specific count -- is an architecture decision, not a bug in this
candidate, and is left for PRINCIPAL, consistent with how it was already
deferred once in PR #9's delivery note.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from v0.1/v0.2/v0.3: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger at all -- it proves that
the *offline reference primitives* compose with each other, which is a
narrower and different claim than "this works on real Signalvev
transport."

## Open deviations and assumptions

1. **TRACE is reimplemented, not imported from v0.3.** PR #7 has since
   merged, so unifying on v0.3's `FlightRecorder` is now possible.
   PRINCIPAL's HOLD on this rebase asked specifically for a rebase and
   gate re-verification, not a scope change, so that unification is
   intentionally left as a follow-up candidate rather than folded in
   here without being asked.
2. **`canonical_foundation.py` no longer crashes on this branch** --
   confirmed after this rebase. It still reports one error
   (`README_METADATA_COUNT_DRIFT`), which is a pre-existing,
   PRINCIPAL-scoped judgment call unrelated to this candidate's own
   files -- see "Mandatory pre-delivery gate" above for the full,
   honest result.
3. **The golden-path scenario (E2E1) is one specific, hand-constructed
   attempt**, not a property-based or randomized test across the full
   space of possible message sequences. It demonstrates that composition
   *can* work cleanly; the seven falsifiers demonstrate specific ways it
   must *not* silently break. Broader scenario coverage (concurrent
   attempts, multiple signals for one command, interleaved requests) is
   out of scope here.
4. **This candidate does not itself decide whether the "senere" wave
   (Durable Workflow, multi-node federation, store-scale routing) is now
   unblocked.** WAY HOME frames end-to-end composition as a
   *precondition* for PRINCIPAL to consider widening scope, not an
   automatic trigger. This README presents the evidence; the decision is
   PRINCIPAL's.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a claim that Durable Workflow, federation, or store-scale routing
  are now in scope -- see open deviation #4.
- Not a modification of `candidates/signalvev-reference-v0.1/`,
  `candidates/signalvev-reference-v0.2/`, or
  `candidates/signalvev-reference-v0.3/`.
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
