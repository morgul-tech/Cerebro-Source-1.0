# Fix: invalid YAML in request-reply-candidate.yaml (PR #8 regression)

**STATUS: BUGFIX. AUTHORITY: NONE.** Isolated, single-line-scope
correction to a defect introduced by
`candidates/signalvev-reference-v0.2/request-reply-candidate.yaml` (PR
#8, merged into `main` at `4e756e7`).

## The bug

`tooling/validator/canonical_foundation.py` recursively parses every
`.yaml` file in the repository (`root.rglob('*.yaml')`) as part of its
`validate --source-root .` check. As delivered in PR #8, list item 2 of
`request-reply-candidate.yaml`'s `explicitly_not:` block was:

```yaml
  - Not authority: a RESPONSE is "current state owner at response revision"
    only, never canonical state itself (subject registry
    cerebro.v1.status.response, ack_semantics
    RESPONSE_NE_CANONICAL_STATE;REQUESTER_REREAD_IF_EFFECTFUL).
```

The colon after "Not authority" makes YAML parse this as an implicit
single-key mapping (`{"Not authority": "a RESPONSE is ..."}`) rather than
a plain string. Combined with the multi-line continuation, this is
invalid YAML and crashes `yaml.safe_load()`:

```
yaml.scanner.ScannerError: while scanning a simple key
  in "<unicode string>", line 65, column 5
could not find expected ':'
  in "<unicode string>", line 66, column 5
```

This was not caught by `signalvev_reference_v02_validation.py`'s own
selftest (12/12 PASS in PR #8) because that suite never calls
`yaml.safe_load()` on this file -- it only reads
`subject-registry-v0.1-candidate.json` (JSON, unaffected) for its
cross-version checks. It went undetected until `canonical_foundation.py`
was run against the merged `main` after PR #8 landed.

## The fix

One file, four lines changed (whitespace/structure only, **zero content
change**): the list item is now an explicit folded block scalar (`>-`),
which is unambiguous regardless of internal colons.

```
A candidates/signalvev-reference-v0.2/request-reply-candidate.yaml
```

```diff
-  - Not authority: a RESPONSE is "current state owner at response revision"
+  - >-
+    Not authority: a RESPONSE is "current state owner at response revision"
     only, never canonical state itself (subject registry
     cerebro.v1.status.response, ack_semantics
     RESPONSE_NE_CANONICAL_STATE;REQUESTER_REREAD_IF_EFFECTFUL).
```

Verified the parsed string value is byte-identical before/after (only
the YAML *encoding* of the scalar changed, not its content):

```python
>>> data['explicitly_not'][1]
'Not authority: a RESPONSE is "current state owner at response revision" only, never canonical state itself (subject registry cerebro.v1.status.response, ack_semantics RESPONSE_NE_CANONICAL_STATE;REQUESTER_REREAD_IF_EFFECTFUL).'
```

## Base commit

```
4e756e7 (main, after PR #5 + PR #8 merged)
```

## Verification performed

```
$ python3 -c "import yaml, pathlib
for p in sorted(pathlib.Path('.').rglob('*.yaml')):
    yaml.safe_load(p.read_text(encoding='utf-8-sig'))"
0 failing yaml files   (every .yaml file in the repo now parses)

$ python3 tooling/validator/signalvev_reference_v02_validation.py selftest
signalvev_reference_v02_validation selftest: 12/12 PASS   (regression check)

$ python3 tooling/validator/signalvev_reference_v01_validation.py selftest
signalvev_reference_v01_validation selftest: 18/18 PASS   (regression check)
```

## A second, separate issue this fix surfaces (NOT fixed here)

With the crash resolved, `canonical_foundation.py validate --source-root
.` now completes and reports one remaining finding, previously invisible
because the crash prevented the script from ever reaching this check:

```
README_METADATA_COUNT_DRIFT:components:DECLARED=19:DERIVED=21
```

Root `README.md` states "Komponenter: 19". The validator now derives 21
-- because `candidates/signalvev-reference-v0.1/component.yaml` and
`candidates/signalvev-reference-v0.2/component.yaml` each follow the
repo's `schema: cerebro-component/v1` convention (matching how every
other component in the repo is counted), and the validator counts every
`component.yaml` file in the tree (`root.rglob('component.yaml')`).

This drift has existed since PR #4 merged (adding the first
`component.yaml`) and PR #8 (adding the second) -- it was simply never
visible because no one ran `canonical_foundation.py` against `main`
after either merge until this fix uncovered the earlier crash.

**This PR intentionally does not fix the count.** Whether "19" should
become "21", whether candidate `component.yaml` files should count
toward the authoritative Source component count at all, or whether they
need a different `status`/`type` marker to be excluded from that count,
is a judgment call about what "Komponenter" is supposed to mean --
outside the scope of a YAML-syntax bugfix. Flagged here for PRINCIPAL to
decide, following the same separate-small-PR pattern already used for
the README rule-count fix (PR #5).

## Explicitly not claimed

- Not a content change to any candidate's architecture, schema, or logic.
- Not a fix to the newly surfaced README component-count drift.
- Not a change to `candidates/signalvev-reference-v0.1/` or
  `candidates/signalvev-reference-v0.3/` (verified clean already).
- Not production-readiness or authority of any kind.
