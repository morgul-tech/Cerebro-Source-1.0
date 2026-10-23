# Callable placement and journal contract v1 — candidate

## One source of route IDs

The executor owns one `cerebro-drive-owner-routing-map/v1`. The caller provides its exact UTF-8 `raw_json` and actual `source_ref`; `request.routing.sha256` hashes those raw bytes, including line endings, **not** JSON normalization. `sha256(text)` is a caller-injected qualified SHA-256 implementation. JSON request/payload binding uses the factory's `canonical(value)` (sorted object keys, array order retained). Map filenames, labels or hashes do not grant authority.

The factory selects exactly `map.routes[request.purpose].parent_id`. There is no embedded live ID list, lookup by title, root/Misc fallback or duplicated policy map. Unknown/array-valued/ambiguous selection is HOLD. Exact route scope is `route.task` or `route.tasks[]`; both together, wildcard or duplicate matching task are refused. Initial legacy rows without route task scope apply only to their map's introduction `task`; that task is never silently equated with future callers. The executor must add a legitimate explicitly scoped route and increment revision for an actually selected other task. A caller cannot override its parent ID through `destination`.

`request` fields:

```text
schema: cerebro-drive-placement-request/v1
operation_id: stable own logical operation, [A-Za-z0-9_-]{1,120}; not a title
task: exact legitimate task/revision reference
purpose: exact executor map route key
kind: upload|folder|copy|create|import_document|import_spreadsheet|import_presentation|update
mime_type: actual expected resulting MIME
routing: {source_ref, revision:string, sha256: exact raw map SHA256}
payload_sha256: SHA256(canonical(private payload))
content: {source_ref, revision, sha256: expected binary/native-projection digest}  # nonfolder
existing: {file_id, content_update_authorized:true, source_ref, revision}         # update only
destination: {
  disposition: QUALIFIED_OWNER_HOME, fallback:false,
  allowed_operations:[actually authorized kind(s)], expected_parent_ids:[actual qualified ancestor ID(s)],
  access_boundary_ref, owner_basis:{source_ref,revision}, access: qualified branch below
}
```

`destination` is the owner's task-local authorized metadata/boundary expectation, not another routing map. Parent ID comes only from the exact map. Expected ancestors cannot be empty/self-parent; fresh actual parent ID/folder MIME/parents must match. Known native `trashed:true` always holds. Requests and journals are private task-owned; metadata journals never store payload bytes, private title/filename/URI, permissions or credentials. `payload` stays only in the lawful caller's memory/tool arguments and is not emitted by CLI prepare. Its admitted fields are file_uri/file_name, source_file/title, source_id/new_title, or folder name according to the actual kind. Do not pass arbitrary tool arguments or parent overrides.

## Fresh access metadata and honest limits

`access.mode=provider_capability` requires actual `capabilities.canAddChildren===true` and `trashed===false`. When the actual connector omits both despite a fields request, use only a rightful explicitly qualified alternative:

```text
access: {mode:owner_qualified_permission, permission_id:exact_observed_user_permission_id,
  source_ref, revision,
  unexposed_capabilities:OWNER_SCOPED_NOT_PROVIDER_ATTESTATION,
  unexposed_trashed:OWNER_DISPOSITION_REQUIRED_IF_UNKNOWN}
```

This branch requires fresh `source_visibility_status=permission_metadata_available` and exactly matching user permission ID with owner/writer/organizer/fileOrganizer role, not deleted. It qualifies the owner's scope, **not** an observed provider canWrite capability or a general access grant. `current_user_can_share` is never a write signal. The concrete caller must dispose of the known unexposed trash/access boundary; real connector errors remain HOLD. Missing permission IDs/metadata require the rightful owner to qualify another supported input, never manufacture a value. Camel/native and connector-normalized id/mime_type/parent_ids/url/title are accepted; conflicting simultaneous aliases hold.

## Actual managed-tool composition

Actual-normalizer continuation / NORMALIZER-02 (A1 original SHA256b31bd1f8caaf79fe0ceef8737645a20dbac6d7f442b663a9dab243f4481d486c): when the actual normalized user row omits permission IDs, the explicit owner-qualified reserve may choose `principal_selector:{kind:'observed_user_email',value:EXACT_PRIVATE_OBSERVED_EMAIL,source_ref,revision}` **instead of** permission_id. Exactly one selector is required; missing/both/malformed/conflicting aliases hold. Matching is the exact privately observed user email, not displayName/case guessing, and must yield exactly one allowed not-deleted user row under actual permission visibility. If that row exposes id/permission_id, use the ID selector instead. No ID is synthesized from email/hash. Known capability deny/conflict still wins before selector choice. The selector and actual principal remain private input; only existing scope labels, hashes and nonprincipal fields reach the operation journal/result/CLI stdout. Never echo the principal or native permission body into Shared evidence.

