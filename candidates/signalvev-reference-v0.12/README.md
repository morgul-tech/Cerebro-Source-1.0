# Signalvev Reference Implementation Candidate v0.12 (presence envelope authority-boundary guard)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

The Human-supplied text's own PRESENCE / SERVICE DIRECTORY section
states: "Et eget system for: ONLINE, OFFLINE, READY, ARMED, DEGRADED,
UNKNOWN. Presence er ikke: authority, claim, work_started, consumed,
effect." Falsifier 6, verbatim: "Presence READY or ARMED supplies
command authority or occupancy." The same negative-rules section adds
`ONLINE != READY`, `READY != AUTHORIZED`, and `PRESENCE != OCCUPANCY`.
v0.1's `test_falsifier_06_presence_not_authority` has a passing check,
but it is a single inline closure with one hardcoded scenario -- it
does not check the envelope's own `authority_ref`/`effect_class`
fields, does not name all four claim types the Human text lists
(claim/occupancy, work_started, consumed, effect), and does not
specifically exercise `READY` and `ARMED` the way the falsifier itself
names them. This candidate builds the real, reusable, structural guard.

## Base commit

```
01a24d3 (main, after PR #5 + PR #8 + PR #9 + PR #7)
```

## Branch

`claude/signalvev-reference-v0.12`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.12/presence-envelope-authority-boundary-guard-candidate.yaml
A  candidates/signalvev-reference-v0.12/component.yaml
A  candidates/signalvev-reference-v0.12/README.md   (this file)
A  tooling/validator/signalvev_reference_v12_validation.py
```

Nothing in any prior candidate directory is modified. This validator
has **no dependency on any other signalvev-reference candidate** -- it
is a pure, stateless structural guard over a caller-supplied presence
message (`presence_state`, `effect_class`, `authority_ref`, and four
claim flags).

## Why this is not a duplicate of v0.7, v0.8, v0.10, or v0.11

`signalvev-reference-v0.7`, `-v0.8`, `-v0.10`, and `-v0.11` all answer
questions about `DURABLE_COMMAND` or `REQUEST`/`RESPONSE` messages --
idempotency, revision currentness, reply currentness, and effect
quarantine, respectively. This candidate answers an independent,
structural question scoped to `PRESENCE` messages only: does this
presence message improperly carry authority-, claim-, or
effect-bearing fields. None of the five import or duplicate each other,
and this candidate makes no admission, currentness, or replay-safety
decision of its own.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `presence-envelope-authority-boundary-guard-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> Falsifiers (item 6); 1. Human text -> PRESENCE / SERVICE DIRECTORY; 1. Human text -> negative regler (ONLINE != READY; READY != AUTHORIZED; PRESENCE != OCCUPANCY) |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v12_validation.py selftest
signalvev_reference_v12_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```
(v0.4 through v0.11 are separate branches, not on this one; each independently regression-clean per its own README.)

9 checks total: one golden-path composition test (`PG0`), plus eight
falsifiers (`PG1`-`PG8`):

- **PG0** (golden path): a clean `READY` presence message with no
  authority/claim fields validates.
- **PG1**: `READY` presence claiming occupancy is rejected -- the core
  falsifier 6 scenario for `READY`.
- **PG2**: `ARMED` presence claiming occupancy is rejected -- the
  falsifier names `ARMED` specifically alongside `READY`, with no
  exemption.
- **PG3**: a non-null `authority_ref` is rejected regardless of
  presence_state.
- **PG4**: a non-`NONE` `effect_class` is rejected on a presence
  message.
- **PG5**: a presence message claiming `work_started` is rejected.
- **PG6**: a presence message claiming `consumed` is rejected.
- **PG7**: a presence message claiming `effect` is rejected.
- **PG8**: an unrecognized `presence_state` raises, and all six of the
  Human text's own clean presence states (`ONLINE`, `OFFLINE`, `READY`,
  `ARMED`, `DEGRADED`, `UNKNOWN`) validate when carrying no
  authority/claim fields.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. This candidate implements no presence/service-directory
transport or scheduler, per the Human text's own "skal aldri brukes som
skjult scheduler" line.

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
contains v0.1/v0.2/v0.3/v0.12 as candidates -- v0.4 through v0.11 are
separate un-merged branches -- so `DERIVED=23` here, not the higher
count a fully-merged state would eventually show.)

**Self-caught fix during this candidate's own build:** an earlier draft
of `presence-envelope-authority-boundary-guard-candidate.yaml` had an
unquoted colon inside a YAML sequence item's free text (a quoted
sub-phrase like `Human text: "..."`), which broke block-mapping
parsing -- the same class of error PR #9 previously fixed elsewhere in
this repo. Caught by this candidate's own mandatory YAML scan before
delivery, not after; fixed by folding the item into a single quoted
block scalar (`>-`) before running any test or gate.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
structural-boundary property over a presence message a caller already
constructed, a narrower claim than "this works against a real live
provider" or "this is a service directory implementation."

## Open deviations and assumptions

1. **This candidate does not implement presence transport itself** --
   no actual ONLINE/OFFLINE detection, no heartbeat, no service
   directory. It only validates a presence message a caller already
   constructed. Wiring it to an actual presence/service-directory
   system is out of scope here, same as every other candidate in this
   project.
2. **The four claim flags (`claims_occupancy`, `claims_work_started`,
   `claims_consumed`, `claims_effect`) are this candidate's own
   modeling of the Human text's "authority, claim, work_started,
   consumed, effect" list**, collapsing "authority" and "claim" into
   the `authority_ref` field check (step 3) since both concern the same
   underlying authority concept in `CEREBRO_MESSAGE_V1`. A future
   candidate could split them further if a live envelope schema
   distinguishes them.
3. **This candidate does not itself decide what a valid `authority_ref`
   looks like on a non-`PRESENCE` message** -- it only asserts that a
   `PRESENCE` message must never carry one at all.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a presence/service-directory transport or scheduler
  implementation.
- Not an occupancy/claim ledger -- this candidate rejects violations,
  it does not track or grant occupancy itself.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.11`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
