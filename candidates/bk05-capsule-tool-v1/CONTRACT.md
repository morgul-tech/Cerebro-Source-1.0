# BK05 field contract (exact; no hidden defaults)

Every input is a local file. Every JSON input must be UTF-8 (no BOM), a single object, with **no duplicate keys at any
level, no NaN/Infinity, and no field outside the lists below**. Every listed field is required; there is no default for
authority, scope, effect, privacy, return or review. "str" = non-blank string; "str[]" = non-empty list of unique non-blank
strings; "path[]" = non-empty list of unique canonical repo-relative paths (see Paths).

## Inputs

**parent.txt** — exact bytes of the canonical parent task (non-empty, valid UTF-8).
**verifier.txt** — exact bytes of the verifier return (non-empty, valid UTF-8).

**parent_manifest.json** (owner-supplied; the CLI cannot prove it describes parent.txt faithfully)

| Field | Type | Note |
|---|---|---|
| task_ref | str | |
| parent_revision | str | non-empty |
| parent_sha256 | 64 lowercase hex | must equal sha256(parent.txt) |
| actor_ref, effect_class, privacy_class, live_scope, authority_class | str | exact values the verifier request must repeat |
| allowed_paths | path[] | |
| required_invariants | str[] | absent/empty ⇒ `MISSING_REQUIRED_INVARIANT` |
| stop_edges | str[] | absent/empty ⇒ `MISSING_STOP_EDGE` |
| return_target | str | absent/blank ⇒ `MISSING_RETURN_TARGET` |
| way_home | str | absent/blank ⇒ `MISSING_WAY_HOME` |
| source_head | str | compared as an exact string, no hex assumption |
| semantic_review | {status: `OWNER_REVIEWED`\|`UNREVIEWED`, receipt_ref: string, may be empty} | caller-asserted, unauthenticated |

**verifier_delta.json**

| Field | Type | Note |
|---|---|---|
| verifier_ref | str | |
| verifier_sha256 | 64 lowercase hex | must equal sha256(verifier.txt) |
| finding | str | |
| repair_paths | path[] | must be a subset of parent allowed_paths |
| requested_actor_ref, requested_effect_class, requested_privacy_class, requested_live_scope, requested_authority_class, requested_return_target | str | must equal the parent's values exactly |
| required_test_delta | str[] | non-empty (structural choice of this candidate, see README) |
| evidence_refs | str[] | become `inherited_proof_refs` |
| idempotency_key | str | non-empty |
| semantic_review | {status: `VERIFIER_REVIEWED`\|`UNREVIEWED`, receipt_ref: string} | caller-asserted, unauthenticated |

**currentness.json** (caller-supplied evidence, never a live provider read)
`observed_source_head` str, `observed_parent_revision` str, `observed_at` ISO-8601 timestamp **with explicit UTC offset**
(the tool never reads a clock and never judges age), `provenance` str.

**full_continuation.txt** (optional) exact bytes of the baseline continuation, used only for measurement.
**prior_record.json** (optional) exactly `{idempotency_key: str, payload_sha256: 64 lowercase hex}`.
An optional input that is named on the command line but cannot be read is `INVALID_INPUT/ABSENT_INPUT_FILE`, never skipped.

## Paths
A path is valid only if it is already in canonical form: not absolute (`/…`, `~…`, `C:…`), no `..` segment (`PATH_TRAVERSAL`);
no backslash, no empty or `.` segment, no trailing slash, no surrounding whitespace, no control character, no
`%2e/%2f/%5c` escape, NFC-normalized (`PATH_AMBIGUOUS_NORMALIZATION`). Two entries equal after NFKC+casefold in one list, or a
repair path that matches an allowed path only after folding, are also `PATH_AMBIGUOUS_NORMALIZATION`. Subset comparison is exact.

## Decision (stdout JSON, also `decision.json`)
`schema_version bk05-decision-v1`, `decision`, `reasons[]` of `{class, code, field, detail}`, constants `authority:"NONE"`,
`status:"LOCAL_CANDIDATE_ONLY"`, `validation:"STRUCTURAL_ONLY"`, `review_basis:"CALLER_ASSERTED_UNAUTHENTICATED"`,
`currentness_basis:"CALLER_SUPPLIED_NOT_LIVE_PROOF"`, `limits[]`, `idempotency{status[, idempotency_key]}`, `payload_sha256`
(null unless a capsule was built), `measurement` (`PRODUCED`|`NOT_PRODUCED`).

