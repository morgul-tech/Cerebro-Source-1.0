# CAP26 — pointer-first context slice resolver (opt-in, authority NONE)

Assignment `CLAUDE_CAP26_POINTER_CONTEXT_RESOLVER_V01` rev 1.1. One concrete reader plus a thin adapter behind the
**existing** `OwnerResolver`. This is not a context platform, store, index, scheduler, model router or token tool. Nothing
here is wired into the runtime by default. A caller constructs it explicitly. It performs passive one-shot reads with no
background work, no retry, no cache and no owner mutation.

| File | Role |
|---|---|
| `src/signalvev_sensing/context_slice.py` | typed request/result, strict parsing, `UTF8_BYTE_RANGE` selector, bounded verified read, full-original, counters |
| `src/signalvev_sensing/context_slice_provider.py` | provider protocol + `LocalPinnedSnapshotProvider` (the only executable provider) |
| `src/signalvev_sensing/context_slice_owner.py` | `ContextSliceOwnerResolver` — existing `OwnerResolver.resolve(ResolveRequest) -> ResolverResult` |
| `src/signalvev_sensing/context_slice_cli.py` | offline one-shot `resolve` / `full-original` / `compare` |
| `tests/test_context_slice.py`, `tests/test_context_slice_owner.py`, `tests/context_slice_support.py`, `tests/context_slice_fixtures/` | focused tests A–H + supplied harmless fixtures (copied unchanged) |

No existing file was changed: `receiver.py`, `model.py`, `resolver.py`, `__init__.py`, BK05 and the Durable reference are untouched.

## Setup (offline, stdlib only, CPython 3.12+; produced and tested on CPython 3.13.15 Linux)

```
python -B bootstrap_materials.py --out ./inputs                     # from the input package
export SIGNALVEV_VALIDATOR_DIR=<inputs>/source/tooling/validator      # explicit, pinned (default locator also works)
export PYTHONPATH=<inputs>/source/candidates/signalvev-sensing-runtime-v0.1/src
cd <inputs>/source/candidates/signalvev-sensing-runtime-v0.1/tests
python -B -m unittest -v test_context_slice test_context_slice_owner
```

## Public API

* `ContextSliceResolver(provider, *, clock=time.monotonic).resolve(SliceRequest) -> SliceResult`
* `ContextSliceResolver.full_original(SliceRequest, *, max_full_output_bytes, out_path) -> SliceResult`
* `ContextSliceResolver.describe_only(SliceRequest) -> (SliceResult, ReadDescriptor | None)`: metadata only, used for REVISION_CHECK
* `ContextSliceOwnerResolver(resolver, *, subject_ref, bindings=[PointerBinding...]).resolve(ResolveRequest) -> ResolverResult`
* `parse_request(bytes | dict) -> SliceRequest` (strict)

Deadlines are absolute values on ONE clock. The resolver and its provider must share that clock (both default to
`time.monotonic`).

## Provider protocol (frozen)

```
describe_authorized(subject_ref, content_ref, owner_ref, *, deadline) -> ReadDescriptor     # fresh, metadata only
open_verified_snapshot(descriptor, *, max_bytes, chunk_bytes, deadline) -> SnapshotHandle   # context manager
    handle.chunks()  -> iterator of bytes chunks of ONE stable open handle (<= chunk_bytes each)
    handle.confirm() -> bool: handle AND path identity still equal descriptor.snapshot_identity
errors: ProviderDenied(code) | ProviderUnavailable(code) | ProviderDeadline(code)   (codes only; no paths or content)
```

`ReadDescriptor` fields:
* identity: `canonical_ref`, `owner_ref`, `revision` (opaque), `size`, `sha256` (full raw content), `way_home`
* currentness: `head_relation` (SAME|SUPERSEDED|UNKNOWN), `superseded_revisions`, `owner_seq` (int only when attested,
  else null), `semantic_currentness` (UNKNOWN)
* basis: `authorization_basis`, `assertion`, `provider`, `snapshot_identity`