REFINE-01 / DPC-655-F1: before selecting either access branch, fresh explicit `canAddChildren:false` is HOLD_CAPABILITY_EXPLICIT_DENY, and a present malformed/conflicting non-boolean value is HOLD_CAPABILITY_CONFLICT. An omitted-field owner reserve cannot erase those known provider facts. Legitimate actual omission with qualified owner permission remains available, without a provider canWrite claim.

Load/evaluate the **hash-verified exact** `placement-factory.js` in the same functions isolate, then call `globalThis.CerebroDrivePlacement({sha256})`. In Node `require('./placement-factory.js')({sha256})` returns the same API. Code pin verification is the caller's real loading prerequisite, not asserted by the factory. No fs/Node import is used by the portable files.

```javascript
const tools = api.managedConnector({
  google_drive_get_file_metadata: args => ACTUAL_MANAGED_METADATA_TOOL(args),
  google_drive_upload_file: args => ACTUAL_MANAGED_UPLOAD_TOOL(args),
  google_drive_create_folder: args => ACTUAL_MANAGED_FOLDER_TOOL(args),
  google_drive_copy_file: args => ACTUAL_MANAGED_COPY_TOOL(args),
  google_drive_create_file: args => ACTUAL_MANAGED_NATIVE_CREATE_TOOL(args),
  google_drive_import_document: args => ACTUAL_MANAGED_DOC_IMPORT_TOOL(args),
  google_drive_import_spreadsheet: args => ACTUAL_MANAGED_SHEET_IMPORT_TOOL(args),
  google_drive_import_presentation: args => ACTUAL_MANAGED_SLIDE_IMPORT_TOOL(args),
  google_drive_update_file: args => ACTUAL_MANAGED_UPDATE_TOOL(args)
}, {decode: QUALIFIED_ACTUAL_RESPONSE_DECODER, readContent: ACTUAL_CONTENT_READBACK});
await api.execute({request, routingMap:{source_ref, raw_json}, payload, binding, tools, journal});
```

The callback names above are exact available connector operation shapes; the upper-case symbols are **not** shipped implementations or evidence of invocation. `decode(raw, operation)` must handle the actual connector result/error shape: metadata returns its observed record; writes return `{id:observedReturnedID}`. A connector `isError` must throw, never parse as success. If the only returned identity is in an actual returned URL, the decoder may extract that observed identity, not invent/predict a URL/ID. Unknown create result without positively observed ID stays HOLD. No transient URL is returned by the public execute result.

`readContent({fileId,mimeType,expectation})` separately reads the same exact file and returns `{file_id,source_ref,observed_sha256}`. Binary bytes or sufficient native content are hashed/projected under the selected content contract. Native body stays in the lawful same managed-tool isolate; no native response to shell/file/cache/store. Supplying the expected digest again without actual reading is not this port. Folders instead require exact name, folder MIME and parent readback. Content callbacks are read-only and may not hide a write/create.

`binding={source_ref,revision,connection:oauth_my_drive|direct_shared_drive}`. OAuth/delegated native create/import additionally requires `transient_staging=EXPLICIT_NEW_ID_MY_DRIVE_ROOT_COMPLETE_BEFORE_LINK`; parent args are omitted. Direct shared-drive mode supplies parent only under the actually qualified service-account connection. Upload uses parent_folder_id; folder/copy parent_folder. Native import variants use their matching upload_mode. Update accepts only already authorized same-ID raw content at the already correct parent; native content update is refused for the separate native component to own.

NORMALIZER-02 root port: the actual normalized literal `readMetadata('root')` may expose parent_ids:null. Only an explicit OAuth My Drive binding `root_lookup:{lookup:'literal_root',expected_id:EXACT_ACTUAL_ROOT_ID,source_ref,revision,null_parent_disposition:'ACTUAL_LITERAL_ROOT_NORMALIZER_OMITS_PARENTS'}` can qualify that shape. The literal-root callback must return that exact observed ID and folder MIME; selected destination itself, nonempty/conflicting parents, absent (undefined) parent metadata, wrong binding/ID/type or missing provenance hold. Actual observed [] root parents remain separately supported. The returned root evidence marks normalized-null as **not an observed empty list**; generic metadata/targets still refuse missing/null parents. No name-based root inference or generic null→[] decoder exists.

Before the first new native create/import, execute actually reads and qualifies root metadata and persists its ID/evidence with intent. A known unsupported root shape therefore holds before artifact creation. Immediately before own-new-ID staging completion, fresh root and target are qualified again; a changed root ID or an artifact whose sole parent is not that exact root holds without a placement. This does not grant any root destination fallback, existing-object cleanup right, terminal artifact mutation or access capability. A1 qualifies the real private-principal/root bindings after independent review; the sanitized actual shape observation is source evidence, not provider-write/use proof.

## Durable exclusive journal and recovery

