# Signalvev Reference Implementation Candidate v0.6 (human notice projection)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass's own crosswalk table names a specific, concrete
material gap in L7 (Human Communication): "No shared rule for
projecting uncertain machine state without translating UNKNOWN into an
apparent Human gate or success." No prior candidate (v0.1-v0.5) touches
L7 at all. v0.1's falsifier 12 ("A Human notice turns machine
HOLD/UNKNOWN into an invented approval request") has a passing test,
but on inspection -- the same pattern found and closed for falsifier 3
by v0.5 -- that test is a single inline closure defined inside the test
function itself. It checks three states for one forbidden substring
pattern; it is not a reusable primitive, does not distinguish a NOTICE
from a GATE, and only covers half of X4's stated gap (fabricated
success -- not the "invented Human gate" half at all). This candidate
builds the real projection rule.

## Base commit

```
b1109b8 (main, after PR #5 + #8 + #9 + #7 + #10-#15 -- rebased)
```

Originally prepared and delivered against `01a24d3`. Rebased here onto
`b1109b8` (after PR #10-#15 merged v0.8-v0.13) via a clean cherry-pick
of the original commit -- file content is unchanged, only the base
moved forward. See "Mandatory pre-delivery gate" below for the
re-verification against the new base.

## Branch

`claude/signalvev-reference-v0.6`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.6/human-notice-projection-candidate.yaml
A  candidates/signalvev-reference-v0.6/component.yaml
A  candidates/signalvev-reference-v0.6/README.md   (this file)
A  tooling/validator/signalvev_reference_v06_validation.py
```

Nothing in any prior candidate directory is modified, and
`signalvev-reference-v0.1/subject-registry-v0.1-candidate.json` is
**not** touched -- this candidate's proposed eighth registry entry
(`cerebro.v1.human.notice`) lives only inside its own
`human-notice-projection-candidate.yaml`. The validator **imports**
(does not copy) `validate_envelope`, `ReceiptTrail`, `_base_msg` from
v0.1. It has **no dependency on v0.2, v0.3, or v0.5**.

## Why this is new scope, not a duplicate of v0.1's falsifier 12

See `why_this_is_new_scope_not_a_duplicate` in
`human-notice-projection-candidate.yaml`. In short: v0.1's own test is
real and stays valid, but it is a narrow inline check, not a candidate.
This delivers the actual `HumanNoticeProjector` primitive X4 asked for,
as its own source-cited, falsifiable, reusable reference implementation
-- the same relationship v0.5 has to v0.1's falsifier 3.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `human-notice-projection-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> L7 HUMAN COMMUNICATION; 1. Human text -> Communication classes v0.1 -> HUMAN_NOTICE; 2. X4 runtime-fagpass -> Current runtime crosswalk (L7 row); 2. X4 runtime-fagpass -> Falsifiers (item 12) |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v06_validation.py selftest
signalvev_reference_v06_validation selftest: 10/10 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```
(v0.4 and v0.5 are separate branches, not on this one; both independently regression-clean per their own READMEs.)

10 checks total: one golden-path composition test (`HN0`), plus nine
falsifiers (`HN1`-`HN9`):

- **HN0** (golden path): a receipt trail that reaches `EFFECT_UNKNOWN`
  (quarantine) projects as an uncertain `NOTICE`, never a fabricated
  success, never an invented gate, never authority.
- **HN1**: every uncertain machine state (`HOLD`, `UNKNOWN`,
  `EFFECT_UNKNOWN`, `NO_RESPONDER`, `TIMEOUT_UNKNOWN`) projects without
  any success/approval-reading substring.
- **HN2**: uncertainty never silently escalates to `GATE` -- the default
  `kind` is always `NOTICE` unless explicitly requested otherwise (the
  "invented Human gate" half of X4's material gap).
- **HN3**: `EFFECT_UNKNOWN` and `EFFECT_ACCEPTED` never produce the same
  label -- a genuinely uncertain outcome is never merged with a
  confirmed one.
- **HN4**: an unrecognized machine state fails closed to `UNCERTAIN`,
  never defaults to `SUCCESS`.
- **HN5**: constructing and schema-validating a `HUMAN_NOTICE` envelope
  never, by itself, advances a v0.1 `ReceiptTrail` -- `MESSAGE !=
  AUTHORITY` holds for this message type specifically, not just in the
  abstract.
- **HN6**: the projection always reports `authority: NONE`, even when
  the underlying envelope happens to carry a non-null
  `authority_class`/`authority_ref` pointing elsewhere in the system.
- **HN7**: the positive-path check -- genuinely confirmed states
  (`EFFECT_ACCEPTED`, `RELEASED`, `NO_EFFECT`) are still allowed to
  project as `CONFIRMED`. The rule is "don't fabricate certainty," not
  "suppress it."
- **HN8**: an explicit `GATE` request is honored (not silently
  downgraded to `NOTICE`), and still reports `authority: NONE` -- a Human
  Gate request is a request for a decision, never a grant of one.
- **HN9**: an invalid `kind` value is rejected, not silently accepted.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. No Tower/HMI/Postkasse surface is implemented or wired --
this is only the projection rule such a surface would call.

## Mandatory pre-delivery gate

Full-repo YAML scan and `canonical_foundation.py validate --source-root .`
were run against the rebased base (`b1109b8`) before considering this
candidate ready:

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
flagged in PR #9 and in v0.4/v0.5's READMEs. The count moved from 24
(against the original `01a24d3` base) to 29 against this rebased base
(`b1109b8`, after PR #10-#15 merged v0.8-v0.13, each adding its own
`component.yaml`) -- not a defect in this candidate's own files.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from v0.1-v0.5: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
projection-safety property of one reference primitive, a narrower claim
than "this works on a real Tower/HMI surface."

## Open deviations and assumptions

1. **This is the first L7 primitive in the project.** No prior
   candidate establishes conventions for how L7 primitives should be
   structured, so this candidate's shape (a stateless `project()`
   function returning a dict) is a first proposal, not an established
   pattern -- a future Tower/HMI candidate may reveal it needs to
   change.
2. **`MACHINE_STATE_CLASS` is a fixed, hand-authored table**, not
   derived from v0.1's `STAGE_PREDECESSORS` or any other existing
   registry. It deliberately overlaps only partially with v0.1's receipt
   stages (it also classifies `NO_RESPONDER`/`TIMEOUT_UNKNOWN`, which
   are v0.2 request/reply outcomes, not receipt stages) because L7
   projection needs to cover uncertainty from multiple primitives, not
   just receipts. Unifying this with a single canonical "uncertainty"
   enum across all candidates is a reasonable future step, not done
   here.
3. **The proposed eighth subject-registry entry
   (`cerebro.v1.human.notice`) is this candidate's own proposal**, not
   yet reconciled with `signalvev-reference-v0.1`'s actual registry
   file. Whether to fold it into v0.1's file is a PRINCIPAL-scoped
   decision, left open.
4. **GATE handling is intentionally minimal.** This candidate proves
   that a GATE request is never invented and never silently downgraded,
   but does not implement any actual Human decision/approval workflow
   (queueing, timeout, escalation) -- that remains L7 UI/workflow
   territory, out of scope for an architecture-only projection rule.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a Tower, HMI, or Postkasse implementation.
- Not a Human Gate approval/decision mechanism.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.5`), including v0.1's actual
  subject registry file.
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
