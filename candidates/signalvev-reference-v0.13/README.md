# Signalvev Reference Implementation Candidate v0.13 (artifact pointer bytes-and-custody consumption guard)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass's own crosswalk table names a material L5 gap:
"Provider snapshots, Drive ZIPs/hashes and local manifests exist. No
standard artifact pointer with owner, hash, size, MIME, custody and
expiry. A Drive link is a location, not a file-transfer contract."
Falsifier 10, verbatim: "An artifact pointer with wrong bytes/hash is
consumed." X4's own normative draft adds the operative rule: "An
artifact pointer names immutable artifact identity, owner, content
hash, size, MIME/schema, provenance, access/custody requirement and
location hint. Receiver verifies bytes **and** current access before
use." v0.1's `test_falsifier_10_artifact_hash_mismatch_blocks_consumption`
has a passing check, but it is a single inline closure that checks hash
alone -- it does not check declared size, and critically does not
implement the "bytes AND current access" conjunction: a pointer with
byte-perfect content but unverified custody must still be rejected,
which v0.1's test never exercises. This candidate builds the real,
reusable, stateless guard for the full conjunction.

## Base commit

```
01a24d3 (main, after PR #5 + PR #8 + PR #9 + PR #7)
```

## Branch

`claude/signalvev-reference-v0.13`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.13/artifact-pointer-consumption-guard-candidate.yaml
A  candidates/signalvev-reference-v0.13/component.yaml
A  candidates/signalvev-reference-v0.13/README.md   (this file)
A  tooling/validator/signalvev_reference_v13_validation.py
```

Nothing in any prior candidate directory is modified. This validator
has **no dependency on any other signalvev-reference candidate** -- it
is a pure, stateless guard using only Python's stdlib `hashlib` over
caller-supplied bytes and declarations (`declared_hash`,
`declared_size`, `actual_bytes`, `custody_verified`).

## Why this is not a duplicate of v0.7, v0.8, v0.10, v0.11, or v0.12

Each of `signalvev-reference-v0.7` through `-v0.12` classifies a
`DURABLE_COMMAND`, `REQUEST`/`RESPONSE`, or `PRESENCE` message, or
reconstructs a receipt chain. This candidate answers an independent
question scoped to `ARTIFACT_POINTER` consumption only: given declared
identity fields (hash, size) and a custody-verification flag, may the
actual bytes be consumed. None of the six import or duplicate each
other, and this candidate makes no admission, currentness,
replay-safety, or presence-structural decision of its own.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `artifact-pointer-consumption-guard-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> Current runtime crosswalk (L5 row); 2. X4 runtime-fagpass -> Falsifiers (item 10); 2. X4 runtime-fagpass -> First-wave normative contract draft -> Request/reply and pointers; 1. Human text -> ARTIFACT POINTER / FILE TRANSFER |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v13_validation.py selftest
signalvev_reference_v13_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```
(v0.4 through v0.12 are separate branches, not on this one; each independently regression-clean per its own README.)

9 checks total: one golden-path composition test (`AP0`), plus eight
falsifiers (`AP1`-`AP8`):

- **AP0** (golden path): matching hash, matching size, and verified
  custody is `CONSUMED`.
- **AP1**: a hash mismatch alone blocks consumption -- the core
  falsifier 10 scenario.
- **AP2**: a size mismatch alone (correct hash, wrong declared size)
  blocks consumption.
- **AP3**: byte-perfect content with unverified custody is still
  `REJECTED` -- X4's own "bytes AND current access" conjunction, the
  invariant v0.1 never tested.
- **AP4**: hash, size, and custody violations occurring together are
  all reported, not just the first one found.
- **AP5**: an empty-payload edge case (`declared_size=0`) is handled
  correctly.
- **AP6**: a single-byte content difference still triggers a hash
  mismatch and blocks consumption.
- **AP7**: the guard is a pure function -- a prior `CONSUMED` result
  never leaks into an unrelated later call, and the caller's bytes are
  never mutated.
- **AP8**: output shape is exactly the two documented keys, and
  `status`/`reasons` stay consistently coupled (`CONSUMED` iff no
  reasons, `REJECTED` iff at least one).

All tests run fully offline: no socket, subprocess, filesystem write, or
network call, other than Python's own stdlib `hashlib`. This candidate
is not a file-transport or Drive-download implementation -- it verifies
bytes and a custody flag a caller already holds.

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
contains v0.1/v0.2/v0.3/v0.13 as candidates -- v0.4 through v0.12 are
separate un-merged branches -- so `DERIVED=23` here, not the higher
count a fully-merged state would eventually show.)

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or file delivery at all -- it proves an offline
verify-before-consume property over bytes and a custody flag a caller
already holds, a narrower claim than "this works against a real live
provider" or "this is a file-transfer implementation."

## Open deviations and assumptions

1. **This candidate does not implement custody/access verification
   itself** -- `custody_verified` is taken as a plain caller-supplied
   input. What counts as valid custody verification for a given
   artifact store (Drive, local disk, a provider) is out of scope here,
   same as every other candidate in this project.
2. **MIME/content-type and expiry are not checked.** The Human text and
   X4's normative draft both list MIME/schema and expiry alongside
   hash, size, and custody as required pointer fields; this candidate
   scopes to the bytes-and-custody gate the falsifier and normative
   text name explicitly. A future candidate could extend this guard
   with MIME/schema and expiry checks.
3. **All violations are reported as a flat set of named reasons**, not
   a structured per-field diff. A future candidate could report actual
   vs. declared values alongside each reason if that proves useful for
   diagnostics.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a file-transport or Drive-download implementation.
- Not a custody/access verification mechanism -- this candidate takes
  `custody_verified` as a plain input.
- Not a canonical-state override -- state-owner remains the truth
  source per the Human text's own grunnlov-kandidat.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.12`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
