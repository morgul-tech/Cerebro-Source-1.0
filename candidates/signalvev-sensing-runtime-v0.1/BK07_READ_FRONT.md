# BK07 read front — navigation bookmarks + revision/access-bounded reuse (AUTHORITY NONE, default OFF)

Assignment `EXT-CLAUDE-BK07-DRIVE-READ-REUSE-V01` rev 1.0. This is a thin integration around existing homes:
* the CAP26 `ContextSliceResolver` / provider port, on the basis `a51fea0` + X4 `CAP26_FROM_BASE.patch`;
* the DNI reader-v2 `orient_paragraph`;
* the owner-supplied BK07 navigation registry.

It is not a new truth store, Drive reader, OwnerResolver, scheduler or PM-X9 replacement.

| File | Role |
|---|---|
| `src/signalvev_sensing/bk07_read_front.py` | `read_front(request, ports, state)`, registry import, manifest bookmarks, reuse policy, DNI adapter, trusted-config ports, one-shot CLI |
| `src/signalvev_sensing/read_front_state.py` | local derived state: one writer, atomic replace, versioned schema, corruption detection, bounds |
| `tests/test_bk07_read_front.py`, `tests/bk07_read_front_support.py` | focused tests for the new seams and the direct CAP26/DNI composition |
| `tests/bk07_compare_driver.py` | deterministic offline comparison driver (SYNTHETIC_TEST_ONLY) |
| `tests/bk07_read_front_fixtures/` | the frozen package fixtures, copied unchanged |

No existing file changed. CAP26 (including the X4 fix), the DNI reference, `pm_x9.py`, `pm_x9_cli.py`,
`control_context_registry.py`, the receiver/model/resolver files and Atlas are untouched.

## Callable and CLI

```
read_front(FrontRequest, ReadFrontPorts, ReadFrontState) -> FrontResult      # .to_json() <= 4096 B, .receipt() <= 2048 B
python -m signalvev_sensing.bk07_read_front --config <trusted-local-config> --question BK07 --receipt <new-file>
```

CLI exit codes:

| Exit | Meaning |
|---|---|
| 0 | FRESH_SLICE / REUSED_SLICE / ORIENTATION_ONLY |
| 3 | DISABLED / MISSING_PORT / FALLBACK_REQUIRED |
| 2 | any other typed outcome |
| 64 | unreadable config, or the receipt file already exists (never overwritten) |

Outcomes are FRESH_SLICE, REUSED_SLICE, ORIENTATION_ONLY, INVALIDATED, STALE, SUPERSEDED, DENIED, AMBIGUOUS,
FALLBACK_REQUIRED and UNREADABLE (plus DISABLED, MISSING_PORT and INVALID_REQUEST at the edge). They are navigation
outcomes, kept separate from PM-X9 STALE/NO_MATERIAL_DELTA/MATERIAL_ROUTE. Every result carries:
* `wake: NONE`
* `chat_message_injected: false`
* `pm_x9_disposition: NOT_TOUCHED`

Negatives never carry content, row text, projection text or principal/scope values.

## Trusted local config (`bk07.read_front_config/v1`; default OFF unless `"enabled": true`)

```json
{"schema":"bk07.read_front_config/v1","enabled":true,"mode":"FIXTURE|LIVE_OBSERVATIONS",
 "state_path":"<A1 BK07 runtime home>/read_front_state.json","subject_ref":"<subject>",
 "questions":{"BK07":"BK07.READ_FRONT"},
 "registry_path":"<owner-supplied BK07 navigation registry export>",
 "manifest_path":"<fresh bounded owner manifest for the known scope>",
 "known_scope":{"scope":"<scope id>","message_ids":["..."],"max_rows":128},
 "cap26":{"provider":"LOCAL_PINNED_SNAPSHOT_FIXTURE","root":"...","registry_path":"..."},
 "import_roots":{"dni_reader_v2":"<dir holding the pinned reader_v2_adapter.py>"},
 "dni_observations":{"<documentId>":{"find":"<find json>","paragraph":"<paragraph json>"}},
 "access":{"principal_ref":"...","scope_fingerprint":"...","provider_context":"...","attestation":"<basis>|NONE","expires_at":<epoch>},
 "human_turn_flag_path":"<optional; observed only>"}
```

Mode rules:
* The `cap26` block is accepted ONLY in `FIXTURE` mode. In `LIVE_OBSERVATIONS` it returns `MISSING_PORT
  CAP26_LIVE_PROVIDER_NOT_AVAILABLE`, so live mode never silently falls back to the synthetic provider.
* Without a `cap26` block, live mode returns `ORIENTATION_ONLY` (with `missing_port: cap26_provider`) from the trusted
  DNI observations, or `MISSING_PORT` with one responsible action.
* The DNI adapter is imported from the explicit root and must match SHA256
  `e036f169…3a3902` (the exact `reader_v2_adapter.py`).

## Flow per lawful call (one shared operation deadline ≤ 5 s)

**A. Registry**
* Bounds: ≤ 64 entries, ≤ 64 KiB, strict keys, ID-validated refs (no path semantics).
* Status: the current owner status plus an explicit `superseded_by` chain (depth ≤ 4, cycle detection) decide.
* Results:
  * more than one active referent → AMBIGUOUS;
  * missing status, evidence or replacement → FALLBACK_REQUIRED;
  * the old original appears in `historical` and is never CURRENT.

**B. Rows**
* One fresh manifest for the configured scope only (≤ 128 rows, coverage must be complete). Stable `message_id` plus
  the exact row digest bind identity, and each row is classified:

  | Change | Meaning |
  |---|---|
  | NEW | id not seen before |
  | UNCHANGED | same digest, same locator |
  | MOVED | sort: locator rebuilt, no text resent |
  | EDITED | content changed; detected anywhere, including below the old highwater |
  | TOMBSTONE | id removed from a complete manifest |

