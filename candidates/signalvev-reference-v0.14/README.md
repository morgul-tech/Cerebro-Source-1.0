# Signalvev Reference Implementation Candidate v0.14 (workload identity attestation binder)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

All 12 of X4 runtime-fagpass's originally-numbered falsifiers now have
real, source-cited reference coverage across `signalvev-reference-v0.1`
and `-v0.5` through `-v0.13`. This candidate is not a 13th closure of
that same list -- it opens a **separately named** material gap from the
same X4 crosswalk table: the **L1 Identity** row. Verbatim: *"No trusted
workload attestation joining actor/generation to current
machine/session/custody. Envelope `source` alone must remain a claim."*
The Human text's own maturation ordering ("Nåværende modningsrekkefølge")
explicitly lists "Workload Identity/Attestation" under **Tredje bølge**
(third wave) -- scope the project has always intended to reach once the
first two waves closed, which they now have.

v0.1's own `validate_envelope` checks that `source.principal_ref` is a
non-empty string and stops there. Every other field in
`CEREBRO_MESSAGE_V1`'s `source` object -- `actor_id`, `generation`,
`workload_ref`, `machine_ref`, `session_ref`, `custody_ref` -- is
schema-typed as nullable and never checked for truthfulness by any
existing candidate. An envelope can claim any actor/generation/
machine/custody combination it likes and pass every existing offline
test in this project. This candidate builds the missing binder that
keeps a bare claim and an independently-obtained attestation record
distinct, per X4's own normative sentence: *"A separate trusted
identity/authority check must bind source actor, generation, workload
and custody to the current state owner. The envelope may carry an
authority reference; it may not mint one."*

## Base commit

```
b1109b8 (main, after PR #5 + #8 + #9 + #7 + #10-#15)
```

## Branch

