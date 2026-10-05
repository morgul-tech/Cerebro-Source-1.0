# RETURN — BK05 same-arc continuation capsule (candidate v2)

STATUS=OFFLINE_CANDIDATE_DELIVERED. The candidate passes its own written acceptance and falsifier cases. This is **not** evidence
about any real episode, and it authorizes no integration or sending.

## Exact supported boundary
Offline, standard-library, Python 3.12+ (run on 3.13.15). Five local input files (+ optional baseline and prior record) in,
one typed local decision out: `LOCAL_CAPSULE_CANDIDATE | FULL_TASK_REQUIRED | HOLD_STALE | INVALID_INPUT`, with machine-readable
reasons and exit codes 0/10/11/12 (2 usage, 3 tool failure). Capsule and measurement files are written only to an explicit
`--out-dir`, never overwriting. Authority is always `NONE`, status `LOCAL_CANDIDATE_ONLY`, validation `STRUCTURAL_ONLY`;
review refs and currentness are labelled `CALLER_ASSERTED_UNAUTHENTICATED` / `CALLER_SUPPLIED_NOT_LIVE_PROOF`. Field contract:
`CONTRACT.md`. No Drive, PM, Source, NATS, provider, sender, scheduler, watcher, auto-bind, clock or state.

## Tests (raw commands and exit codes, Python 3.13.15, from the archive root)
| Command | Exit |
|---|---|
| `python3 -B -m unittest tests.test_bk05_capsule` → `Ran 58 tests … OK` (also with `-W error`) | 0 |
| `python3 -B -m bk05_capsule synthetic-fixture --out-dir fx` | 0 |
| `build …` positive synthetic case (decision `LOCAL_CAPSULE_CANDIDATE`; run twice, files byte-identical) | 0 |
| `build …` stale source head → `HOLD_STALE` | 11 |
| `build …` third repair path → `FULL_TASK_REQUIRED` | 10 |
| `build …` effect class `LOCAL_ONLY`→`LIVE` → `FULL_TASK_REQUIRED` | 10 |
| `build …` owner review `UNREVIEWED` → `FULL_TASK_REQUIRED` | 10 |
| `build …` parent bytes changed → `INVALID_INPUT` | 12 |
| `build …` path `../adapter.py` → `INVALID_INPUT` | 12 |
| `build …` stop edges emptied → `INVALID_INPUT` | 12 |
| `build …` duplicate JSON key → `INVALID_INPUT` | 12 |
| `reconstruct …` all bytes present | 0 |
| `reconstruct …` parent missing → `INVALID_INPUT` | 12 |
| `python3 -B -m bk05_capsule` (no command) | 2 |

All acceptance negatives from the work order are covered by automated tests with typed reasons and the nonzero exit codes:
parent hash changed; verifier bytes changed; parent revision stale; source head stale; third repair path; effect class to LIVE
(and each of the other five scope fields); stop edge removed; return target changed; owner/verifier review missing; duplicate
JSON key (also nested); path traversal and ambiguous normalization (absolute, `..`, `.`/empty segments, backslash, `%2e`,
NFD, case variants); same idempotency_key + changed payload + prior_record (`IDEMPOTENCY_COLLISION`); missing parent during
reconstruction; unknown fields at every object level; review/currentness misreported as authenticated (unknown field,
invalid status, labels unchanged by provenance text). Test-quality check: 12 targeted mutants of `core.py` (hash checks,
subset check, scope compare, both staleness checks, review check, unknown-field check, collision check, payload check,
severity order, traversal check) were each killed by the suite.

## Measured synthetic result (self-created fixture; characters/bytes, not tokens)
Fixture sizes mirror the counts quoted in the work order (parent 3,640, continuation 2,876) purely to exercise the path.
The capsule is 1,834 bytes (ASCII, so characters = bytes). Verifier return 1,500.

| Reuse assumption (stated, not verified) | Recipient files read | Combined bytes = characters | vs 2,876 full continuation (1 file) |
|---|---|---|---|
| REREAD_ALL: capsule + parent + verifier | 3 | 6,974 | +4,098 → **NO_OBSERVED_SIZE_ADVANTAGE** |
| PARENT_ALREADY_HELD: capsule + verifier | 2 | 3,334 | +458 → **NO_OBSERVED_SIZE_ADVANTAGE** |
| BOTH_ALREADY_HELD: capsule only | 1 | 1,834 | −1,042 → OBSERVED_SIZE_ADVANTAGE (requires trusting held copies; hash check by the owner) |

Reading: on this synthetic case the capsule is smaller than the full continuation only if the recipient reuses both parent
and verifier text without rereading. If it must reread either one, the combined burden is not lower. Token count, provider
spend, latency, Human effort and net saving are `UNKNOWN`. Nothing here predicts the real episode, where the capsule's fixed
overhead (labels, hashes, the carried invariants/stop edges) may weigh more or less than here.

## Limitations (also in README)
* Structural only. The manifest is owner-supplied typed data: dropping one of several stop edges/invariants is undetectable
  (only empty/absent is caught); receipts and currentness are never authenticated; matching strings are not live proof.
* No semantic reading of parent.txt or verifier.txt; `semantic completeness` is explicitly not asserted.
* Paths are string-checked only (no filesystem resolution). The tool never reads a clock, so it cannot call anything old by age.
* Candidate-specific choices that the owner may reverse: non-empty `required_test_delta` / `evidence_refs`; precedence
  INVALID_INPUT > HOLD_STALE > FULL_TASK_REQUIRED; currentness basis carried in the capsule.
* No synthetic fixture was used to infer a real outcome; no real data was available or used.

## AMBIENT_CONTEXT_USED
NONE as input. The only source was the work-order text itself (fetched from the supplied Drive link). Earlier unrelated work
in this session and the (empty) account memory were not consulted.

## First internal real-episode evaluation step
The owner takes one real same-arc episode, writes `parent_manifest.json` and `verifier_delta.json` by hand from the full
prose, authenticates the receipt refs and currentness outside this tool, and runs `build` with the real `full_continuation.txt`
under the reuse assumption that actually applies to the recipient. Then compare the measurement with the actual reread and
correction cost, and have the separate reviewer try stale head, scope widening and hidden authority against the capsule.

## Handoff / way home
To the internal owner for archive-integrity check (`MANIFEST.json`, archive SHA-256 in the covering message), test run, and the
real-episode evaluation above. Authority/effect: none; this contribution is local, candidate-only, and not Cerebro truth.
