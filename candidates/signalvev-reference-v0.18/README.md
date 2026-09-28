# Signalvev Reference Implementation Candidate v0.18 (attested work-consumption gate)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

This candidate is not built against a newly-discovered gap in the
source text -- it composes two already-merged, independently-built
candidates that have never been wired together. X4's own normative
text on receipts says: *"WORK_CONSUMED requires the exact actor/workload
to claim the item. WORK_STARTED requires worker-owned evidence."*
`signalvev-reference-v0.1`'s `ReceiptTrail` enforces the receipt partial
order but never checks whether the `actor_id` claiming `WORK_CONSUMED`
or `WORK_STARTED` is actually who it says it is.
`signalvev-reference-v0.14`'s `bind_source_identity` proves whether a
claimed identity is attested, but was never wired into any receipt
-emission decision. As of `main` today, nothing stops an unattested
`actor_id` from successfully claiming `WORK_CONSUMED`. This candidate
closes that composition gap -- in the same spirit as
`signalvev-reference-v0.4`'s end-to-end composition of v0.1 and v0.2.

Before building this, two other candidates for the last untested
Section 5 negative laws (`DELIVERED != CONSUMED`, `MESSAGE != AUTHORITY`)
were considered and explicitly rejected as redundant: both are already
well covered -- `DELIVERED != CONSUMED` directly by v0.1's own
`test_falsifier_01_delivered_as_work_consumed`, and `MESSAGE !=
AUTHORITY` structurally throughout the schema and every candidate's own
"explicitly not authority" discipline. Building isolated tests for
either would have been thin duplication, not new coverage.

## Base commit

```
a4ae21e (main, after PR #5+#8+#9+#7+#10-#21)
```

## Branch

`claude/signalvev-reference-v0.18`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.18/attested-work-consumption-gate-candidate.yaml
A  candidates/signalvev-reference-v0.18/component.yaml
A  candidates/signalvev-reference-v0.18/README.md   (this file)
A  tooling/validator/signalvev_reference_v18_validation.py
```

Nothing in any prior candidate directory is modified. The validator
**imports** (does not copy) `ReceiptTrail`, `ReceiptError` from v0.1
and `bind_source_identity` from v0.14, both unmodified. It has no other
dependency on any candidate from v0.2 through v0.13 or v0.15 through
v0.17.

## Why this is new scope, not a duplicate

See `why_this_is_new_scope_not_a_duplicate` in
`attested-work-consumption-gate-candidate.yaml`. In short: neither v0.1
nor v0.14 tests this composition. v0.1's `ReceiptTrail.emit()` only
checks `actor_id`/`generation` for `RELEASED` (falsifier 8) and never
for `WORK_CONSUMED`/`WORK_STARTED`. v0.14's `bind_source_identity` has
no receipt-trail awareness at all. This is the first candidate to wire
them together.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `attested-work-consumption-gate-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> First-wave normative contract draft -> Receipts; 1. Human text -> L1 IDENTITY; 1. Human text -> WORKLOAD IDENTITY / ATTESTATION |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v18_validation.py selftest
signalvev_reference_v18_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged (run in the
same tree; this branch carries v0.1/v0.2/v0.3 plus every already-merged
candidate through v0.15, and its own v0.18):

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
signalvev_reference_v14_validation selftest: 9/9 PASS
```

9 checks total: one golden-path composition test (`AG0`), plus eight
falsifiers (`AG1`-`AG8`):

- **AG0** (golden path): a `TRUSTED_BOUND` identity matching the
  trail's own attempt owner emits `WORK_CONSUMED` successfully.
- **AG1**: no attestation (`ASSERTED_ONLY`) is rejected, and the trail
  is provably unmutated.
- **AG2**: a mismatched attestation (e.g. stale `custody_ref`) is
  rejected, trail unmutated.
- **AG3**: a `TRUSTED_BOUND` identity for the *wrong* actor -- not the
  trail's own attempt owner -- is still rejected.
- **AG4**: v0.1's own predecessor rule still applies underneath the
  gate -- attempting `WORK_CONSUMED` before `DELIVERED` is rejected
  even with a fully trusted identity.
- **AG5**: a stage outside `{WORK_CONSUMED, WORK_STARTED}` fails closed
  -- this gate does not overreach into stages X4's text never ties to
  identity.
- **AG6**: a rejected attempt is not a poisoned state -- supplying real
  attestation on a later call for the same trail still succeeds.
- **AG7**: a trusted result for one trail never leaks into an unrelated
  trail's gate check.
- **AG8**: output shape is exactly the two documented keys, and
  `status` stays consistently coupled to `reason`.

All tests run fully offline: no socket, subprocess, filesystem write,
or network call.

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
`component.yaml` on top of the fully-merged main (`a4ae21e`,
`DERIVED=34`) this branch is based on.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, identity infrastructure, or Drive at all -- it proves an
offline composition property between two already-independently-tested
primitives, a narrower claim than "this works against a real
receipt-emitting worker and a real attestation system."

## Open deviations and assumptions

1. **This gate only covers WORK_CONSUMED/WORK_STARTED.** A future
   candidate could extend identity-gating to other actor-scoped stages
   (e.g. requiring attestation for `RELEASED` too, on top of v0.1's
   existing actor/generation match check) -- not done here to stay
   tightly scoped to what X4's own text explicitly names.
2. **The "attempt owner" check (`AG3`) is a plain string comparison**
   against `trail.actor_id`/`trail.generation`, not itself an attested
   fact -- it relies on the trail having been opened correctly in the
   first place, which is out of this candidate's scope.
3. **This candidate does not persist or retry rejected attempts.**
   Each call is independent and stateless from the gate's own
   perspective; only the `ReceiptTrail` instance itself carries state,
   exactly as v0.1 already designed it.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a modification of v0.1's `ReceiptTrail` or v0.14's
  `bind_source_identity`.
- Not an extension of identity-gating beyond `WORK_CONSUMED`/
  `WORK_STARTED`.
- Not an implementation of a real attestation mechanism.
- Not a replacement for v0.1's own structural receipt rules -- they
  remain fully in force underneath this gate.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.17`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
