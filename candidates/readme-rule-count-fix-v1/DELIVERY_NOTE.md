# README rule-count fix (421 -> 422)

**STATUS: small, isolated documentation fix. No schema, engine, standard, or
tooling logic touched.**

## Base commit

```
8c503cda639838d44f29dd6e555fed7dc6e0e2ee
```
Verified against live `refs/heads/main` on `morgul-tech/cerebro-source-1.0`
via `git ls-remote --heads origin` at the time this branch was prepared
(2026-09-28) -- no drift between this local base and the actual current
`main`.

## Branch

`claude/readme-rule-count-fix-v1`

## Changed files

```
M  README.md   (single line: "Regler: 421" -> "Regler: 422")
```

## What was verified, and how

`tooling/validator/canonical_foundation.py validate --source-root .`
reports (on unmodified `main`):

```json
"errors": ["README_METADATA_COUNT_DRIFT:rules:DECLARED=421:DERIVED=422"]
```

Rather than trusting either number, the 422 figure was independently
reproduced by hand: loading each of the 24 YAML files the validator itself
lists under `inputs.rule_paths` (5 under `engines/*/rules.yaml`, 5 under
`mcp/*.yaml`, 1 under `modules/terminology/rules.yaml`, 13 under
`standards/*.yaml`) and summing `len(rules)` per file independently of the
validator's own code path:

```
  37  engines/context/rules.yaml
  89  engines/interaction/rules.yaml
  49  engines/presentation/rules.yaml
  31  engines/project/rules.yaml
  15  engines/quality/rules.yaml
  10  mcp/activation.yaml
   6  mcp/architecture.yaml
   6  mcp/authority.yaml
   4  mcp/identity.yaml
   5  mcp/priorities.yaml
   9  modules/terminology/rules.yaml
  13  standards/change-architecture.yaml
  31  standards/change-delivery.yaml
  11  standards/mcp.yaml
  12  standards/operational-evidence.yaml
  16  standards/operational-progress.yaml
  19  standards/patch-package.yaml
   7  standards/principalstambok.yaml
   8  standards/project-status.yaml
   4  standards/publication-gate.yaml
  15  standards/quality-gate.yaml
   4  standards/release-platform.yaml
  14  standards/source-authority.yaml
   7  standards/valider-command.yaml
TOTAL: 422
```

This independently confirms 422 is the actual current count and README's
"421" is stale. The fix direction is therefore to correct README.md, not
the validator.

## Offline test result after the fix

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/canonical_foundation.py validate --source-root .
$ echo $?
0
```
Full result: `"result": "PASS"`, `"errors": []`. This was the only failing
check in `canonical_foundation.py` on unmodified `main` -- fixing it
brings the whole validator to a clean PASS with no other changes.

## Open deviations and assumptions

1. **Root cause of the drift (which commit introduced the 422nd rule
   without updating README) was not investigated.** This clone is shallow
   (depth 1), so `git log`/`git blame` history is not available here to
   trace it. The fix corrects the currently-observable drift; it does not
   explain how it arose.
2. **No claim that 422 is a "correct" number in any deeper sense** --
   only that it is what the 24 declared rule files currently, actually
   contain, matching what the repo's own validator already asserts.
3. This PR intentionally does not touch anything in
   `candidates/signalvev-reference-v0.1/` (PR #4) and has no dependency on
   it or vice versa.
