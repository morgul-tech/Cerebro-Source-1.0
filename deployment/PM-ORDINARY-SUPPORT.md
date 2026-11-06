# r3 — concrete existing custodian history and honest manual owner phases

This is the CURRENT source/API contract for implementation-r3. R1's INSTALL-AND-ENTRY-CONTRACT and r2 closure remain retained history; their callback API is superseded here. R1 manifest939d and r2 manifest5a2ef0/zipaaab remain immutable. E2 closed SUPPORT-01/02 on r2 in review693067f68ec5c62f13eba449a9cb6070526d108cdeb6aa3c2b88014c93236118. Both fixes remain in r3. Source, test, deployment, commissioning, PM use and useful outcome remain separate evidence states.

## Actual method and source owner split

The selected carrier is existing Railway Context/root with dedicated `/var/lib/cerebro-pm-authority/authority`; D1 owns actual volume/provision/runtime/start/operator/receiver/issuer transport. No build/pre-deploy volume use, DB-volume loan or credential move. A1 factoryea990 and separate ordinary-lower7c0ad are byte-identical; installed legacyb061/startup/Assistant/trial controls stay untouched. Warm ordinary path and root runner use the separately named `pm_ordinary_owner_operator`. D1 dc72 read-only mount/custody leaf is a mandatory shared entry gate, not just an optional readiness call.

D1's existing supported history route is OPERATOR-SUPPORT1.3 -> CURRENT_PM -> actual HumanCyborg custodian -> one owned Railway SSH/private capture driver. Independent archive is `C:/Cerebro/Authority/PM/history`, protected SID/SYSTEM and separate from the Context volume. The driver keeps immutable pre-dispatch invocation marker before target dispatch, actual before/after full ledger exports, effect/result and own closure originals. Unresolved marker, missing original, volume/config mismatch or uncertain prior effect gives named custodian HOLD. Current files/chats are locators, not grants. There is no new attest service, HA engine or actual future candidate.

## Implemented concrete history reader — no callback slots

`OrdinaryPmSupport(expected_target, expected_backend_sha256, expected_volume_ref, source_files, clock=...)` has **no history_verifier or observation_verifiers callbacks**. The protected installed composition binds actual target, backend and actual selected provider volume ID from supported afterread; worker JSON/environment is not the trust root. Every loaded direct module, including `pm_custodian_history.py` and dc72 mount leaf, must be included in the sealed source pins. All entries take the same exclusive nonblocking root service lock, check code/target and actual selected mount/custody.

`export_history()` is a private owned SSH/capture API returning bounded canonical full retained history bytes. It preserves exact BLOBs as hex and all appointments, operations including spent/UNKNOWN, manual/executable checkpoints and rebind originals. The exported ledger digest is recomputed over actual ordered appointment/operation/checkpoint bytes; config hash is separate. No public export/CLI or authority claim.

The owning driver supplies fixed root-private `custodian-history-original.json` and `history-originals/<sha256>.original` raw copies after actual independent archive reconciliation. `pm_custodian_history.read_selected_history` uses bounded nonblocking no-follow private FD reads, verifies exact schema, genuine selected root input joins, all named original hashes/lengths, before-export full row digest/config match, expected target/backend/volume, exact purpose/binding and no unresolved prior invocation. It labels this **MANUAL_CUSTODIAN_RECONCILIATION_ORIGINALS_AND_STATE_MATCH**, never automatic rollback-freshness proof or a new authority grant. A root file/string alone does not establish the owning transport or actual manual full reading; D1's real source-pinned private capture/owner path establishes and evidences those prerequisites.

Exact reconciliation fields:

```
schema = cerebro-pm-custodian-history-reconciliation/v1
custodian_ref, invocation_ref
method_ref = OPERATOR-SUPPORT1.3/CURRENT_PM/D1_HUMAN_CYBORG/OWNED_RAILWAY_SSH_PRIVATE_CAPTURE
archive_ref = C:/Cerebro/Authority/PM/history
archive_anchor_sha256
target = {project,environment,service,deployment}
backend_sha256, volume_ref, ledger_sha256, config_sha256
scope_kind = OPERATION | OFFICE | REBIND | INSPECT
binding = exact purpose binding below
unresolved_prior_invocations = []
restore_disposition = NORMAL_SAME_VOLUME_RECONCILED |
 RESTORE_RECONCILED_WITH_INDEPENDENT_ORIGINALS | FIRST_EXCLUSIVE_COMMISSION_CONFIRMED
originals = [{role,sha256,bytes}, ...]
```

Required original roles: `before_ledger_export`, `independent_archive_anchor`, `prior_invocation_effect_closure_index`. The anchor's SHA must match archive_anchor_sha256. Actual full ledger export contents/digest are recomputed, not accepted from its label. Each original <=16MiB, total<=64MiB, record<=1MiB, 3..32 unique roles, content-addressed filenames only. Reader never resolves Windows/private worker paths or repairs permissions.

For OPERATION, binding is `{operation_id,raw_owner_sha256,raw_plan_sha256}`. The existing signed OPERATION nonce is `HISTORY-<exact reconciliation raw SHA256>-<fresh unique suffix>`; this adds no authority/signature field. Both acceptance and execution require the same signed binding. The original before snapshot precedes insertion of this exact ACCEPTED row; only that current still-ACCEPTED operation may be omitted when recomputing the snapshot join. No SPENT/STARTED/COMPLETED or different operation may be omitted. Actual factory still checks genuine current office/signature/time/raw materials and all other immutable rows remain part of the digest. D1 independent marker prevents blind replay after unknown dispatch.

