# Signalvev Reference Implementation Candidate v0.17 (readiness non-conflation guard)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

Two more of the ten Section 5 negative laws -- explicitly required to
be *"testbare invariants, ikke bare dokumentasjon"* -- have never been
tested by any prior candidate: **`ONLINE != READY`** and
**`TRANSPORT_AVAILABLE != WORK_ALLOWED`**. `signalvev-reference-v0.12`'s
`PresenceEnvelopeGuard` proves a closely related but different thing:
that a presence envelope never carries an illegal
authority/occupancy/work_started/consumed/effect *claim*. It never
proves that a caller must not conflate a weaker presence signal
(`ONLINE`) with the stronger "candidate for work" signal (`READY` /
`ARMED`) in the first place, and it never touches transport
availability at all. This candidate closes that gap.

## Base commit

```
a4ae21e (main, after PR #5+#8+#9+#7+#10-#21)
```

## Branch

`claude/signalvev-reference-v0.17`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.17/readiness-non-conflation-guard-candidate.yaml
A  candidates/signalvev-reference-v0.17/component.yaml
A  candidates/signalvev-reference-v0.17/README.md   (this file)
A  tooling/validator/signalvev_reference_v17_validation.py
```

Nothing in any prior candidate directory is modified. This validator
has **no dependency on any other signalvev-reference candidate** -- it
is a pure, stateless pair of classifiers.

## Why this is not a duplicate of v0.12

See `why_this_is_new_scope_not_a_duplicate` in
`readiness-non-conflation-guard-candidate.yaml`. In short: v0.12
answers "does this presence envelope illegally claim authority?" This
candidate answers a prior, different question -- "does merely being
reachable, or having transport available, by itself qualify as a
signal that work should even be considered?" -- a state-conflation
failure mode, not a claim-injection one. The two compose: a caller
would use this guard's `work_candidate=True` as a pre-filter, then
still need v0.12's guard (or real authority machinery) before treating
anything as permission to act.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `readiness-non-conflation-guard-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> 5. Viktige negative regler -> ONLINE != READY, TRANSPORT_AVAILABLE != WORK_ALLOWED; 1. Human text -> PRESENCE / SERVICE DIRECTORY; 2. X4 runtime-fagpass -> Falsifiers (item 6) |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v17_validation.py selftest
signalvev_reference_v17_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged (run in the
same tree; this branch only carries v0.1/v0.2/v0.3/v0.17 as candidates):

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```

9 checks total: one golden-path composition test (`RC0`), plus eight
falsifiers (`RC1`-`RC8`):

- **RC0** (golden path): `READY` classifies as a work-candidate
  presence state.
- **RC1**: `ONLINE` does **not** classify as a work-candidate state --
  direct proof of `ONLINE != READY`.
- **RC2**: `ARMED` also classifies as a work-candidate state, matching
  falsifier 6's own `READY`-or-`ARMED` pairing.
- **RC3**: `OFFLINE`, `DEGRADED`, and `UNKNOWN` are all **not**
  work-candidate states.
- **RC4**: an unrecognized presence state fails closed with an explicit
  error.
- **RC5**: `transport_available=True` never establishes
  `work_allowed` -- direct proof of `TRANSPORT_AVAILABLE !=
  WORK_ALLOWED`, even for the "positive-looking" case.
- **RC6**: `transport_available=False` also never allows work -- no
  special-casing either direction.
- **RC7**: a non-boolean `transport_available` value fails closed.
- **RC8**: even with both inputs looking maximally favorable
  (`READY` + `True`), this module never produces a combined
  authority-flavored field -- the two reports' shapes stay exactly
  their own documented keys, with no cross-contamination.

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
`DERIVED=34`) this branch is based on. v0.16's own `component.yaml` is
on a separate, still-unmerged sibling branch and is not present on this
branch's own tree (which only carries v0.1/v0.2/v0.3/v0.17 as
candidates).

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, presence infrastructure, or Drive at all -- it proves two
offline non-conflation classification properties over caller-supplied
values, a narrower claim than "this works against a real presence or
transport observation system."

## Open deviations and assumptions

1. **Transport availability is modeled as a plain boolean**, not an
   enum. The Human text explicitly enumerates the six presence states
   but defines no equivalent vocabulary for transport status -- rather
   than inventing one (which would risk introducing a new,
   uncited protocol surface), this candidate uses the simplest
   faithful representation: "could bytes currently move between
   endpoints, yes or no."
2. **This candidate does not implement a combined
   presence-plus-transport decision.** The two functions are
   deliberately kept separate with no merged output, so a future
   candidate composing them (alongside v0.12 and/or real authority
   machinery) is free to decide how they interact rather than inheriting
   an opinion from here.
3. **`work_candidate=True` is a pre-filter fact only.** This candidate
   does not claim that a `READY`/`ARMED` classification is sufficient
   for anything on its own -- v0.12's guard (or real L4 authority
   machinery) still governs whether work may actually proceed.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not an implementation of a real presence or transport observation
  mechanism.
- Not an authority, claim, or effect grant.
- Not a replacement for or duplicate of v0.12's `PresenceEnvelopeGuard`.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.16`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
