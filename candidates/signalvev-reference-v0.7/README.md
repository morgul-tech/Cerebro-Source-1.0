# Signalvev Reference Implementation Candidate v0.7 (durable command idempotency scope)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

X4 runtime-fagpass specifies, verbatim: "The idempotency scope must be
specified per command as authority owner + effect domain + claim +
attempt + payload fingerprint. Exact duplicate means same identity and
same canonical payload. Same key with changed payload is CONFLICT,
never a cached success. A new message ID does not create a new lawful
attempt." The Subject Registry v0.1 draft names the exact same five-part
scope for `cerebro.v1.work.command`:
`owner_plus_effect_domain_plus_claim_plus_attempt_plus_payload_fingerprint`.

v0.1's falsifier 4 has a passing test, but on inspection it collapses
this entire five-part identity into one opaque `key` string compared
against one `payload_fingerprint` string -- it never constructs or
tests the five named parts separately, so it cannot show whether the
reference logic actually treats all five as load-bearing (as opposed to
silently ignoring four of them), and it never tests the "a new message
ID does not create a new lawful attempt" sentence at all. This
candidate builds and tests the real five-part scope.

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

`claude/signalvev-reference-v0.7`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.7/durable-command-idempotency-scope-candidate.yaml
A  candidates/signalvev-reference-v0.7/component.yaml
A  candidates/signalvev-reference-v0.7/README.md   (this file)
A  tooling/validator/signalvev_reference_v07_validation.py
```

Nothing in any prior candidate directory is modified. The validator
**imports** (does not copy) `validate_envelope`, `ReceiptTrail`,
`ReceiptError`, `_base_msg` from v0.1. It has **no dependency on v0.2,
v0.3, v0.5, or v0.6**.

## Why this is not a duplicate of v0.5's DurableCommandOutbox

`signalvev-reference-v0.5`'s `DurableCommandOutbox` answers a different
question: does a committed command survive signal loss and a restart,
and is redelivery safe -- keyed by a single, already-formed,
opaque `command_identity` string it is simply handed. This candidate
answers a prior question: what makes two `DURABLE_COMMAND` attempts the
"same" attempt in the first place, and what happens when only one part
of that identity changes. It does not import or duplicate
`DurableCommandOutbox`, and v0.5 never decides idempotency-scope
composition.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `durable-command-idempotency-scope-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> QUARANTINE + IDEMPOTENCY; 2. X4 runtime-fagpass -> Envelope (idempotency scope paragraph); 4. Subject Registry v0.1 draft -> cerebro.v1.work.command entry; 2. X4 runtime-fagpass -> Falsifiers (item 4) |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v07_validation.py selftest
signalvev_reference_v07_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
signalvev_reference_v06_validation selftest: 10/10 PASS
```
(v0.4 and v0.5 are separate branches, not on this one; both independently regression-clean per their own READMEs.)

9 checks total: one golden-path composition test (`IS0`), plus eight
falsifiers (`IS1`-`IS8`):

- **IS0** (golden path): two attempts differing only in `claim` are
  admitted independently and each reaches `RELEASED` through v0.1's
  `ReceiptTrail` without interfering with each other.
- **IS1**: an exact duplicate (same five-part identity, same payload)
  is a safe replay (`ADMITTED_DUPLICATE`), never a fresh admission.
- **IS2**: same identity, changed payload, is `CONFLICT` -- never a
  cached success.
- **IS3**-**IS6**: changing `owner`, `effect_domain`, `claim`, or
  `attempt` *alone* (holding the other three constant) each
  independently yields a different identity, admitted as new -- proving
  all four parts are load-bearing, not just decorative.
- **IS7**: two envelopes with an identical five-part identity and
  payload but *different* `message_id` collapse to the same attempt --
  direct proof of X4's "a new message ID does not create a new lawful
  attempt."
- **IS8**: a `CONFLICT` outcome is never paired with a `WORK_CONSUMED`
  receipt emission, and v0.1's own `STAGE_PREDECESSORS` still rejects
  skipping straight to `EFFECT_ACCEPTED` -- this candidate does not
  bypass v0.1's stage machine to make conflicts look safe.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call.

## Mandatory pre-delivery gate

```
$ python3 -c "... yaml.safe_load() every *.yaml in repo ..."
0 failing file(s)

$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
exit code: 1
result: FAIL
errors: ['README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=29']
```

Run against the rebased base (`b1109b8`). No new failures introduced.
The one error is the same pre-existing, PRINCIPAL-scoped
`README_METADATA_COUNT_DRIFT` judgment call already flagged in PR #9
and in every candidate since. The count moved from 25 (against the
original `01a24d3` base) to 29 against this rebased base (after PR
#10-#15 merged v0.8-v0.13, each adding its own `component.yaml`) -- not
a defect in this candidate's own files.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
identity-composition property of one reference primitive, a narrower
claim than "this works on real Signalvev."

## Open deviations and assumptions

1. **`payload_fingerprint` is deliberately not part of `IdempotencyScope`
   itself** -- it is passed separately to `admit()`. This mirrors X4's
   own phrasing ("same identity and same canonical payload" as two
   distinct conditions) and is what lets `ADMITTED_DUPLICATE` and
   `CONFLICT` be distinguished at all.
2. **This candidate does not compute `payload_fingerprint` from actual
   payload bytes** (e.g. via a hash) -- callers pass a fingerprint
   string directly. Wiring this to a real hash function (as v0.2's
   artifact pointer candidate already does for artifact bytes) is a
   reasonable, small follow-up, not done here to keep scope on the
   identity-composition question itself.
3. **`DurableCommandAdmitter` is in-memory only**, like v0.1's own
   `ReceiptTrail` -- it does not claim restart-safety. Combining this
   admitter with v0.5's restart-safe outbox is a reasonable future
   candidate, not done here (see non-duplication note above for why
   they are deliberately kept separate for now).

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a claim/occupancy grant mechanism.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.6`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