`claude/signalvev-reference-v0.14`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.14/workload-identity-attestation-candidate.yaml
A  candidates/signalvev-reference-v0.14/component.yaml
A  candidates/signalvev-reference-v0.14/README.md   (this file)
A  tooling/validator/signalvev_reference_v14_validation.py
```

Nothing in any prior candidate directory is modified. This validator
has **no dependency on any other signalvev-reference candidate** -- it
is a pure, stateless binder over two caller-supplied dicts (a claimed
source and an attestation record). It does not import or duplicate
v0.1's `validate_envelope`.

## Why this is new scope, not a duplicate or a 13th falsifier closure

See `why_this_is_new_scope_not_a_duplicate` in
`workload-identity-attestation-candidate.yaml`. In short: this is not
one of the 12 numbered items in X4's "Falsifiers for one offline
reference arc" list -- it is a distinct, separately-named material gap
from the same document's **crosswalk table** (the L1 Identity row), the
only crosswalk row no prior candidate has touched at all. v0.1's schema
validator takes the envelope's `source` object as given, well-formed
input; this candidate answers a prior question the schema never asks --
is the claim itself backed by anything, and does it still match.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `workload-identity-attestation-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> L1 IDENTITY; 1. Human text -> WORKLOAD IDENTITY / ATTESTATION; 2. X4 runtime-fagpass -> Current runtime crosswalk (L1 row); 2. X4 runtime-fagpass -> First-wave normative contract draft -> Envelope; 3. CEREBRO_MESSAGE_V1 draft JSON Schema -> properties.source |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v14_validation.py selftest
signalvev_reference_v14_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged (run in the
same tree, this branch only carries v0.1/v0.2/v0.3/v0.14 as candidates;
v0.4 through v0.13 are separate branches):

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```

9 checks total: one golden-path composition test (`WI0`), plus eight
falsifiers (`WI1`-`WI8`):

- **WI0** (golden path): a claimed source matching an attestation record
  on all six attestable fields is `TRUSTED_BOUND`.
- **WI1**: no attestation supplied at all -- `ASSERTED_ONLY`, never
  `TRUSTED_BOUND`. Direct proof of X4's "source alone must remain a
  claim."
- **WI2**: a single mismatched field (`actor_id`) is reported by exact
  name.
- **WI3**: a `custody_ref` mismatch alone -- everything else matching --
  still blocks full binding. The crosswalk's own named triple is
  "machine/session/custody"; stale custody must be caught on its own.
- **WI4**: multiple simultaneous mismatches (`generation`,
  `machine_ref`) are all reported together, not just the first.
- **WI5**: a `None` claimed field never counts as bound, even if the
  attestation record happens to hold a concrete value for it -- an
  absent assertion is not a verified one.
- **WI6**: `principal_ref` is out of this binder's scope entirely --
  v0.1's `validate_envelope` already owns that check; this candidate
  never reads a `principal_ref` key from the attestation record at all
  (a real `KeyError` would fire if it tried).
- **WI7**: a `TRUSTED_BOUND` result is an identity fact only, never an
  authority grant -- the output carries no `authority`/`effect` field,
  and a bound result for one identity never leaks into an unrelated
  later call for a different identity.
- **WI8**: output shape is exactly the three documented keys, and
  `status` stays consistently coupled to `mismatched_fields`/
  `bound_fields` across all three possible statuses.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. This candidate does not implement any real attestation
mechanism (OS-level, session-token, or custody-ledger) -- it verifies
agreement between two dicts a caller already holds.

## Mandatory pre-delivery gate

The gate caught a real YAML bug before this candidate was shown to
anyone: `component.yaml`'s `interfaces.input`/`interfaces.output` list
items originally wrote `claimed_source (dict: actor_id, ...)` -- the
unquoted colon right after `dict` inside a plain-scalar list item broke
block-mapping parsing (`yaml.scanner.ScannerError: while scanning a
simple key ... could not find expected ':'`), the same class of bug
found and fixed in v0.12's own pre-delivery gate. Fixed by rewriting
both list items as explicit folded block scalars (`- >-`).

```
$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=29']
```

No new failures introduced. The one error is the same pre-existing,
PRINCIPAL-scoped `README_METADATA_COUNT_DRIFT` judgment call already
flagged since PR #9, grown by exactly one for this candidate's own
`component.yaml` on top of the fully-merged main (`b1109b8`) this
branch is based on.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, identity infrastructure, or Drive at all -- it proves an
offline claim-vs-attestation binding property over two dicts a caller
already holds, a narrower claim than "this works against a real
OS-level or session-level attestation system."

## Open deviations and assumptions

1. **This candidate does not implement the attestation check itself.**
   `attestation` is taken as a plain caller-supplied input. How
   machine/session/custody are actually verified in a live system (an
   OS-level attestation API, a signed session token, a custody ledger)
   is out of scope here, matching every other candidate's treatment of
   inputs it consumes rather than produces.
2. **The six attestable fields are a fixed, hand-picked set** --
   everything in `CEREBRO_MESSAGE_V1`'s `source` object except
   `principal_ref`. The Human text and X4's crosswalk name slightly
   different subsets across three different sentences ("machine/
   session/custody" in the crosswalk row; "workload + custody" in the
   normative draft's binding sentence; "workload/actor/generation/
   machine/custody" in the L1 layer description) -- this candidate
   binds the union of all named fields rather than picking one
   sentence's narrower list, since a caller can always choose to ignore
   fields it does not attest.
3. **A `None` claimed field is always treated as unbindable**, even
   against a concrete attested value (WI5). This is a deliberate
   fail-closed choice, not the only defensible one -- a future candidate
   could instead treat a `None` claim as "no assertion made, so no
   contradiction possible" and exclude it from `mismatched_fields`
   entirely. Left as-is here to match this project's established
   fail-closed precedent (e.g. v0.6's `MACHINE_STATE_CLASS` unrecognized
   -state handling).

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not an implementation of a real attestation mechanism.
- Not an authority, claim, or effect grant -- `TRUSTED_BOUND` is an
  identity fact only.
- Not one of the 12 originally-numbered X4 falsifiers.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.13`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