The resolver never trusts the descriptor alone:
* it counts bytes while reading and caps at `min(max_source_bytes, size)` even if the metadata lies;
* it rejects any chunk larger than `chunk_bytes`;
* it hashes every byte itself and requires `computed == descriptor.sha256 == expected_content_sha256` and `total == size`;
* it calls `confirm()` at EOF;
* it performs a SECOND fresh `describe_authorized` and requires an identical descriptor (head, ACL and identity unchanged).

No byte is returned before all of that. Budget `max_provider_calls=2` covers the two describes. Content opens and
reads are counted separately (`content_opens`, `read_calls`, `bytes_read`, `bytes_hashed`).

### LocalPinnedSnapshotProvider

The provider takes a configured `root` and an explicit registry file. The registry is re-read on every describe and
open. It never accepts a caller path or URL. The order of checks is:
1. Strict registry parse: unknown or duplicate keys, bad types, duplicate refs or more than 64 KiB/256 bindings make the
   whole registry fail closed.
2. Unknown ref, foreign owner or subject not in `authorized_subjects` returns DENIED with zero filesystem access.
3. Path validation: only root-relative POSIX segments matching `[A-Za-z0-9][A-Za-z0-9._-]*` are accepted. This rejects
   absolute, drive, UNC, backslash, `..`, `.`, `%`-encoded, empty and NUL segments.
4. Every component is `lstat`-checked. A symlink or reparse point returns DENIED. A non-regular file returns DENIED. The
   resolved path must stay under the resolved root.
5. The file is opened with `O_NOFOLLOW` where available. An `fstat` identity (dev/ino/size/mtime_ns) must equal the
   descriptor's identity.

The registry is a `SYNTHETIC_OWNER_ASSERTION`. It is not authenticated source evidence and never semantic currentness.
`ProviderStats` records the actual provider operations: describes, registry reads, content opens, read calls, bytes
read and stat calls.

Registry binding keys:
* required: `content_ref`, `path`, `owner_ref`, `revision`, `sha256`, `authorized_subjects`, `head_relation`,
  `semantic_currentness`=UNKNOWN, `owner_seq` (int|null), `way_home`
* optional: `superseded_revisions`, `size`

## Request schema (JSON, strict)

Required keys: `content_ref`, `expected_revision`, `expected_content_sha256` (64 lowercase hex), `subject_ref`,
`owner_ref`, `way_home`, `selector`, `limits`.

Optional keys:
* `need`: `SELECTED_RANGE` (default) or `COMPLETE_ORIGINAL`. `COMPLETE_ORIGINAL` returns FALLBACK_REQUIRED without
  opening content.
* `allow_unknown_head_relation`: default false. When true it allows a pinned-only standalone read, which is never
  receiver-acceptable.

`selector` = `{"kind":"UTF8_BYTE_RANGE","start":int,"end":int,"expected_slice_sha256":hex}`:
* zero-based, half-open, `0 <= start < end <= size`;
* the slice must decode as strict UTF-8, so a split sequence returns SELECTOR_DRIFT;
* the slice SHA256 must match;
* no other selector kinds and no multiple ranges.

`limits` may only lower the hard maxima. Absent keys take the default:

| key | hard max / default |
|---|---|
| `max_source_bytes` | 1048576 |
| `max_slice_bytes` | 1024 |
| `max_result_bytes` | 4096 (whole UTF-8 JSON envelope) |
| `max_provider_calls` | 2 |
| `max_deadline_seconds` | 5 |
| `chunk_bytes` | 65536 |

Rejected with INVALID_REQUEST: unknown or duplicate keys, wrong types (including bool-as-int), NaN or Infinity, blank
or invalid refs, missing fields, requests over 8 KiB.

## Result schema `cap26.slice_result/v1` (canonical JSON: sorted keys, `,`/`:` separators, UTF-8, no NaN)

