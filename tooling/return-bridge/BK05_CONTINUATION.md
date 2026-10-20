# Complete BK05 recipient continuation v1

Selected build: P23-BK05-RECIPIENT-CONTINUATION-20261020-01/rev2. This adapter surrounds the unchanged BK05 core; it is separate from the existing FACT_RETURN observation schema and backward-compatible `rom_e_material_return.finalize(..., held_basis_reader=...)`. It does not fabricate typed facts, adapt655 into1B, prove another model's memory, send, grant access or execute work. Python>=3.12; use the existing qualified3.13 and complete owner-selected Source package, without installation/dependencies/services. Private task packets stay in the existing legitimate task home, outside public Source/Work.

## Ordinary calls and three voices

```text
QUALIFIED_PYTHON -I -B ABS/bk05_continuation.py prepare --request ABS/request.json --out-root ABS/existing-task-home
QUALIFIED_PYTHON -I -B ABS/bk05_continuation.py read --manifest ABS/packet-KEY/manifest.json --expect-sha EXACT_SHA256 --mode short|full --current-observation ABS/current-observation.json
```

The public Python functions have the corresponding keyword arguments. `read` optionally accepts `--part INDEX`/`part=INDEX` for an exact bounded UTF-8 part of a large selected rendering. No shell-string builder, provider callback loader, clock/model, credential, sender or effect process is invoked. The ordinary sender carries the returned `envelope` unchanged through its already legitimate transport: exact absolute same-host manifest locator/hash, task/revision/continuation and distinct issuer/reviewer/recipient bindings, return target and Way Home. A Cyborg local path is not a remote transport locator. A local sender double is not actual delivery.

First selected route is issuer A1→recipient same1B on Cyborg, with unchanged attributed655 review. A1 chooses its own operative decision/delta; reviewer disagreement is preserved, not rewritten into a core PASS. No new business attempt is created by reading full instead of short. An appropriate genuine review finding may be chosen by A1 after qualification; no fabricated REFINE/retrocapture/FACT_RETURN episode.

## Input fields (fixed, no hidden defaults)

All JSON is one UTF-8 object with no BOM, duplicate fields, NaN/Infinity or extra fields. All listed fields are required. Nonempty text is UTF-8 without lone surrogates. A role binding has exactly `actor_ref`, `generation_ref`, `carrier_ref`, each nonempty text; issuer/reviewer/recipient actor references must differ. These are caller-qualified references, not authenticated actors or grants.

`request.json`:

| Field | Exact meaning |
|---|---|
| schema | `bk05-recipient-continuation/v1` |
| task_ref, task_revision, continuation_ref | Legitimate current task and its actual continuation reference. Key binds these plus kind and all three full role bindings. Same key/different producer bytes conflicts; no changing key merely to evade conflict. |
| kind | REPAIR or REFINE |
| issuer, reviewer, recipient | Separate role bindings above |
| review_ref | Actual attributed review reference; when core reviewer_delta is supplied its verifier_ref must match |
| purpose, return_target, way_home | Nonempty actual owner purpose and existing return/home route |
| code_basis | Exactly `{source_commit: <40lowercasehex>, core_sha256: <actual qualified core bytes SHA256>}`; reproducible selected code, not a global HEAD lease |
| inputs | Exactly the eight named roles below, each an absolute local non-reparse filename, or null only for the three optional code-profile roles |

Input roles: `original` (full current task original), `review` (unaltered attributed review), `owner_decision`, `owner_delta`, `current` (frozen original owner-current provenance); `parent_manifest`, `reviewer_delta`, `producer_currentness` are the three nullable core-profile inputs. No ambient same-directory discovery or other actor/private-home search. Opaque original/review/current text is not machine-authenticated semantic truth. A legitimate owner checks completeness in its ordinary disposition, with one changed-risk review.

`owner_decision` has exactly:

```text
schema = bk05-owner-decision/v1
task_ref, task_revision, continuation_ref
issuer, reviewer, recipient                 # exact request bindings
original_sha256, review_sha256              # exact corresponding bytes
decisions = [
  {finding_ref, disposition, reviewer_position, owner_position, dissent}, ...
]
```

Every decision field is nonempty text; unique finding_ref; disposition ACCEPTED/PARTIAL/REJECTED. State actual absence of dissent honestly rather than omit the field. The reviewer and owner positions remain separately attributed. The generator cannot prove these summaries cover every material finding or original constraint.

`owner_delta` has exactly:

```text
schema = bk05-owner-delta/v1
task_ref, task_revision, continuation_ref
owner_decision_sha256                       # exact decision artifact
action                                     # current operative owner action, nonempty text
scope = {effect_class, privacy_class, live_scope, authority_class, allowed_paths}
critical_constraints = [nonempty text, ...]
done_criteria = [nonempty text, ...]
```