`journal={qualification:{source_ref,revision,durability_evidence_ref,exclusivity_evidence_ref},runExclusive(operationId,async storage=>...)}`. `storage.load()` returns null or one exact record; `storage.commit(expectedVersion,record)` is a durable compare-and-set under the held exclusive owner lock, returning `{version,durable:true,evidence_ref}`. The factory **reloads and compares the entire record after every commit**. Fields/booleans alone are not proof; A1/actual owner qualifies storage, actual readback and exclusivity before use. Memory variables/functions.store cannot implement crash durability. There is no timeout, lease stealing or retry loop.

Intent is committed before create/update. Returned ID is committed before any subsequent metadata/content/placement work. The ordinary returned URL is released only after exact ID/MIME/sole parent and relevant content/name readback, another final metadata read, a URL observed from that read containing the same ID, and persisted release state. Read→write is not provider CAS; caller must preserve real exclusive producer/effect scope. Raw edits outside the adapter can bypass it.

Unknown create without ID never creates again. Positive recovery uses `recoverKnownCreate({operationId,evidence:{operation_id,file_id,intent_hash,source_ref,revision},journal,verifyReceipt})`; the actual caller must verify the exact provider result receipt against that operation/intent. Filename uniqueness/search, guesses and flag-only attestation are insufficient. A known ID resumes reads/content checks using the same record. Unknown content/placement outcomes reconcile the same ID; a failed/uncertain placement is not automatically repeated. Existing wrong-parent objects are never moved here. Only a new operation's explicit transient native root staging may move, after reading `root` to observe the actual account root ID and matching that exact current parent; no generic removeParents from arbitrary metadata.

Local binding: preinstall/qualify a private existing `placement-journal.jsonl` in an existing non-symlink directory before use. One global exclusive lock serializes append snapshots. Each append is fsynced and reread; partial lines/CAS drift/stale lock hold without truncation or stealing. Installation/filesystem durability is a separate actual owner qualification, not proven by a unit test. Fixture temporary cleanup is not real job rollback.

Native Chat binding: load/hash-verify `doc-journal-factory.js` in the same isolate, then `CerebroDriveDocJournal({read,write,newLockToken,qualification})`. A1 first creates/qualifies ONE owned native JSON-journal Doc under the selected task home through the legitimate released path. Bootstrap body is `{schema:'cerebro-drive-doc-journal/v1',version:1,lock:null,operations:{}}`. `read()` performs actual managed get_document, returning its exact revisionId plus dedicated JSON body text, **within that isolate**. `write({requiredRevisionId,text})` must use actual batch_update_document write_control.requiredRevisionId and replace ONLY that reserved body's range, preserving the qualified document/tab format. No implicit body-range guessing or fallback CAS. The adapter always rereads matching body at a changed provider revision. A1 owns the exact native tab/range decoder/writer binding and records readback, not merely an isSuccess flag.

One unique token locks via provider CAS before the first placement action; each commit checks token/operation and expected operation version, uses current provider revision CAS and reads back. Conflicts/unknown acquisition hold, no retry/rebase/stale-lock steal. Known normal exit releases only its exact observed token via another CAS/readback. Crash/unknown release is a concrete owner-reconciliation rest. The code offers no new service, credentials or claim of native Chat runtime support; A1 must qualify the actual native journal/callback port and the correct Chat surface before claiming use.

## Evidence levels and independent control

REFINE-01 / DPC-655-F2: VERIFIED_RELEASED is terminal for the artifact's create/copy/content/parent effects. A subsequent exact same-ID parent/MIME/content/link observation may be reused, without calling artifact tools.invoke or rewriting the terminal operation record. Parent drift holds for the current legitimate owner to reconcile; an old create cannot reopen staging or cleanup authority. Actually unfinished own-new-ID staging and prior unknown-outcome guards remain distinct. Under A1 OWNER-CLARIFICATION-01 (SHA2567e40e485a159be060bb85db72420cf3f9061b1a27a59490d2a57d01c72234a06), the qualified journal's existing lock/release CAS is separate housekeeping and remains unchanged. Thus terminal observation is not a claim of literally zero native-provider writes. No readOnly-journal API, path expansion or new architecture is introduced. Original 26-case evidence is reused; only discriminating REFINE-01 regression cases are run for this repair.

Focused synthetic controls cover routes/task scopes, raw binding drift, actual descriptor arguments, permission-normalizer limit, intent/ID/readback interruption, direct/transient native imports, known/unknown same-ID paths, folder/copy/update, CAS conflict/locks/partial journals, portable IIFE and shared CLI. They do not prove cloud writes, cleanup, Chat placement, a native provider event or persistent-port admission. Preserve valid old E1 evidence without rerunning its unrelated controls. Actual code release requires existing independent 655 final changed-risk review and A1 disposition. Representative ordinary Chat/Work/script use remains with actual qualified owner/surface; current Human-window Work chat cannot retroactively be labeled ChatGPT or method use.