| Field | Content |
|---|---|
| `outcome` | `SLICE_READY`, `FULL_ORIGINAL_READY`, `FALLBACK_REQUIRED`, `STALE`, `HASH_MISMATCH`, `SELECTOR_DRIFT`, `DENIED`, `IDENTITY_MISMATCH`, `UNREADABLE`, `INVALID_REQUEST`, `LIMIT_EXCEEDED` |
| `reason` | machine code |
| `authority` | `NONE` |
| `request` | echo of the request's refs, revision and hash |
| `selector` | echo |
| `observed_revision` | from the owner descriptor |
| `observed_owner_ref` | from the owner descriptor |
| `descriptor_content_sha256` | owner assertion |
| `verified_content_sha256` | computed by the resolver |
| `slice_sha256`, `slice_bytes`, `slice_text` | **`slice_text` only on SLICE_READY** |
| `provenance` | provider, assertion, authorization basis, snapshot identity |
| `currentness` | `pinned_identity`, `head_relation`, `semantic_currentness`=UNKNOWN, `owner_seq_attested`, `receiver_acceptable`, `basis` |
| `way_home` | owner evidence; the caller's value must agree or the outcome is IDENTITY_MISMATCH |
| `owner_seq` | attested only |
| `fallback` | full-original descriptor: ref, revision, hash, size, owner, scope, way_home, `body_inlined:false` |
| `full_output` | `file`, `bytes`, `sha256`, `complete` |
| `missing_capability` | set when a capability is missing |
| `counters` | provider_calls, content_opens, read_calls, bytes_read, bytes_hashed, retries=0, elapsed_ms |

Digest domains:
* full = SHA256 of all canonical raw bytes;
* slice = SHA256 of the returned raw range;
* the envelope is size-bounded and not hashed.

Opaque revisions are compared by equality only. A matching hash is never `CURRENT`.

## Full original (explicit caller choice)

* `full-original` applies the same authorization, currentness, hash pin, stable-snapshot and size checks as `resolve`,
  plus a recheck.
* Without `--max-full-output-bytes` and `--out` it returns a descriptor only: FALLBACK_REQUIRED with `missing_capability`.
* The budget is separate from the 4096-byte slice envelope. Its hard maximum is 1 MiB, and it must cover the source plus
  a 4096-byte envelope reserve; otherwise LIMIT_EXCEEDED with a lawful pointer and no truncation.
* The verified body is written only after the EOF proof and recheck, with exclusive create (no overwrite).
* No fallback overcomes DENIED, STALE, HASH_MISMATCH, UNREADABLE or a deadline.

## CLI

```
python -m signalvev_sensing.context_slice_cli resolve       --registry REGISTRY --request REQUEST [--root DIR]
python -m signalvev_sensing.context_slice_cli full-original --registry REGISTRY --request REQUEST [--root DIR] \
                                                            [--max-full-output-bytes N --out NEW_FILE]
python -m signalvev_sensing.context_slice_cli compare       --registry REGISTRY --request REQUEST [--root DIR] \
                                                            --out-dir NEW_DIR [--max-full-output-bytes 131072]
```

* `--root` defaults to the registry's directory.
* Exit codes: 0 = SLICE_READY / FULL_ORIGINAL_READY / compare complete; 3 = FALLBACK_REQUIRED; 2 = other typed
  outcome; 64 = usage problem, existing out-dir or unreadable request file.
* `compare` runs two FRESH scenarios (new provider and resolver each, no cache): an authorized full original with
  `max_full_output_bytes=131072`, and the slice. It writes `measurement.json` (`cap26.measurement/v1`), both envelopes
  and the verified full body.
* Model-bound output = envelope + body file.
* Tokens, model turns, spend and net saving are `UNKNOWN`: no named or versioned tokenizer and no model use were observed.
* The slice path still reads and hashes the whole original locally.

## OwnerResolver adapter (`ContextSliceOwnerResolver`)

