# bk05_capsule — same-arc continuation capsule (offline, standard library, Python 3.12+)

Local, **structural-only** candidate tool for internal review. It builds, validates and measures a compact capsule for a
same-arc verifier REFINE returned to the current writer. Authority is always `NONE`; the result is `LOCAL_CANDIDATE_ONLY`.
It does not send, bind, schedule, start, approve, deploy, read Drive/PM/Source/NATS/providers, read a clock or keep state.
The exact field contract is in `CONTRACT.md`.

## Commands (run from the archive root; `-B` avoids writing `__pycache__`)

```
python3 -B -m bk05_capsule synthetic-fixture --out-dir fx          # self-created SYNTHETIC inputs (exit 0)

python3 -B -m bk05_capsule build \
  --parent fx/parent.txt --parent-manifest fx/parent_manifest.json \
  --verifier fx/verifier.txt --verifier-delta fx/verifier_delta.json --currentness fx/currentness.json \
  [--full-continuation fx/full_continuation.txt] [--prior-record prior_record.json] \
  [--reuse-assumption REREAD_ALL|PARENT_ALREADY_HELD|BOTH_ALREADY_HELD] [--out-dir out]

python3 -B -m bk05_capsule reconstruct --capsule out/capsule.json --parent fx/parent.txt \
  --verifier fx/verifier.txt --out-dir rec

python3 -B -m unittest tests.test_bk05_capsule -v                 # 58 tests
```

`build` always prints the decision JSON to stdout. With `--out-dir` it writes `decision.json` and, **only for
LOCAL_CAPSULE_CANDIDATE**, `capsule.json` and `measurement.json` (created exclusively; an existing file is never
overwritten, exit 2). `reconstruct` verifies the capsule's payload hash and the exact parent/verifier bytes, then writes the
identical `parent.txt`, `verifier.txt` and `reconstruction.json` (typed capsule fields) to `--out-dir`.

Exit codes: 0 candidate / reconstructed · 10 FULL_TASK_REQUIRED · 11 HOLD_STALE · 12 INVALID_INPUT · 2 usage or refused
output · 3 unexpected tool failure.

## What LOCAL_CAPSULE_CANDIDATE means
Only that the typed fields are mutually consistent: hashes match the bytes, repair paths are a subset of the allowed paths,
the six scope/authority fields and return target are identical to the parent's, the typed review fields assert
OWNER_REVIEWED / VERIFIER_REVIEWED with non-empty receipt refs, and the caller-supplied currentness strings equal the manifest's.
It is **not** semantic completeness, not an authenticated owner/verifier approval, not live currentness, and not permission
to send. The owner must still compare the manifest with the full parent/verifier prose, authenticate the receipts and
currentness, decide and send, and verify recipient consumption.

## Choices to review (this candidate's, not the work order's)
* `required_test_delta`, `evidence_refs` must be non-empty (a capsule without a test delta or proof refs would silently
  carry less than a repair needs). Say so if the owner wants empty lists allowed.
* `observed_at` must be ISO-8601 with an explicit UTC offset; the tool never reads a clock, so it cannot call anything old.
* Precedence when several outcomes apply: INVALID_INPUT > HOLD_STALE > FULL_TASK_REQUIRED (a stale basis is refreshed before
  even the full task is relied on). All applicable reasons are listed.
* The capsule additionally carries the currentness basis (labelled caller-supplied) so the recipient sees what was compared.
* List order is preserved; lists must not contain duplicates.

## Limitations
* The manifest is typed, owner-supplied data. If the owner drops *one* of several stop edges or invariants, the tool cannot
  know (only an empty/absent list is detected; see test `test_removing_one_of_several_stop_edges_…`). The capsule carries exactly
  the manifest's lists, so a recipient or reviewer can compare them to the full prose.
* Receipts and currentness are never authenticated. Matching strings are not proof; the labels say so in every output.
* Paths are checked as strings only; nothing is resolved on any filesystem (symlinks etc. are out of scope).
* Size results are bytes/characters of the supplied files under stated reuse assumptions; tokens, spend, latency, Human
  effort and net saving stay `UNKNOWN`.
