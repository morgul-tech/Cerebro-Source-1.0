# Signalvev Reference Implementation Candidate v0.16 (state delta truth-boundary guard)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

The Human text's own Section 5 ("Viktige negative regler") lists ten
short negative laws -- `DELIVERED != CONSUMED`, `ACK != EFFECT`,
`MESSAGE != AUTHORITY`, and so on -- and closes the section with an
explicit mandate: *"Dette skal være testbare invariants, ikke bare
dokumentasjon."* (These shall be testable invariants, not just
documentation.) Most of the ten already have real coverage, woven
through v0.1's `ReceiptTrail`/`STAGE_PREDECESSORS` and the various
falsifier candidates. One has never been tested by any candidate:
**`EVENT != STATE_TRUTH`**.

`STATE_DELTA` is the message class the Human text defines as: *"Informerer
om endring; state-owner er fortsatt sannhetskilden."* (Informs of a
change; the state owner remains the source of truth.) The already
-merged Subject Registry entry for `cerebro.v1.state.delta` names the
exact enforcement this implies: `"authority_requirement":
"source_attribution_and_canonical_state_reread"`. No candidate before
this one reads that field or gives it runtime meaning -- v0.1's
`validate_envelope` accepts `STATE_DELTA` as a valid `message_type` enum
value and stops there. A consumer could, today, treat any well-formed
`STATE_DELTA` payload as truth with nothing in this project to stop it.

## Base commit

```
a4ae21e (main, after PR #5+#8+#9+#7+#10-#21)
```

## Branch

`claude/signalvev-reference-v0.16`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.16/state-delta-truth-boundary-guard-candidate.yaml
A  candidates/signalvev-reference-v0.16/component.yaml
A  candidates/signalvev-reference-v0.16/README.md   (this file)
A  tooling/validator/signalvev_reference_v16_validation.py
```

Nothing in any prior candidate directory is modified. The validator
**imports** (does not copy) `load_registry` from v0.1, used strictly
read-only as a cross-version consistency canary (test `ET8` below) --
it does not modify v0.1's registry file and has no other dependency on
v0.1 or any candidate from v0.2 through v0.15.

## Why this is new scope, not a duplicate

See `why_this_is_new_scope_not_a_duplicate` in
`state-delta-truth-boundary-guard-candidate.yaml`. In short: v0.1
validates `STATE_DELTA`'s envelope shape only. `signalvev-reference-v0.8`'s
`stale-revision-admission-guard` governs a different question entirely
-- whether a stale route/claim revision may admit `DURABLE_COMMAND`
work -- and never touches `STATE_DELTA` or the truth-boundary question.
No candidate before this one reads the `cerebro.v1.state.delta`
registry entry's `authority_requirement` field at all.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `state-delta-truth-boundary-guard-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> 5. Viktige negative regler -> EVENT != STATE_TRUTH; 1. Human text -> 3. Communication classes v0.1 -> STATE_DELTA; 4. Subject Registry v0.1 draft JSON -> entries[cerebro.v1.state.delta].authority_requirement |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v16_validation.py selftest
signalvev_reference_v16_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged (run in the
same tree; this branch only carries v0.1/v0.2/v0.3/v0.16 as candidates):

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```

9 checks total: one golden-path composition test (`ET0`), plus eight
falsifiers (`ET1`-`ET8`):

- **ET0** (golden path): a reread matching the declared state owner
  establishes `TRUTH_ESTABLISHED`, with the reread's own content as
  truth.
- **ET1**: no reread performed at all -- `DELTA_NOT_TRUTH`, direct
  proof of Section 5's own `EVENT != STATE_TRUTH`.
- **ET2**: a reread performed from a source that does not match the
  declared state owner establishes nothing.
- **ET3**: content that would have matched, had a reread been
  performed, still does not establish truth when no reread actually
  happened -- matching content is never a substitute for the reread
  step itself.
- **ET4**: when a fresh reread disagrees with the stale delta that
  prompted it, the reread always wins -- the delta is discarded as
  evidence, never blended with or preferred over the state owner's
  current answer.
- **ET5**: a non-`STATE_DELTA` message type fails closed -- this guard
  never silently extends its truth-boundary logic to a class it was not
  built for.
- **ET6**: a `TRUTH_ESTABLISHED` result from one call never leaks into
  an unrelated later call missing its own reread.
- **ET7**: output shape is exactly the three documented keys, and
  `status` stays consistently coupled to `reason`/`truth`.
- **ET8**: cross-version consistency canary -- v0.1's own,
  already-merged `cerebro.v1.state.delta` registry entry names exactly
  `"source_attribution_and_canonical_state_reread"` as its
  `authority_requirement`, read (never modified) to prove this
  candidate implements what the registry already declares, not an
  invented requirement.

All tests run fully offline: no socket, subprocess, filesystem write
(other than reading v0.1's already-committed registry JSON), or network
call.

## Mandatory pre-delivery gate

```
$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=35']
```

No new failures introduced. The one error is the same pre-existing,
PRINCIPAL-scoped `README_METADATA_COUNT_DRIFT` judgment call already
flagged since PR #9, grown by exactly one for this candidate's own
`component.yaml` on top of the fully-merged main (`a4ae21e`, `DERIVED=34`
after PR #16-#21) this branch is based on.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, state storage, or Drive at all -- it proves an offline
truth-boundary classification property over caller-supplied dicts plus
one read-only registry-file canary, a narrower claim than "this works
against a real canonical state store."

## Open deviations and assumptions

1. **This candidate does not implement the reread mechanism itself.**
   `reread` is taken as a plain caller-supplied input; how a real
   reread against a live state owner would actually be performed is out
   of scope, matching every other candidate's treatment of inputs it
   consumes rather than produces.
2. **`declared_state_owner_ref` equality is a plain string comparison.**
   Whether that equality check should instead route through v0.14's
   workload-identity attestation binder (so the "owner" claim is itself
   attested, not just string-matched) is a reasonable future
   composition -- not done here to keep this candidate's own scope on
   the truth-boundary question alone.
3. **`delta_payload` is accepted as a parameter but deliberately never
   read past the initial call** -- it exists only so a caller's full
   `STATE_DELTA` envelope can be passed through without a separate
   destructuring step; the function's actual truth decision never
   depends on it.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not an implementation of a real state-reread mechanism.
- Not an authority, claim, or effect grant -- `TRUTH_ESTABLISHED` is a
  content fact about the state owner's current answer only.
- Not a replacement for v0.1's `validate_envelope`.
- Not a duplicate of v0.8's `stale-revision-admission-guard`.
- Not a modification of v0.1's actual registry file.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.15`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