Bindings:
* bounded explicit `PointerBinding`s: `pointer_ref` (an opaque key equal to the event's `delta.ref`), owner, referent,
  `content_ref`, `way_home`, selector and limits;
* event owner and referent must equal the binding; otherwise `ResolverUnavailable`, which the existing receiver turns
  into HOLD_UNREADABLE.

| Depth or outcome | `ResolverResult` returned |
|---|---|
| `REVISION_CHECK` (pointer_ref null; binding found by owner+referent) | one describe, **zero content opens**; `grounding=None`; `observed_sha256` = owner's full-content hash |
| `POINTER_GROUND`, `SLICE_READY` | `revision_relation` = owner head relation; `observed_sha256` = **verified full hash**; `grounding` = slice text, slice sha256, selector, provenance, currentness, way_home (canonical size ≤ `MAX_GROUNDING_BYTES` = 4096) |
| STALE + SUPERSEDED | SUPERSEDED (existing STALE_SUPERSEDED) |
| STALE + UNKNOWN | UNKNOWN (existing HOLD_UNREADABLE `REVISION_RELATION_UNKNOWN`) |
| caller hash ≠ owner hash at the owner's SAME revision | returned as observed, so the existing receiver gives CONFLICT_HOLD; never a fabricated SAME + expected hash |
| everything else | `ResolverUnavailable` |

In every case:
* `source_ref` = the owner the provider described;
* `owner_seq` is set only when the provider attests it (the existing receiver then confirms or conflicts; no invented
  highwater).

## Not claimed / residual ports

Fixture tests are synthetic. They are not live Drive, EDGE, Durable or Docs evidence. Each residual port is listed with
who supplies it:

1. **Real provider adapter (selected owner):** lawful fresh authorization, authenticated revision/identity readback
   before and after, honoured remaining deadline per call, and stable snapshot or fail-closed. For Google Docs this also
   needs an owner-approved canonical export with format/tab/serialization provenance.
2. **Installation (X4/Source):** installation import root and default-off wiring.
3. **Runtime and config (A1):** target runtime and config.
4. **Canary:** one separately authorized bounded read canary.


## X4 bounded correction 2026-10-06
Every SliceResult.to_json() checks the complete UTF-8 canonical JSON envelope against
the request max_result_bytes, including negative, fallback, metadata and full-original
results. An oversized slice becomes a no-content FALLBACK_REQUIRED only when that
complete envelope fits. Identity/provenance/Way Home are never trimmed to fit.
When even the required envelope does not fit, ResultEnvelopeUnavailable is a typed
transport failure: ZERO serialized result bytes; retain the original request and its
Way Home locally. The CLI exits 2, writes only the fixed failure code on stderr and
writes zero stdout bytes. This stderr diagnostic is not a result envelope.
Impossible initial envelopes are rejected before describe; a required provider-bound
fallback that cannot fit is rejected after authorized describe but before content
open. No global numeric minimum is invented; the exact full envelope decides.
Full-original receipt serialization is checked before exclusive output-file creation.
The OwnerResolver maps the transport failure to existing ResolverUnavailable/HOLD_UNREADABLE
and clears last_result, so an earlier successful read cannot masquerade as this one.
The shared monotonic deadline is checked immediately after describe returns, before
any descriptor/currentness is used. Late metadata returns UNREADABLE and no descriptor.
Windows may deny replacing an open file: the negative test then requires
UNREADABLE/READ_INTERRUPTED and no slice. Platforms allowing replacement still require
SNAPSHOT_IDENTITY_CHANGED_DURING_READ. Symlink skip remains UNRUN.


## X5 exact1947 final-fallback correction
The preread reservation now includes the FINAL verified-content digest field,
EXACT_FULL_SHA256_VERIFIED currentness, the complete fallback and conservative
final counter lengths, for both resolve and full-original. The reservation is
internal sizing ONLY and is never returned as verification evidence. Successful
read counters are reserved for request source/chunk/provider bounds, including
a read-call cap max_source_bytes+1 (bounds empty-chunk loops without a new knob),
and deadline-bounded elapsed formatting with one serialization second reserved.
Real counters are never clipped. Final to_json remains the mandatory guard.
A tight cap previously fitting a preread envelope may now be refused BEFORE
content open; original identity/provenance/Way Home are preserved, never trimmed.
The prior positive control tests use adequate caps (4096 normal /2600 oversized
slice fallback); they were NOT rerun or claimed as newly verified. ONLY the exact
1947 supplied regression was executed on this delta: typed failure,1describe,
0content opens,0original bytes read. Originaldeadline/Windows/finalserialization
evidence is reused in its reviewed scope, not expanded to production proof.