Four scope labels are nonempty. allowed_paths is a unique list of canonical repo-relative paths under the core's existing path rule; it may be empty for a genuine non-code full continuation, never filled with fake repo/test data. No label creates permission. Short and full renderings contain this SAME complete exact owner decision/delta, identity, purpose, critical constraints, finish, return/home and profile limits. Short adds exact packet-local original/review/current/profile references/hashes. Full adds the exact original+review and provided profile inputs. The core's optional measurement baseline is not used as this binding.

## Core fit, stable producer packet and atomic publication

No profile inputs selects a complete full continuation (`CODE_PROFILE_NOT_SELECTED`). Partially supplied code basis is structurally checked where provided, then names incomplete profile basis and selects complete full continuation. Any supplied parent/currentness pair is compared BEFORE that incomplete/full choice: contradictory observed_parent_revision or observed_source_head refuses with PRODUCER_PARENT_REVISION_STALE or PRODUCER_SOURCE_HEAD_STALE, even when reviewer_delta is absent. Missing basis alone is not stale, and unrelated global HEAD is not this pair check. Supplied malformed/current-stale/hash-conflicting inputs refuse rather than becoming a sendable stale reserve. Complete genuine code inputs call unchanged `core.evaluate` once in production preparation. Actor/scope/path/review nonfit selects the legitimate complete full route; scope/mandate/access changes still need their real owner decision before effect. Reviewer_delta is never rewritten to fit A1's chosen action. Core/finalize contracts remain intact.

The core's producer `observed_at` and provenance are exact frozen inputs. `read` NEVER calls evaluate or regenerates a capsule/currentness timestamp: it verifies the stored capsule with unchanged `core.reconstruct`. Later recipient observation is outside the packet. Reading full→short→full or observing a later time/global unrelated HEAD preserves the same manifest hash/packet/key. A new material owner decision needs legitimate new continuation/version; local read mode is not a new production identity. Reader format v1 and stored-render reconstruction are explicitly checked; capsule reconstruction additionally needs the qualified current core bytes to match producer code. Reader/producer code pins are reported separately. Upgrading a reader does not edit old producer artifacts.

Under an already existing local output root, exclusive `.bk05-stage-KEY` carries an exact ownership/input-fingerprint marker. The marker remains immutable packet evidence, avoiding an unlink-before-rename crash gap. Only a matching own stage with known fixed members can be cleaned/rebuilt in the same attempt. Unknown marker/content or an incomplete published target is retained for precise owner reconciliation, never overwritten or silently assigned a fresh business key. Known local recovery is not a new Human approval gate; the rightful local owner reconciles actual files/effects in the same attempt.

Preparation snapshots exact request and inputs, verifies full packet, capsule/renderings and readback INSIDE staging, rechecks external input bytes, then atomically renames the complete directory on the same filesystem to `packet-KEY`, without os.replace. Existing target is never intentionally replaced. The ready manifest and every member are hashbound. This is ordinary local filesystem consistency under legitimate root custody, not authenticated malicious-race protection, crash/parent-directory power-loss durability or global locking. Packet immutability is checked by future reads, not enforced by chmod/ACL.

`manifest.json` has exactly schema, version(1), key, request_fingerprint, producer_code(adapter/core SHA256), files(relative fixed member→`{bytes,sha256}`), authority(NONE), status(COMPLETE_IMMUTABLE_PACKET). Fixed members include request, owner marker, profile, short/full, original/review/current/decision/delta and provided profile inputs; capsule only when short structurally fits. No arbitrary relative locator is accepted. All hand-action-bearing bytes and both deliverables are covered. `prepare` returns PREPARED_COMPLETE_PACKET or EXACT_REUSE plus manifest/hash/envelope; NOT_SENT/NOT_OBSERVED remain explicit. Same input/identity with intact prior packet reuses without regeneration; changed inputs/code pin on the same production identity conflicts.

## Separate current observation and bounded recipient read

The actual recipient derives `current-observation.json` in its ordinary current-reading round, preserving the actual source, timestamp and limitations, without a new model/provider/status service. Exact fields:

```text
schema = bk05-recipient-current-observation/v1
task_ref, task_revision, continuation_ref, recipient
owner_decision_sha256, original_sha256
source_current = {path: <actual absolute source>, sha256: <actual full source bytes>}
selected_source_commit
observed_global_source_head
observed_at                               # ISO8601 with explicit UTC offset
provenance                                # nonempty caller source/limits explanation
relevant_change                           # NONE|UNRELATED_GLOBAL_HEAD|RELEVANT_CODE|TASK_DECISION|SCOPE_OR_ACCESS
newer_direction                           # NONE|QUESTION|CORRECTION|STOP
work_state                                # NOT_STARTED|PARTIAL|COMPLETED|UNKNOWN_EFFECT
send_outcome                              # NOT_SENT|SENT|UNKNOWN
```