For OFFICE and REBIND, binding is `{decision_sha256}` over exact retained genuine Human decision. For INSPECT it is `{}` and grants no effect. FIRST_EXCLUSIVE_COMMISSION also requires actual empty appointments/operations/checkpoint tables; it cannot relabel an old store. D1 additionally supplies actual new volume/exclusive provision/new Human+holder origin/setup readbacks. Possible restore without genuinely reconciled independent history remains HOLD; own snapshot hash or a valid signature cannot override that boundary.

## Executable versus manual phases

| Phase/API | Actual implementation and proof boundary |
|---|---|
| Installed support | `readiness()` performs actual selected code/mount/config/schema/store readback and existing factory current-office signature/scope/time/revocation check when appointed. No candidate/operation is invented. Missing origin remains explicit until real ceremony; this is not accepted as whole finish. |
| Initial provision | Explicit runtime `provision`, unchanged factory, absent DB only, no auto key/office/acceptance. Actual D1 controlled install/afterread and real P24/Human origin remain required. |
| Whole material | Shared Prepared guards before acceptance and repeated in live consumer; exact signed code/window joined before insertion and before STARTED checkpoint. No backend freshness/authority from pure preflight. |
| STAGE / TRANSFER | Runnable custodian entry, actual accepted signed op, independent history join, real BackendWitness/SQL/native/current-head guards, global spend/epoch fence and independent readback; own witness closure precedes COMPLETE. UNKNOWN remains NO_RETRY. |
| ROLE_ATTACH | Candidate's own existing admitted tool/normal owner route, then actual material return to PM/custodian; journal OWNER_RECORDED, not machine/native proof. TRANSFER independently checks real native birth/role/shadow and exact staged decision, so a manual record cannot authorize transfer. |
| PROFILE | Successor's own actual profile/currentness tool, material returned through existing PM route; manually documented original, no root impersonation/profile probe. |
| FIRST_USE | Actual current PM work and rightful P24/recipient use return; manually documented owner original/use, no synthetic candidate or dummy task. |
| FUNCTION_RETIRE | Existing separately selected Human/P24 function-retirement disposition and actual outgoing owner's return, manually documented. No native retirement claimed from a journal label. |
| HANDOFF | Actual author/owner handoff and recipient reading/continuation through existing homes/route, manually documented original and next owner. |

`record_owner_observation(routine_ref,subject,phase,original_bytes,owner_ref,next_owner,method_ref)` is a private custodian documentation consumer after actual owner return. Its method is fixed `OPERATOR-SUPPORT1.3/CURRENT_PM/EXISTING_OWNER_MATERIAL_RETURN`. It retains the original bytes/hash and records OWNER_RECORDED with `automated_native_or_use_proof=false` and `effect_performed=false`. It performs no lifecycle action or grant. The journal names each manual phase and first unfinished step; final label is JOURNAL_CLOSED_OWNER_RECORDS_RETAINED, not a fabricated operative PASS. Subject still binds candidate/chat/decision hash/Source/method/current predecessor/epoch/target. First unfinished executable phase cannot skip preceding documented owner step and actual effect guards still decide.

## Rebind and restore

Existing explicit PREPARED rebind journal and real next Human APPOINTMENT signature remain; history kindREBIND and exact decision original are required. Config-only rewrite, new empty store or cached old-scope authority are refused. Interrupted migration remains pending and ordinary effects refuse. Explicit recovery rechecks the same old/new signed decision/history/ledger; it is recovery of that migration, not replay of a PM operation. Config and SQLite are not falsely called one atomic FS/SQL write. The driver fences/closes old actual writers and verifies actual normal restart/deployment continuity. No source claim of automatic provider restore detection is made.

## Actual finish and authorization

T0 is the separately recorded actual issuance/driver-start event after receiverREADY, within the already frozen owner/decision/OP finite interval. No generic guard requires not_before==T0. Factory interval requires lower<=now<upper; warm live verification uses the intersection of owner and decision windows; signed OP times must exactly match owner release. Thus READY -> register actual T0 inside that interval -> approve/sign the SAME operation bytes -> bounded authenticated pipe is supported. No time-field finalization, retiming, deadline extension or template substitution. Parent rechecks actual time/remaining validity before Human/key/sign/dispatch and its own <=120s bounds. Late/expired response refuses preserving the attempt. Historical B/observation T0 originals are unchanged.

User's active finish mandate covers supported reviewed source/mount/support installation and afterread; there is no invented blanket final-live microapproval. D1 continues through actual legitimate ports/access after the whole affected package is ready. Only a genuinely new reserved trust/key action or actual refusal needs its real decision route. P24 signing-preparation original296afc4515d6dd79dae54be7b2a557441974c0084045e8cdd4fb040a773f0b1c preserves Human's P24 appointment. Any necessary distinct Human/holder key creation, registration and actual fingerprints are bound within one complete protected commissioning ceremony, never guessed or separately microapproved. Current ordinary support use and P24 final consumption must actually occur; local source/tests are not installed readiness or useful effect.

Source-focused evidence: r3 runs nineteen affected tests (r2 fourteen plus five new reader/manual-phase/actual-office-readiness methods). Full export/reader joins and disposable SQLite are real local data; root/flock/mount/private transport/manual reconciliation and most OS operations are explicitly fixture seams, not live qualification. Ephemeral signatures exercise only changed joins; no actual keys/appointment/operation/provider/PG/native effect or old suite replay. Failed fixture-root setup run is preserved and corrected only by explicit test-only custody, without weakening production checks.
