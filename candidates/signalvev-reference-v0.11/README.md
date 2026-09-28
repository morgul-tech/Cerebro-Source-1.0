# Signalvev Reference Implementation Candidate v0.11 (effect quarantine classifier)

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

The Human-supplied text's own QUARANTINE + IDEMPOTENCY section states:
"Ukjent effekt skal ikke replayes automatisk. Hvis systemet vet: 'for
effekt' -> NO_EFFECT kan vaere sikkert. 'effektgrensen kan ha blitt
krysset' -> UNKNOWN + quarantine." X4's own first-wave normative
contract draft restates the same rule for Receipts: "NO_RECEIPT
remains UNKNOWN unless an independent pre-effect proof establishes
NO_EFFECT." Falsifier 5, verbatim: "A timeout after possible effect is
labeled NO_EFFECT or triggers replay."

**Correction, made before any delivery of this candidate:** an earlier
draft of this README claimed v0.1's
`test_falsifier_05_timeout_after_effect_not_no_effect` was "a single
inline closure" that does not require independent proof before a
`NO_EFFECT` label. On closer reading of `ReceiptTrail.emit()` in
`signalvev_reference_v01_validation.py`, that is wrong: the core rule
is already enforced there as real, reusable code --
`stage == "NO_EFFECT" and not no_effect_proof` raises `ReceiptError`
-- and that method is imported and exercised by every later candidate
that uses `ReceiptTrail`. Falsifier 5 was **not** a thin,
single-hardcoded-closure gap the way falsifiers 6, 9, and 11 genuinely
were. What v0.1's rule does not do, and what this candidate actually
contributes: model `UNKNOWN_QUARANTINED` as a first-class, inspectable
classification in its own right; reject an unverified caller *belief*
that the boundary was not crossed as distinct from actual proof;
detect the logical contradiction of "proof exists" plus "boundary
possibly crossed"; and expose an advisory replay-safety signal usable
independently of holding a live `ReceiptTrail` instance (for example,
for pre-admission planning before any attempt object exists). This is
a genuine but more modest extension of existing reusable enforcement,
not a from-scratch closure of an unaddressed falsifier gap. The tests
and code below are unchanged and remain accurate; only this framing
was corrected.

## Base commit

```
01a24d3 (main, after PR #5 + PR #8 + PR #9 + PR #7)
```

## Branch

`claude/signalvev-reference-v0.11`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.11/effect-quarantine-classifier-candidate.yaml
A  candidates/signalvev-reference-v0.11/component.yaml
A  candidates/signalvev-reference-v0.11/README.md   (this file)
A  tooling/validator/signalvev_reference_v11_validation.py
```

Nothing in any prior candidate directory is modified. This validator
has **no dependency on any other signalvev-reference candidate** -- it
is a pure, stateless classifier over two caller-supplied booleans
(`has_independent_pre_effect_proof`, `effect_boundary_possibly_crossed`).

## Why this is not a duplicate of v0.7, v0.8, or v0.10

`signalvev-reference-v0.7`'s `IdempotencyScope` answers whether two
attempts are "the same" attempt. `signalvev-reference-v0.8`'s
`RevisionGuardedAdmitter` answers whether a caller's snapshot revision
is still current for work admission. `signalvev-reference-v0.10`'s
reply-currentness guard answers whether a REQUEST/REPLY timeout makes a
*read-only* retry safe. This candidate answers a fourth, independent
question: after a timeout on a potentially-effectful `DURABLE_COMMAND`,
may the outcome be labeled `NO_EFFECT` at all, or must it quarantine as
`UNKNOWN` -- a strictly narrower and distinct decision from all three.
None of the four import or duplicate each other.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `effect-quarantine-classifier-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 2. X4 runtime-fagpass -> Falsifiers (item 5); 2. X4 runtime-fagpass -> First-wave normative contract draft -> Receipts; 1. Human text -> QUARANTINE + IDEMPOTENCY |

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v11_validation.py selftest
signalvev_reference_v11_validation selftest: 9/9 PASS
$ echo $?
0
```

Regression checks -- all prior suites still pass unchanged:

```
signalvev_reference_v01_validation selftest: 18/18 PASS
signalvev_reference_v02_validation selftest: 12/12 PASS
signalvev_reference_v03_validation selftest: 11/11 PASS
```
(v0.4 through v0.10 are separate branches, not on this one; each independently regression-clean per its own README.)

9 checks total: one golden-path composition test (`EQ0`), plus eight
falsifiers (`EQ1`-`EQ8`):

- **EQ0** (golden path): independent pre-effect proof classifies as
  `NO_EFFECT`, not quarantined, replay advisably safe.
- **EQ1**: a timeout without independent proof is never labeled
  `NO_EFFECT` -- the core falsifier 5 scenario.
- **EQ2**: the same unproven timeout never allows
  `replay_may_be_safe = true`.
- **EQ3**: an unverified caller *belief* that the boundary was not
  crossed is not a substitute for independent proof -- still
  quarantined.
- **EQ4**: every quarantined outcome, regardless of the caller's belief
  about the boundary, never pairs with `replay_may_be_safe = true`.
- **EQ5**: independent proof together with "boundary possibly crossed"
  is a logical contradiction and raises rather than silently resolving.
- **EQ6**: the classifier is a pure function -- a `NO_EFFECT` result
  from one call never leaks into an unrelated later call.
- **EQ7**: the output carries only the three documented advisory
  fields -- no field that could be mistaken for an execution-authority
  grant.
- **EQ8**: non-boolean inputs raise rather than being silently coerced.

All tests run fully offline: no socket, subprocess, filesystem write, or
network call. This candidate is not an authorization to execute a
replay, and does not itself generate or verify pre-effect proof.

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
contains v0.1/v0.2/v0.3/v0.11 as candidates -- v0.4 through v0.10 are
separate un-merged branches -- so `DERIVED=23` here, not the higher
count a fully-merged state would eventually show.)

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from prior candidates: S0/S1 remain prior, independent,
behavior-real transport evidence. This candidate does not touch
transport, Drive, or the Coordination Ledger -- it proves an offline
classification property over a timeout situation a caller already holds
evidence about, a narrower claim than "this works against a real live
provider."

## Open deviations and assumptions

1. **This candidate does not generate or verify independent pre-effect
   proof.** It assumes a caller already has (or does not have) such
   proof and classifies from that. What counts as valid independent
   proof for a given effect domain is out of scope here, same as every
   other candidate in this project.
2. **`replay_may_be_safe` is advisory, not enforcement.** This candidate
   does not itself execute or authorize a replay -- it only classifies
   whether one would be advisable if attempted, mirroring
   `signalvev-reference-v0.10`'s `safe_to_retry_as_read_only` pattern.
3. **The contradiction check (EQ5) is a modeling choice, not a claim
   about all possible real systems.** A future candidate could instead
   treat a contradictory input as an automatic `UNKNOWN_QUARANTINED`
   fail-closed outcome rather than raising; this candidate chose to
   surface the contradiction loudly instead, since silently resolving
   it either way risks masking an upstream caller bug.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not an authorization to execute a replay.
- Not a canonical-state override -- state-owner remains the truth
  source per the Human text's own grunnlov-kandidat.
- Not a modification of any prior candidate directory
  (`signalvev-reference-v0.1` through `-v0.10`).
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated.