The reader checks exact task/revision/continuation/recipient, selected owner decision/original and actual current-source hash/read stability. Its timestamp/age remains caller-supplied, not clock-authenticated or automatically judged. It cannot parse a source body or prove the observer understood newer Human messages. Ordinary receiver reconciliation is essential. An unrelated new global source HEAD is not a stale gate when selected relevant code/task remains unchanged. A real newer task/owner decision, selected relevant code, scope/access change or relevant correction/STOP produces concrete owner-reconciliation with no payload; choosing full cannot bypass it. A mere Human question retains unfinished delivery. Unknown send/effect calls for reconciliation, already completed work says no replay; reading still performs no work/send.

The receiver always gets identity, manifest hash, selected representation, source/time/provenance limits, exact necessary original pointers and action-state warning. observation preserves BOTH actual work_state and send_outcome. COMPLETED+UNKNOWN retains completed/no-work-replay AND RECONCILE_UNKNOWN_SEND_BEFORE_ACTION in the action; completion never erases an unknown return/send duty or authorizes resend. Existing UNKNOWN_EFFECT remains a separate reconcile outcome. It gets COMPLETE_INLINE_TEXT only when the whole selected text fits12000UTF8 bytes. Otherwise EXACT_PARTS_REQUIRED_NOT_FULL_DELIVERY lists exact rendering/member path, UTF8 byte offsets/lengths/digests and indices; `read ... --part INDEX` returns EXACT_PART_ONLY (at most8000UTF8 bytes), total parts and `complete_delivery_claim:false`. Read all necessary parts/originals through the ordinary qualified surface. No head/tail clipping or truncated-tool-output-as-full claim. Tool JSON/metadata overhead is separate; the caller must allow enough output for each returned part. Optional part retrieval never changes production, manifest, capsule or send count. Native file/reference availability is not actual model reading/understanding.

CLI success exits0. Refusal exits2 with bounded OPEN_RECONCILE_SAME_ATTEMPT/reason and no payload; parser usage errors also exit2. Raw private exceptions/body bytes are not printed as error messages. Required/missing/mutated files, manifest hash, illegal members/reparse/traversal, recipient/task/owner/current drift, stale producer or profile inputs and incompatible version remain concrete scoped failures. Direct API raises the corresponding local exception. Authority is always NONE.

## Qualification, evidence, rollback and Way Home

`python -I -B ABS/tests/test_bk05_continuation.py` is the one changed A–F ordinary prepare/read CLI arc: both bound renderings and mutation, reviewer/owner disagreement, frozen identity/later observation/global-HEAD nonfit, missing/changed/private-path/recipient/scope/staging, interrupted context/full exact parts/no repeated effect, envelope double/nonfit plus targeted existing finalize/core callback compatibility. Synthetic profile data and callbacks do not prove actual reviewer finding, semantic held basis, transport, understanding, useful effect or saving. Existing old suites are not a requirement/replay.

Before actual admission A1 and same1B qualify their real runtime/caller/paths, resolve conflicting old entry clauses for THIS new route, read exact reviewed/published code and preserve prior compatible lanes/history. A1's issuer decision does not decide how much actual1B must read. Existing finalize's qualified held_basis_reader remains a separate legacy in-process option, never mandatory fake memory proof for this new self-reader. Existing explicit/handoff/resume entry points carry the selected method; no new hook engine or per-message gate. Reversible preparation and file readback do not claim entry placement/read/use.

Same655 changed-risk review goes through A1; AD1's one accepted rev2 contract critique is reused, not a second implementation approval. Actual publication/placement/explicit reading/first genuine A1→1B continuation/result/A1 disposition and useful recurrence are separately attributed. PM keeps relevant other-surface admission/nonfit/product rests in the existing matrix. No E/D/BK11 retask/private copy, protected probe, new BOOT/actor or model override. STOP_BEFORE_CAPABILITY_TEST/REFINE and relevant effect owner boundaries survive.

Rollback disables only this new short/read entry in the rightful owner binding, preserving immutable packet/original/review/owner action/history and competent full continuation. Unknown effect is reconciled before any repeated action. Return learning through actual task original/current→A1's BK05/Source/Work home→PM current→P23. Whole cost and recipient utility remain UNKNOWN until actual ordinary disposition; fewer bytes alone establish no token/net saving.