* Only NEW and EDITED rows carry text.
* Duplicate ids → AMBIGUOUS. Incomplete coverage → FALLBACK_REQUIRED. In both cases the rows are delivered as
  `FRESH_UNQUALIFIED`, the scope bookmarks are invalidated and global completeness stays UNKNOWN.

**C. Content**

*Hit.* All of the following must hold:
1. a valid stored entry with the identical per-entry binding (source, owner, status, revision, full hash, selector,
   slice hash, representation, tab/anchor);
2. attested and unexpired access with the identical principal, scope fingerprint and provider context;
3. a local hash check of the cached slice;
4. **two** CAP26 `describe_only` calls, each identical to the stored descriptor (revision, hash, way_home, snapshot
   identity, basis);
5. the operation deadline has not expired.

The result is REUSED_SLICE with 0 content opens.

*Hit failures:*
* a binding, access or cache failure before any describe → the entry is invalidated, then ONE cold read;
* a describe failure or changed descriptor → INVALIDATED, DENIED or SUPERSEDED with no payload. There is no retry
  loop; the next call does the cold read.

*Cold read.* CAP26 `resolve` performs the full verification. Only the ≤ 1024 B verified slice is cached, and only with
attested access; otherwise the bookmark is kept without payload as REUSE_UNQUALIFIED.

*Native Docs.* DNI `orient_paragraph` → ORIENTATION_ONLY or REUSE_UNQUALIFIED:
* the locator is a hint per document revision, re-found on revision change;
* anchor or tab drift → INVALIDATED;
* a UTF8 selector on a Docs entry → FALLBACK_REQUIRED (UTF-16 indices are never CAP26 byte ranges);
* there is no reader-issued receipt, so the result is never revalidated or CURRENT.

## State (`bk07.read_front_state/v1`)

* JSON `{schema, body, body_sha256}` written via an exclusive lock, a temp file, fsync and `os.replace`.
* Corrupt or tampered state → `CORRUPT_IGNORED` and a cold read.
* Bounds: ≤ 64 entries, ≤ 1 MiB cached slice bytes, ≤ 16 scopes × 128 rows.
* No eval or pickle. Access is stored only as ids/fingerprints.
* An unrelated entry (for example BK11) is untouched when only the BK07 binding changes.

## Adoption recipe (after X2 identity check and the existing Source review/admission)

1. **Import roots:**
   * `PYTHONPATH=<Source>/candidates/signalvev-sensing-runtime-v0.1/src`
   * `SIGNALVEV_VALIDATOR_DIR=<Source>/tooling/validator`
   * the DNI root holding the pinned `reader_v2_adapter.py`
2. **Config:** A1 writes the trusted config under the proposed `D:/Cerebro/Run/Signalvev/BK07/read-front/` (a
   proposal, not a deployed-path claim). Use `mode=LIVE_OBSERVATIONS`. The registry export, manifest and DNI
   observation files come from A1's existing connector reads (trusted bootstrap). Keep `enabled` false until admitted.
3. **Host ports:** in live mode the CAP26 content port stays MISSING until the selected owner supplies a live
   `ContentSliceProvider` (the CAP26 residual port). Until then the result is ORIENTATION_ONLY or REUSE_UNQUALIFIED.
4. **Effect:** passive read only. It writes the local derived state file and one receipt; no owner, Drive, Atlas or
   PM-X9 mutation.
5. **Rollback:** set `enabled:false` or remove the entrypoint and delete the state file. The existing bounded read and
   CAP26 full-original stay available; originals and receipts are kept.
6. **First use:** run the next natural BK07 question once through the entrypoint and record the actual receipt
   counters in the same Living document. If no matching control exists, record `NO_MATCHED_BASELINE`.


## C1180 bounded X2 return corrections
Final body publication refreshes attested access and compares principal/scope/provider
context with initial read/reuse qualification. Expired/changed/unknown access returns
DENIED without slice/orientation/row bodies and invalidates the affected cache.
External registry/manifest/access/CAP26/DNI observation and DNI orientation seams check
the SAME overall monotonic deadline before and after. All representation/fallback paths
are subject to the final gate; no late DNI body is emitted or committed.
State commit has an optional publication check inside its exclusive lock, before and
after atomic replacement. Rejected deadline/access/envelope publication restores the
previous bounded state snapshot while holding that lock (or removes its own first file).
Rollback is cleanup, not another read attempt. Real counters are never clipped; final
persistence/counter representation is conservatively reserved before publication.
Rows retain separate observed digest/locator and delivered_digest in the SAME disposable
derived state file; no row body is stored as canonical authority. Negative/envelope-failed
responses do not acknowledge row delivery. Next fresh manifest reread emits undelivered
rows as PENDING_DELIVERY; a successfully serializable qualified response records the
digest, so the next unchanged response omits row bodies. Legacy rows lacking this marker
are treated as pending once. This is serialization-qualified delivery bookkeeping;
it is NOT an actual consumer ACK or a claim of exactly-once effects after process crashes.
The corrected CAP26 eb9bd19b dependency is an OFFLINE candidate basis, not current admitted
Source/provider proof. Exact1947 and older reviewed receipts are reused, not replayed.
Eight new changed-risk tests PASS on Python3.13.15 Windows, with frozen SYNTHETIC fixtures
and exact pinned DNI adapter. No old28/cap26 suites or mutations repeated; no live
provider, install, backend/token/net gain or savings claim.