| Decision | Exit | Meaning / reason codes |
|---|---|---|
| LOCAL_CAPSULE_CANDIDATE | 0 | typed structural consistency only (`STRUCTURALLY_CONSISTENT`) |
| FULL_TASK_REQUIRED | 10 | `REPAIR_PATH_NOT_ALLOWED`; `REQUESTED_{ACTOR_REF,EFFECT_CLASS,PRIVACY_CLASS,LIVE_SCOPE,AUTHORITY_CLASS,RETURN_TARGET}_DIFFERS`; `PARENT_REVIEW_MISSING`; `VERIFIER_REVIEW_MISSING` |
| HOLD_STALE | 11 | `STALE_PARENT_REVISION`; `STALE_SOURCE_HEAD` (string inequality against caller-supplied currentness) |
| INVALID_INPUT | 12 | `ABSENT_REFERENCED_BYTES`, `ABSENT_INPUT_FILE`, `EMPTY_INPUT`, `NON_UTF8_INPUT`, `MALFORMED_JSON`, `JSON_NOT_OBJECT`, `DUPLICATE_JSON_KEY`, `UNKNOWN_FIELD`, `MISSING_FIELD`, `WRONG_TYPE`, `INVALID_STRING`, `INVALID_SHA256`, `INVALID_ENUM_VALUE`, `CONSTANT_MISMATCH`, `EMPTY_OR_WRONG_LIST`, `DUPLICATE_LIST_ITEM`, `OBSERVED_AT_UNPARSEABLE`, `PARENT_HASH_MISMATCH`, `VERIFIER_HASH_MISMATCH`, `MISSING_REQUIRED_INVARIANT`, `MISSING_STOP_EDGE`, `MISSING_RETURN_TARGET`, `MISSING_WAY_HOME`, `PATH_TRAVERSAL`, `PATH_AMBIGUOUS_NORMALIZATION`, `IDEMPOTENCY_COLLISION` |

Usage errors and refused output (existing file) exit 2 (`bk05-error-v1` on stderr); unexpected tool failure exits 3.
All reasons that apply are listed; the decision is the most severe class: INVALID_INPUT > HOLD_STALE > FULL_TASK_REQUIRED.
`idempotency.status`: `NO_PRIOR_RECORD_CHECKED` (no prior supplied; no cross-invocation idempotency is claimed),
`PRIOR_RECORD_MATCH`, `PRIOR_RECORD_DIFFERENT_KEY`, `IDEMPOTENCY_COLLISION` (⇒ INVALID_INPUT), `NOT_CHECKED_NO_CAPSULE`.

## capsule.json (`bk05-capsule-v1`; canonical JSON: sorted keys, `,`/`:` separators, UTF-8, one trailing LF)
`schema_version`, `authority:"NONE"`, `status:"LOCAL_CANDIDATE_ONLY"`, `validation:"STRUCTURAL_ONLY"`,
`not_claimed:["BIND","CONSUMPTION","DEPLOYMENT","OWNER_APPROVAL","PRODUCTION","SEND","START"]`,
`parent{task_ref,revision,sha256,bytes}`, `verifier{ref,sha256,bytes}`, `actor_ref`, `effect_class`, `privacy_class`,
`live_scope`, `authority_class`, `source_head`, `allowed_repair_paths`, `finding`, `required_test_delta`,
`inherited_proof_refs`, `required_invariants`, `stop_edges`, `return_target`, `way_home`, `idempotency_key`,
`reviews{parent,verifier}` each `{status, receipt_ref, assertion:"CALLER_ASSERTED_UNAUTHENTICATED"}`,
`currentness_basis{observed_source_head, observed_parent_revision, observed_at, provenance,
assertion:"CALLER_SUPPLIED_NOT_LIVE_PROOF"}`, `payload_sha256`.
`payload_sha256` = SHA-256 of the canonical JSON (without trailing LF) of every field except `payload_sha256`.
It is independent of `prior_record.json`, of `--reuse-assumption` and of file paths.

## measurement.json (`bk05-measurement-v1`)
`sizes` (bytes and characters, separately) for capsule.json as written, parent.txt, verifier.txt and, if supplied,
full_continuation.txt; `scenarios` for three explicit reuse assumptions (`REREAD_ALL`: capsule+parent+verifier read, 3 files;
`PARENT_ALREADY_HELD`: capsule+verifier, 2 files; `BOTH_ALREADY_HELD`: capsule only, 1 file) each with the combined
volume and the parent+verifier volume actually reread; `baseline` (1 file); `comparison` for the selected assumption
(`OBSERVED_SIZE_ADVANTAGE` only if lower in **both** bytes and characters, else `NO_OBSERVED_SIZE_ADVANTAGE`, or
`NOT_MEASURED_BASELINE_ABSENT`); `missing_inputs`; `unknown_until_externally_measured` = `UNKNOWN` for token_count,
provider_spend, latency, human_effort, net_saving. Characters are Unicode code points of the decoded text, never tokens.
A measurement result never changes the decision. The reuse assumptions are stated, not verified.
