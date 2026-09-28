# Signalvev Reference Implementation Candidate v0.10 (reply currentness and effectful-timeout guard)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass's own crosswalk table names a material L3 gap: "Ad
hoc status reads exist. No common timeout, NO_RESPONDER, correlation,
bounded reply or currentness contract." Falsifier 9, verbatim: "A reply
lacking current referent/version is treated as canonical status." X4's
own first-wave normative contract draft adds a second, sharper rule
under "Request/reply and pointers": "A timed-out request that could
have crossed an effect boundary cannot be retried as if it were a
read-only question." v0.1's `test_falsifier_09_stale_reply_not_canonical`
has a passing check, but it is a single inline closure with one
hardcoded scenario -- it does not cover `NO_RESPONDER`, does not cover
`TIMEOUT_UNKNOWN`, and does not implement the effect-boundary retry
rule at all. This candidate builds the real, reusable, stateless
classifier for both.

## Base commit

```
01a24d3 (main, after PR #5 + PR #8 + PR #9 + PR #7)
```

## Branch

`claude/signalvev-reference-v0.10`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.10/reply-currentness-effectful-timeout-guard-candidate.yaml
A  candidates/signalvev-reference-v0.10/component.yaml
A  candidates/signalvev-reference-v0.10/README.md   (this file)
A  tooling/validator/signalvev_reference_v10_validation.py
```

Nothing in any prior candidate directory is modified. This validator
has **no dependency on any other signalvev-reference candidate** -- it
is a pure, stateless classifier over caller-supplied inputs (a reply
outcome, an optional response revision, the referent's current
revision, and the original request's effect_class). It does not import
`ReceiptTrail` or anything else from v0.1.

## Why this is not a duplicate of v0.8 or v0.9

`signalvev-reference-v0.8`'s `RevisionGuardedAdmitter` is a write-path
gate: given a snapshot revision, may this caller's *work* be admitted.
`signalvev-reference-v0.9`'s `FirstBrokenEdgeRecorder` is a read-only
reconstruction of one attempt's receipt chain. This candidate answers a
third, independent, read-path question: given a reply a requester
already received, may it be treated as canonical current status, and if
the request instead timed out, is even a read-only retry safe. It takes
`current_revision` as a plain input rather than importing or maintaining
a ledger, so it neither imports nor duplicates v0.8's ledger-holding
admitter, and it makes no receipt-chain judgment at all.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `reply-currentness-effectful-timeout-guard-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> Current runtime crosswalk (L3 row); 2. X4 runtime-fagpass -> Falsifiers (item 9); 2. X4 runtime-fagpass -> First-wave normative contract draft -> Request/reply and pointers; 1. Human text -> REQUEST / REPLY PRIMITIVE |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v10_validation.py selftest
signalvev_reference_v10_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```
(v0.4 through v0.9 are separate branches, not on this one; each independently regression-clean per its own README.)

9 checks total: one golden-path composition test (`SR0`), plus eight
falsifiers (`SR1`-`SR8`):

- **SR0** (golden path): a `RESPONSE` whose `response_revision` matches
  the referent's `current_revision` is `CURRENT`.
- **SR1**: a `RESPONSE` carrying a non-current revision is `STALE`,
  never `CURRENT` -- the core falsifier 9 scenario.
- **SR2**: `NO_RESPONDER` classifies as `UNKNOWN`, never canonical
  status.
- **SR3**: `TIMEOUT_UNKNOWN` on a `READ_ONLY` request is safe to retry
  as read-only.
- **SR4**: `TIMEOUT_UNKNOWN` on a `WRITE_POSSIBLE` request blocks
  retry-as-read-only -- X4's own effect-boundary rule.
- **SR5**: `TIMEOUT_UNKNOWN` on an `UNKNOWN` effect_class also fails
  closed, the same as `WRITE_POSSIBLE` -- absence of proof it was
  read-only is not proof that it was.
- **SR6**: a `RESPONSE` with no `response_revision` raises, never
  defaults to current.
- **SR7**: an unrecognized reply outcome raises, never silently
  classified.
- **SR8**: the classifier is a pure function -- two calls for the same
  referent with different `current_revision` values never interfere via
  hidden state -- and an unrecognized `effect_class` raises.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. This candidate is not a request/reply transport
implementation -- it classifies a reply a caller already holds.

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
contains v0.1/v0.2/v0.3/v0.10 as candidates -- v0.4 through v0.9 are
separate un-merged branches -- so `DERIVED=23` here, not the higher
count a fully-merged state would eventually show.)

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
classification property over a reply a caller already holds, a
narrower claim than "this works against a real live provider."

## Open deviations and assumptions

1. **This candidate does not implement the request/reply transport
   itself** -- no timeout timer, no correlation-id matching, no actual
   NO_RESPONDER detection. It classifies outcomes a caller already
   determined. Wiring it to an actual request/reply primitive is out of
   scope here, same as every other candidate in this project.
2. **Stale-schema replies are not classified by this candidate.** X4's
   text also names "schema mismatch" alongside stale answer as a
   distinguishable outcome; this candidate scopes to currentness
   (revision match) and effectful-timeout retry-safety only. Schema
   compatibility checking is a reasonable future candidate, not done
   here.
3. **`safe_to_retry_as_read_only` is advisory, not enforcement.** This
   candidate does not itself block a retry -- it only classifies
   whether one would be safe if attempted. Wiring that into an actual
   retry mechanism is out of scope here.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a request/reply transport implementation.
- Not a canonical-state override -- state-owner remains the truth
  source per the Human text's own grunnlov-kandidat.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.9`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
