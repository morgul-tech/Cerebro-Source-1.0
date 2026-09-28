# Signalvev Reference Implementation Candidate v0.15 (schema/version compatibility gate)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

The Human text's own maturation ordering ("Nåværende modningsrekkefølge")
names three items under **Tredje bølge** (third wave): Communication
Flight Recorder, Workload Identity/Attestation, and Schema/version
compatibility. The first two already have reference coverage
(`signalvev-reference-v0.3`/`-v0.9` for the flight recorder,
`signalvev-reference-v0.14` for workload identity attestation). This
candidate closes the third and last one.

The real Subject Registry v0.1 draft -- already merged into `main` as
part of `signalvev-reference-v0.1` -- carries a `compatibility` string
on every one of its seven entries: `exact_schema_version_or_typed_hold`,
`version_negotiated_before_reply` (twice), `no_implicit_upgrade;
unknown_version_holds`, `unknown_stage_holds`, and
`content_type_and_schema_version_checked`. No candidate before this one
ever reads that field. v0.1's own `validate_envelope` enforces exactly
one hardcoded `schema_version` const (`"1.0.0-draft"`) and stops there --
it never implements a typed HOLD for an unknown version, never
distinguishes the five different named policies, and never proves X4's
own "no implicit upgrade" rule against a version string that merely
looks newer.

## Base commit

```
b1109b8 (main, after PR #5 + #8 + #9 + #7 + #10-#15)
```

## Branch

`claude/signalvev-reference-v0.15`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.15/schema-version-compatibility-gate-candidate.yaml
A  candidates/signalvev-reference-v0.15/component.yaml
A  candidates/signalvev-reference-v0.15/README.md   (this file)
A  tooling/validator/signalvev_reference_v15_validation.py
```

Nothing in any prior candidate directory is modified. The validator
**imports** (does not copy) `load_registry` from v0.1, used strictly
read-only as a cross-version consistency canary (test `SC7` below) --
it does not modify v0.1's registry file and has no other dependency on
v0.1 or any candidate from v0.2 through v0.14.

## Why this is new scope, not a duplicate

See `why_this_is_new_scope_not_a_duplicate` in
`schema-version-compatibility-gate-candidate.yaml`. In short: this is
the first candidate to give the registry's own `compatibility` field
runtime meaning at all. It does not touch `validate_envelope`'s schema
-shape checks, does not duplicate v0.9's flight recorder or v0.14's
workload attestation (the other two "Tredje bølge" items), and
explicitly recognizes `unknown_stage_holds` as a different axis --
receipt-stage compatibility, already owned by v0.1's
`STAGE_PREDECESSORS`/`ReceiptTrail` -- rather than misapplying version
logic to it.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `schema-version-compatibility-gate-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> 7. Nåværende modningsrekkefølge -> Tredje bølge; 1. Human text -> SUBJECT / CHANNEL REGISTRY; 2. X4 runtime-fagpass -> First-wave normative contract draft -> Registry; 4. Subject Registry v0.1 draft JSON -> entries[*].compatibility |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v15_validation.py selftest
signalvev_reference_v15_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged (run in the
same tree; this branch only carries v0.1/v0.2/v0.3/v0.15 as candidates):

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```

9 checks total: one golden-path composition test (`SC0`), plus eight
falsifiers (`SC1`-`SC8`):

- **SC0** (golden path): a known version under the exact-version policy
  is `ACCEPTED`.
- **SC1**: an unrecognized version is `TYPED_HOLD` regardless of policy
  -- direct proof of X4's "Unknown subject/version/producer is a typed
  HOLD or quarantine, not an improvised fallback."
- **SC2**: a numerically newer-looking unknown version (`"2.0.0-draft"`)
  receives exactly the same `TYPED_HOLD` as any other unknown string --
  proof of `work.command`'s own "no implicit upgrade" wording; no
  version-ordering heuristic sneaks in.
- **SC3**: `version_negotiated_before_reply` holds even with a known
  version if no negotiation flag is set -- a version match alone is not
  sufficient for `status.request`/`status.response`.
- **SC4**: the same policy accepts once negotiation is confirmed.
- **SC5**: `content_type_and_schema_version_checked` holds when content
  type is undeclared, even with a matching version -- `artifact.
  pointer`'s own named conjunction, distinct from v0.13's separate
  bytes-and-custody guard.
- **SC6**: `unknown_stage_holds` returns `NOT_APPLICABLE`, not a
  version-flavored verdict -- proof this gate recognizes receipt-stage
  compatibility as a different axis it does not adjudicate.
- **SC7**: cross-version consistency canary -- every one of the seven
  real entries in v0.1's actual, already-merged Subject Registry file
  (read via `load_registry()`, never modified) declares a
  `compatibility` string this gate recognizes without raising.
- **SC8**: an unrecognized compatibility policy string (a typo, or a
  future policy) fails closed with an explicit
  `SchemaCompatibilityError`, never silently treated as any existing
  policy.

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
transport, registry publication tooling, or Drive at all -- it proves
an offline version-compatibility classification property over
caller-supplied strings and flags plus one read-only registry-file
canary, a narrower claim than "this works against a real
schema-negotiation handshake between two live endpoints."

## Open deviations and assumptions

1. **This candidate does not implement version negotiation itself.**
   `negotiated` is a plain caller-supplied boolean; how a real
   negotiation handshake would set it is out of scope, matching every
   other candidate's treatment of inputs it consumes rather than
   produces.
2. **`known_versions` is caller-supplied, not derived from the
   registry.** The registry file names a `schema_ref`, not an explicit
   set of currently-known versions per subject; a future candidate
   could extend the registry schema itself to carry that set instead of
   requiring the caller to supply it.
3. **`unknown_stage_holds` is recognized by name only, not
   implemented.** This gate correctly declines to adjudicate it
   (`NOT_APPLICABLE`) rather than reimplementing v0.1's own
   `STAGE_PREDECESSORS`/`ReceiptTrail`, which already owns receipt-stage
   compatibility.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not an implementation of a real version-negotiation handshake.
- Not an authority, claim, or effect grant -- `ACCEPTED` is a
  version/negotiation/content-type compatibility fact only.
- Not a replacement for v0.1's `validate_envelope`, `STAGE_PREDECESSORS`,
  or `ReceiptTrail`.
- Not a modification of v0.1's actual registry file.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.14`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
