import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {mkdtemp, writeFile, readFile, rm, mkdir, rmdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import vm from 'node:vm';
import {spawnSync} from 'node:child_process';
const require = createRequire(import.meta.url);
const createFactory = require('./placement-factory.js');
const localJournal = require('./local-journal.cjs');
const sha256 = s => createHash('sha256').update(s).digest('hex');
const api = createFactory({sha256});
const FOLDER = 'application/vnd.google-apps.folder';
const DOC = 'application/vnd.google-apps.document';
const PARENT = 'fixtureParent0001', ABOVE = 'fixtureAbove00001', ROOT = 'fixtureMyRoot001', FILE = 'fixtureOutput001';
const clone = x => JSON.parse(JSON.stringify(x));
const proof = {source_ref: 'synthetic-memory-test-only', revision: '1', durability_evidence_ref: 'NOT_PROVIDER_PROOF', exclusivity_evidence_ref: 'TEST_SERIALIZATION'};
function harness(kind = 'upload', overrides = {}) {
  const mime = kind === 'folder' ? FOLDER : kind === 'import_spreadsheet' ? 'application/vnd.google-apps.spreadsheet' :
    kind === 'import_presentation' ? 'application/vnd.google-apps.presentation' : (kind === 'create' || kind.startsWith('import_')) ? DOC : 'text/plain';
  const source = {schema: 'cerebro-drive-owner-routing-map/v1', task: 'fixtureTask', revision: 1,
    status: 'FIXED_ROUTE_IDS_FRESH_METADATA_QUALIFIED', routes: {RETURN: {parent_id: PARENT}}};
  const raw_json = JSON.stringify(source, null, 2) + '\n';
  const payload = {file_uri: '/PRIVATE/payload', file_name: 'PRIVATE title.txt', title: 'PRIVATE title', name: 'PRIVATE folder', source_file: '/PRIVATE/native.docx', source_id: 'fixtureSource001'};
  const request = {schema: 'cerebro-drive-placement-request/v1', operation_id: 'op1', task: 'fixtureTask', purpose: 'RETURN', kind,
    mime_type: mime, payload_sha256: sha256(api.canonical(payload)), routing: {source_ref: 'fixture-owned-map.json', revision: '1', sha256: sha256(raw_json)},
    content: {source_ref: 'fixture-expected-content', revision: '1', sha256: sha256('expected')},
    destination: {disposition: 'QUALIFIED_OWNER_HOME', fallback: false, allowed_operations: [kind], expected_parent_ids: [ABOVE],
      access_boundary_ref: 'fixture-authorized-task-scope', owner_basis: {source_ref: 'fixture-order', revision: '1'},
      access: {mode: 'provider_capability'}}};
  if (kind === 'update') request.existing = {file_id: FILE, content_update_authorized: true, source_ref: 'fixture-explicit-own-artifact', revision: '1'};
  const parent = {id: PARENT, mimeType: FOLDER, parents: [ABOVE], capabilities: {canAddChildren: true}, trashed: false};
  let record = null, locked = false;
  const events = [], calls = [];
  const state = {output: {id: FILE, mimeType: mime, parents: [PARENT], name: payload.name,
    webViewLink: 'https://drive.google.com/file/d/' + FILE + '/view'}};
  const journal = {qualification: proof, async runExclusive(op, fn) {
    if (locked) throw new Error('HOLD_LOCKED'); locked = true;
    try { return await fn({load: async () => clone(record), commit: async (version, next) => {
      assert.equal(record?.version || 0, version); record = clone(next); events.push('persist:' + record.state);
      return {durable: true, version: next.version, evidence_ref: 'SYNTHETIC:' + next.version}; }}); }
    finally { locked = false; }
  }};
  const tools = {
    supports: () => true,
    async readMetadata(fileId) {
      events.push('read:' + fileId);
      if (fileId === PARENT) return clone(parent);
      if (fileId === 'root') return {id: ROOT, mimeType: FOLDER, parents: []};
      if (fileId === payload.source_id) return {id: fileId, mimeType: mime, parents: [PARENT]};
      return clone(state.output);
    },
    async invoke(name, args) {
      if (!state.externalJournal) assert.ok(record, 'durable intent exists before effect');
      calls.push({name, args: clone(args)}); events.push('tool:' + name);
      if (name === 'update_file' && args.addParents) state.output.parents = [args.addParents];
      return {id: FILE};
    },
    async readContent({fileId}) {
      if (!state.externalJournal) assert.equal(record.file_id, FILE, 'returnedID persisted before content');
      return {file_id: fileId, source_ref: 'fixture-real-reader-callback', observed_sha256: request.content.sha256};
    }
  };
  const input = {request, routingMap: {source_ref: request.routing.source_ref, raw_json}, payload,
    binding: {source_ref: 'fixture-managed-binding', revision: '1', connection: 'oauth_my_drive', transient_staging: 'EXPLICIT_NEW_ID_MY_DRIVE_ROOT_COMPLETE_BEFORE_LINK'}, tools, journal};
  Object.assign(input, overrides);
  return {input, parent, state, calls, events, tools, get record() { return record; }, set record(r) { record = r; }};
}
async function held(code, fn) { await assert.rejects(fn, e => e.code === code || e.message === code); }

test('known upload: direct parent, intent then tool then persisted ID, content+observed link', async () => {
  const h = harness(); const r = await api.execute(h.input);
  assert.equal(r.url, h.state.output.webViewLink); assert.equal(h.calls[0].args.parent_folder_id, PARENT);
  assert.ok(h.events.indexOf('persist:CREATE_INTENT') < h.events.indexOf('tool:upload_file'));
  assert.equal(h.record.state, 'VERIFIED_RELEASED');
  assert.doesNotMatch(JSON.stringify(h.record), /PRIVATE|payload|title/);
});
test('missing/ambiguous route hold before any effect', async () => {
  for (const mode of ['missing', 'ambiguous']) {
    const h = harness(); const map = JSON.parse(h.input.routingMap.raw_json);
    if (mode === 'missing') delete map.routes.RETURN; else map.routes.RETURN = [map.routes.RETURN, map.routes.RETURN];
    h.input.routingMap.raw_json = JSON.stringify(map);
    await held(mode === 'missing' ? 'HOLD_MISSING_ROUTE' : 'HOLD_AMBIGUOUS_ROUTE', () => api.execute(h.input));
    assert.equal(h.calls.length, 0); assert.equal(h.record, null);
  }
});
test('map raw bytes and payload hashes reject drift before mutation', async () => {
  const h = harness(); h.input.routingMap.raw_json += '\n';
  await held('HOLD_ROUTING_BYTES', () => api.execute(h.input)); assert.equal(h.calls.length, 0);
  const b = harness(); b.input.payload.title = 'different';
  await held('HOLD_PAYLOAD_BINDING', () => api.execute(b.input)); assert.equal(b.calls.length, 0);
});
test('task-bound route: explicit other task allowed, old generic ports not allowall', async () => {
  const h = harness(); h.input.request.task = 'newActualTask';
  await held('HOLD_TASK_ROUTE_SCOPE', () => api.execute(h.input));
  const map = JSON.parse(h.input.routingMap.raw_json); map.routes.RETURN.task = 'newActualTask';
  h.input.routingMap.raw_json = JSON.stringify(map); h.input.request.routing.sha256 = sha256(h.input.routingMap.raw_json);
  assert.equal((await api.execute(h.input)).status, 'VERIFIED_RELEASED');
});
test('root/fallback destination cannot become a normal delivery', async () => {
  const h = harness(); h.input.request.destination.fallback = true;
  await held('HOLD_ROUTE_NOT_QUALIFIED', () => api.execute(h.input));
  h.input.request.destination.fallback = false; h.parent.parents = [];
  await held('HOLD_PARENT_BASIS_MISMATCH', () => api.execute(h.input)); assert.equal(h.calls.length, 0);
});
test('normalized connector permission branch does not infer write from sharetrue', async () => {
  const h = harness(); delete h.parent.capabilities; delete h.parent.trashed;
  h.parent.current_user_can_share = true;
  await held('HOLD_CAPABILITY_UNEXPOSED_OR_DENIED', () => api.execute(h.input));
  h.input.request.destination.access = {mode: 'owner_qualified_permission', permission_id: 'fixturePerm00001',
    source_ref: 'fixture-owner-observed-basis', revision: '1', unexposed_capabilities: 'OWNER_SCOPED_NOT_PROVIDER_ATTESTATION',
    unexposed_trashed: 'OWNER_DISPOSITION_REQUIRED_IF_UNKNOWN'};
  h.parent.permissions = [{id: 'fixturePerm00001', type: 'user', role: 'reader'}];
  h.parent.source_visibility_status = 'permission_metadata_available';
  await held('HOLD_OWNER_PERMISSION_DENIED', () => api.execute(h.input));
  h.parent.permissions[0].role = 'owner';
  const r = await api.execute(h.input); assert.match(r.access_qualification, /NOT_PROVIDER_CAPABILITY/);
});
test('metadata alias disagreement holds', async () => {
  const h = harness(); h.parent.mime_type = 'text/plain';
  await held('HOLD_METADATA_ALIAS_CONFLICT', () => api.execute(h.input)); assert.equal(h.calls.length, 0);
});
test('unknown create never retries and needs exact receipt to recover knownID', async () => {
  const h = harness(); const original = h.tools.invoke;
  h.tools.invoke = async (...a) => { await original(...a); throw new Error('unknown provider outcome PRIVATE'); };
  await held('HOLD_UNKNOWN_CREATE_RECONCILE_EXACT_RECEIPT_NO_RETRY', () => api.execute(h.input));
  await held('HOLD_UNKNOWN_CREATE_RECONCILE_EXACT_RECEIPT_NO_RETRY', () => api.execute(h.input));
  assert.equal(h.calls.length, 1);
  const evidence = {operation_id: 'op1', file_id: FILE, source_ref: 'fixture-positive-provider-result', revision: '1', intent_hash: h.record.intent_hash};
  await held('HOLD_RECOVERY_NOT_EXACT', () => api.recoverKnownCreate({operationId: 'op1', evidence, journal: h.input.journal, verifyReceipt: async () => false}));
  await api.recoverKnownCreate({operationId: 'op1', evidence, journal: h.input.journal, verifyReceipt: async () => true});
  assert.equal((await api.execute(h.input)).file_id, FILE); assert.equal(h.calls.length, 1);
});
test('knownID survives interrupted readback and does not recreate', async () => {
  const h = harness(); h.tools.readContent = async () => { throw new Error('reader interrupted'); };
  await assert.rejects(() => api.execute(h.input)); assert.equal(h.record.file_id, FILE);
  h.tools.readContent = async () => ({file_id: FILE, source_ref: 'readback', observed_sha256: h.input.request.content.sha256});
  assert.equal((await api.execute(h.input)).file_id, FILE); assert.equal(h.calls.length, 1);
});
test('import/create OAuth omit parentarg and complete same newID root placement', async () => {
  for (const kind of ['import_document', 'import_spreadsheet', 'import_presentation', 'create']) {
    const h = harness(kind); h.state.output.parents = [ROOT];
    const r = await api.execute(h.input);
    assert.equal('parent_folder_id' in h.calls[0].args, false);
    assert.equal(h.calls[1].name, 'update_file'); assert.equal(h.calls[1].args.fileId, FILE);
    assert.equal(h.calls[1].args.removeParents, ROOT); assert.equal(r.parent_id, PARENT);
    assert.ok(h.events.indexOf('persist:KNOWN_ID') < h.events.indexOf('tool:update_file'));
  }
});
test('unapproved transient or unexpected nonroot parent produces no move', async () => {
  const h = harness('create'); delete h.input.binding.transient_staging;
  await held('HOLD_TRANSIENT_STAGING_NOT_AUTHORIZED', () => api.execute(h.input)); assert.equal(h.calls.length, 0);
  const b = harness('import_document'); b.state.output.parents = ['fixtureUnrelated001'];
  await held('HOLD_TRANSIENT_PARENT_NOT_OBSERVED_MY_DRIVE_ROOT', () => api.execute(b.input)); assert.equal(b.calls.length, 1);
});
test('unknown placement reads sameID and never blindly retries move', async () => {
  const h = harness('create'); h.state.output.parents = [ROOT];
  const invoke = h.tools.invoke;
  h.tools.invoke = async (tool, args) => { if (tool === 'update_file') { h.calls.push({name: tool, args}); throw new Error('unknown'); } return invoke(tool, args); };
  await held('HOLD_UNKNOWN_PLACEMENT_RECONCILE_SAME_ID', () => api.execute(h.input));
  await held('HOLD_UNKNOWN_PLACEMENT_RECONCILE_SAME_ID', () => api.execute(h.input));
  assert.equal(h.calls.length, 2);
  h.state.output.parents = [PARENT]; assert.equal((await api.execute(h.input)).file_id, FILE); assert.equal(h.calls.length, 2);
});
test('success reply with stale/wrong parent readback cannot trigger a second placement', async () => {
  const h = harness('create'); h.state.output.parents = [ROOT];
  const invoke = h.tools.invoke;
  h.tools.invoke = async (tool, args) => {
    if (tool === 'update_file') { h.calls.push({name: tool, args}); return {id: FILE}; }
    return invoke(tool, args);
  };
  await held('HOLD_PARENT_READBACK', () => api.execute(h.input));
  await held('HOLD_UNKNOWN_PLACEMENT_RECONCILE_SAME_ID', () => api.execute(h.input));
  assert.equal(h.calls.length, 2);
});
test('folder direct selected parent, name/type readback; interruption reuses sameID', async () => {
  const h = harness('folder'); h.state.output.name = 'wrong';
  await held('HOLD_FOLDER_NAME_READBACK', () => api.execute(h.input));
  h.state.output.name = h.input.payload.name;
  assert.equal((await api.execute(h.input)).file_id, FILE); assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].args.parent_folder, PARENT);
});
test('copy uses selected parent and exact source before effect', async () => {
  const h = harness('copy'); await api.execute(h.input);
  assert.equal(h.calls[0].args.parent_folder, PARENT); assert.equal(h.calls[0].name, 'copy_file');
});
test('sameID raw update unknown outcome only readbacks; wrong-parent existing file not moved', async () => {
  const h = harness('update'); const invoke = h.tools.invoke;
  h.tools.invoke = async (...args) => { await invoke(...args); throw new Error('unknown'); };
  await held('HOLD_UNKNOWN_UPDATE_RECONCILE_SAME_ID', () => api.execute(h.input));
  assert.equal((await api.execute(h.input)).file_id, FILE); assert.equal(h.calls.length, 1);
  const b = harness('update'); b.state.output.parents = [ROOT];
  await held('HOLD_EXISTING_UPDATE_SCOPE', () => api.execute(b.input)); assert.equal(b.calls.length, 0);
});
test('wrong content/MIME/final observedURL never release', async () => {
  for (const issue of ['mime', 'content', 'url']) {
    const h = harness();
    if (issue === 'mime') h.state.output.mimeType = DOC;
    if (issue === 'content') h.tools.readContent = async () => ({file_id: FILE, source_ref: 'reader', observed_sha256: sha256('wrong')});
    if (issue === 'url') h.state.output.webViewLink = 'https://drive.google.com/file/d/anotherFile001/view';
    await assert.rejects(() => api.execute(h.input)); assert.notEqual(h.record.state, 'VERIFIED_RELEASED');
  }
});
test('commit boolean without proof/readback cannot permit create', async () => {
  const h = harness(); h.input.journal.runExclusive = async (_, f) => f({load: async () => null,
    commit: async (_, r) => ({durable: true, version: r.version, evidence_ref: 'dishonest-memory-ack'})});
  await held('HOLD_JOURNAL_READBACK_MISMATCH', () => api.execute(h.input)); assert.equal(h.calls.length, 0);
});
test('ID persistence failure after successful creation stops and cannot recreate', async () => {
  const h = harness(); h.state.externalJournal = true; let saved = null;
  h.input.journal.runExclusive = async (_, f) => f({load: async () => clone(saved), commit: async (v, row) => {
    if (row.file_id) throw new Error('journal unavailable after returned ID');
    saved = clone(row); return {version: row.version, durable: true, evidence_ref: 'fixture-intent'};
  }});
  await assert.rejects(() => api.execute(h.input)); assert.equal(h.calls.length, 1);
  await held('HOLD_UNKNOWN_CREATE_RECONCILE_EXACT_RECEIPT_NO_RETRY', () => api.execute(h.input)); assert.equal(h.calls.length, 1);
});
test('unsupported managed callback is rejected before intent/effect', async () => {
  const h = harness(); h.tools.supports = () => false;
  await held('HOLD_TOOL_OR_CONTENT_PORT_UNSUPPORTED', () => api.execute(h.input));
  assert.equal(h.record, null); assert.equal(h.calls.length, 0);
});
test('portable IIFE loads without require/Node imports and exposes same factory API', async () => {
  const code = await readFile(new URL('./placement-factory.js', import.meta.url), 'utf8');
  const sandbox = vm.createContext({}); vm.runInContext(code, sandbox);
  assert.equal(typeof sandbox.CerebroDrivePlacement, 'function');
  assert.equal(typeof sandbox.CerebroDrivePlacement({sha256}).managedConnector, 'function');
});
test('managed wrapper calls real descriptor shapes and injectable decoder', async () => {
  const calls = []; const tools = api.managedConnector({google_drive_get_file_metadata: async a => { calls.push(a); return {structuredContent: {id: PARENT}}; },
    google_drive_create_folder: async a => ({structuredContent: a})}, {decode: r => r.structuredContent});
  assert.equal((await tools.readMetadata(PARENT)).id, PARENT);
  assert.equal(calls[0].fileId, PARENT); assert.match(calls[0].fields, /capabilities/);
  assert.deepEqual(await tools.invoke('create_folder', {parent_folder: PARENT}), {parent_folder: PARENT});
});
test('local WAL fsync/readback survives new journal instance, refuses locks/partial tails', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'placement-risk-'));
  try {
    await writeFile(join(dir, 'placement-journal.jsonl'), '');
    const h = harness(); h.state.externalJournal = true; h.input.journal = localJournal(dir);
    const invoke = h.tools.invoke;
    h.tools.invoke = async (...args) => { assert.match(await readFile(join(dir, 'placement-journal.jsonl'), 'utf8'), /CREATE_INTENT/); return invoke(...args); };
    await api.execute(h.input);
    const r = harness(); r.state.externalJournal = true; r.input.journal = localJournal(dir); await api.execute(r.input); assert.equal(r.calls.length, 0);
    await mkdir(join(dir, 'placement.lock'));
    await held('HOLD_JOURNAL_LOCKED_OR_UNAVAILABLE', () => api.execute(r.input));
    await rmdir(join(dir, 'placement.lock'));
    await writeFile(join(dir, 'placement-journal.jsonl'), '{partial', {flag: 'a'});
    await held('HOLD_PARTIAL_JOURNAL_RECONCILE_NO_TRUNCATION', () => api.execute(r.input));
  } finally { await rm(dir, {recursive: true}); }
});

test('nativeDoc CAS journal: actual required revision, intent+ID persisted and readback, lock released', async () => {
  const docJournal = require('./doc-journal-factory.js');
  let revision = 1, body = {schema: 'cerebro-drive-doc-journal/v1', version: 1, lock: null, operations: {}};
  const writes = [];
  const ports = {qualification: proof, newLockToken: async () => 'fixtureUniqueLockToken0001',
    read: async () => ({revisionId: 'rev' + revision, text: JSON.stringify(body)}),
    write: async ({requiredRevisionId, text}) => { assert.equal(requiredRevisionId, 'rev' + revision); body = JSON.parse(text); revision++; writes.push(clone(body)); }};
  const h = harness(); h.state.externalJournal = true; h.input.journal = docJournal(ports);
  const invoke = h.tools.invoke;
  h.tools.invoke = async (...args) => { assert.equal(body.operations.op1.state, 'CREATE_INTENT'); return invoke(...args); };
  await api.execute(h.input);
  assert.equal(body.lock, null); assert.equal(body.operations.op1.state, 'VERIFIED_RELEASED');
  assert.ok(writes.some(b => b.operations.op1?.state === 'CREATE_INTENT'));
  const r = harness(); r.state.externalJournal = true; r.input.journal = docJournal(ports); await api.execute(r.input); assert.equal(r.calls.length, 0);
});
test('nativeDoc CAS conflict/unknown acquire/locked doc produces no provider creation', async () => {
  const docJournal = require('./doc-journal-factory.js');
  for (const mode of ['conflict', 'unknown', 'locked']) {
    let revision = 1, body = {schema: 'cerebro-drive-doc-journal/v1', version: 1, lock: mode === 'locked' ? {token: 'other', operation_id: 'otherOp'} : null, operations: {}};
    const h = harness();
    h.input.journal = docJournal({qualification: proof, newLockToken: async () => 'fixtureUniqueLockToken0002',
      read: async () => ({revisionId: 'rev' + revision, text: JSON.stringify(body)}),
      write: async ({text}) => { if (mode === 'unknown') { body = JSON.parse(text); revision++; } throw new Error('provider conflict/unknown'); }});
    await assert.rejects(() => api.execute(h.input)); assert.equal(h.calls.length, 0);
    if (mode === 'unknown') assert.notEqual(body.lock, null, 'unknown acquisition is not automatically cleared or stolen');
  }
});

test('REFINE-01 F1: explicit deny or malformed capability cannot use owner reserve; actual omission remains qualified', async () => {
  for (const value of [false, 'false']) {
    const h = harness('folder');
    h.input.request.destination.access = {mode: 'owner_qualified_permission', permission_id: 'fixturePerm00001',
      source_ref: 'fixture-owner-scope', revision: '1', unexposed_capabilities: 'OWNER_SCOPED_NOT_PROVIDER_ATTESTATION',
      unexposed_trashed: 'OWNER_DISPOSITION_REQUIRED_IF_UNKNOWN'};
    h.parent.permissions = [{id: 'fixturePerm00001', type: 'user', role: 'owner'}];
    h.parent.source_visibility_status = 'permission_metadata_available';
    h.parent.capabilities.canAddChildren = value;
    await assert.rejects(() => api.execute(h.input));
    assert.equal(h.calls.length, 0); assert.equal(h.record, null);
    delete h.parent.capabilities; delete h.parent.trashed;
    assert.equal((await api.execute(h.input)).status, 'VERIFIED_RELEASED');
    assert.equal(h.calls.length, 1, 'legitimate omitted-field reserve preserved');
  }
});
test('REFINE-01 F2: terminal native release cannot acquire new placement rights after parent drift', async () => {
  for (const kind of ['create', 'import_document']) {
    const h = harness(kind); h.state.output.parents = [ROOT];
    await api.execute(h.input);
    assert.equal(h.calls.length, 2); assert.equal(h.record.state, 'VERIFIED_RELEASED');
    const terminal = clone(h.record);
    h.state.output.parents = [ROOT];
    await held('HOLD_RELEASED_ARTIFACT_DRIFT_OWNER_RECONCILE', () => api.execute(h.input));
    assert.equal(h.calls.length, 2); assert.deepEqual(h.record, terminal);
    h.state.output.parents = [PARENT];
    const again = await api.execute(h.input);
    assert.equal(again.status, 'VERIFIED_RELEASED');
    assert.equal(h.calls.length, 2); assert.deepEqual(h.record, terminal, 'read-only terminal reuse must not rewrite release record');
  }
});
test('REFINE-01 F2: unfinished new-ID staging still resumes, uncertain create/placement never repeats', async () => {
  const h = harness('create'); h.state.output.parents = [ROOT];
  const read = h.tools.readMetadata; let interrupted = true;
  h.tools.readMetadata = async fileId => { if (fileId === FILE && interrupted) { interrupted = false; throw new Error('interrupted after persisted ID'); } return read(fileId); };
  await assert.rejects(() => api.execute(h.input));
  assert.equal(h.record.state, 'KNOWN_ID'); assert.equal(h.calls.length, 1);
  await api.execute(h.input); assert.equal(h.calls.length, 2);
  const noId = harness('create'); noId.tools.invoke = async () => { throw new Error('unknown'); };
  await held('HOLD_UNKNOWN_CREATE_RECONCILE_EXACT_RECEIPT_NO_RETRY', () => api.execute(noId.input));
  await held('HOLD_UNKNOWN_CREATE_RECONCILE_EXACT_RECEIPT_NO_RETRY', () => api.execute(noId.input));
  const uncertain = harness('create'); uncertain.state.output.parents = [ROOT];
  const invoke = uncertain.tools.invoke;
  uncertain.tools.invoke = async (tool, args) => { if (tool === 'update_file') { uncertain.calls.push({name: tool, args}); throw new Error('unknown move'); } return invoke(tool, args); };
  await held('HOLD_UNKNOWN_PLACEMENT_RECONCILE_SAME_ID', () => api.execute(uncertain.input));
  await held('HOLD_UNKNOWN_PLACEMENT_RECONCILE_SAME_ID', () => api.execute(uncertain.input));
  assert.equal(uncertain.calls.length, 2);
});
test('REFINE-01 F2: terminal artifact observation retains qualified native-journal housekeeping only', async () => {
  const docJournal = require('./doc-journal-factory.js');
  let revision = 1, writes = 0;
  let body = {schema: 'cerebro-drive-doc-journal/v1', version: 1, lock: null, operations: {}};
  const ports = {qualification: proof, newLockToken: async () => 'fixtureTerminalJournalToken01',
    read: async () => ({revisionId: 'rev' + revision, text: JSON.stringify(body)}),
    write: async ({requiredRevisionId, text}) => { assert.equal(requiredRevisionId, 'rev' + revision); body = JSON.parse(text); revision++; writes++; }};
  const h = harness('folder'); h.state.externalJournal = true; h.input.journal = docJournal(ports);
  await api.execute(h.input); const terminal = clone(body.operations.op1); const before = writes;
  await api.execute(h.input);
  assert.equal(h.calls.length, 1, 'no artifact mutation on terminal observation');
  assert.equal(writes - before, 2, 'qualified journal lock and release are separate housekeeping');
  assert.deepEqual(body.operations.op1, terminal, 'no gratuitous terminal operation save');
  h.state.output.parents = [ROOT]; const prior = writes;
  await held('HOLD_RELEASED_ARTIFACT_DRIFT_OWNER_RECONCILE', () => api.execute(h.input));
  assert.equal(h.calls.length, 1); assert.equal(writes - prior, 2); assert.deepEqual(body.operations.op1, terminal);
});
function actualNormalizedShape(h) {
  delete h.parent.mimeType; delete h.parent.parents; delete h.parent.capabilities; delete h.parent.trashed;
  Object.assign(h.parent, {title: 'Synthetic target', mime_type: FOLDER, parent_ids: [ABOVE],
    source_visibility_status: 'permission_metadata_available', current_user_can_share: true,
    permissions: [{type: 'user', role: 'owner', emailAddress: 'synthetic-owner@example.invalid', domain: '',
      displayName: 'Synthetic anonymous fixture', allowFileDiscovery: false}]});
  h.input.request.destination.access = {mode: 'owner_qualified_permission', source_ref: 'synthetic-anonymous-actual-shape', revision: '1',
    principal_selector: {kind: 'observed_user_email', value: 'synthetic-owner@example.invalid', source_ref: 'synthetic-private-observed-value', revision: '1'},
    unexposed_capabilities: 'OWNER_SCOPED_NOT_PROVIDER_ATTESTATION', unexposed_trashed: 'OWNER_DISPOSITION_REQUIRED_IF_UNKNOWN'};
  return h;
}
function rootNull(h, rootValue = {id: ROOT, mime_type: FOLDER, parent_ids: null}) {
  const read = h.tools.readMetadata;
  h.tools.readMetadata = async fileId => fileId === 'root' ? clone(rootValue) : read(fileId);
  h.input.binding.root_lookup = {lookup: 'literal_root', expected_id: ROOT,
    source_ref: 'synthetic-literal-root-observation-and-owner-chain', revision: '1',
    null_parent_disposition: 'ACTUAL_LITERAL_ROOT_NORMALIZER_OMITS_PARENTS'};
  return h;
}
test('NORMALIZER-02 principal: exact anonymous normalized shape qualifies privately without fabricated permissionID', async () => {
  const h = actualNormalizedShape(harness('folder'));
  const r = await api.execute(h.input); assert.equal(r.status, 'VERIFIED_RELEASED');
  assert.match(r.access_qualification, /NOT_PROVIDER_CAPABILITY/);
  assert.doesNotMatch(JSON.stringify(h.record), /synthetic-owner@|principal_selector|emailAddress|displayName/);
  assert.doesNotMatch(JSON.stringify(r), /synthetic-owner@|principal_selector|emailAddress/);
  const originalId = harness('folder');
  originalId.input.request.destination.access = {mode: 'owner_qualified_permission', permission_id: 'fixturePerm00001',
    source_ref: 'synthetic-id-owner-basis', revision: '1', unexposed_capabilities: 'OWNER_SCOPED_NOT_PROVIDER_ATTESTATION',
    unexposed_trashed: 'OWNER_DISPOSITION_REQUIRED_IF_UNKNOWN'};
  originalId.parent.permissions = [{id: 'fixturePerm00001', type: 'user', role: 'owner'}];
  originalId.parent.source_visibility_status = 'permission_metadata_available';
  assert.equal((await api.execute(originalId.input)).status, 'VERIFIED_RELEASED', 'actual-ID selector remains supported');
});
test('NORMALIZER-02 principal: missing/duplicate/denied/malformed/conflicting selectors hold before intent or artifact', async () => {
  for (const variant of ['missing-selector', 'missing-row', 'duplicate', 'denied', 'deleted', 'malformed', 'wrong-kind', 'missing-provenance', 'both-selectors', 'row-id-exposed', 'alias-conflict']) {
    const h = actualNormalizedShape(harness('folder')); const a = h.input.request.destination.access;
    if (variant === 'missing-selector') delete a.principal_selector;
    if (variant === 'missing-row') h.parent.permissions = [];
    if (variant === 'duplicate') h.parent.permissions.push(clone(h.parent.permissions[0]));
    if (variant === 'denied') h.parent.permissions[0].role = 'reader';
    if (variant === 'deleted') h.parent.permissions[0].deleted = true;
    if (variant === 'malformed') a.principal_selector.value = 'not an email';
    if (variant === 'wrong-kind') a.principal_selector.kind = 'display_name';
    if (variant === 'missing-provenance') delete a.principal_selector.source_ref;
    if (variant === 'both-selectors') a.permission_id = 'fixturePerm00001';
    if (variant === 'row-id-exposed') h.parent.permissions[0].id = 'fixturePerm00001';
    if (variant === 'alias-conflict') h.parent.permissions[0].email_address = 'other@example.invalid';
    await assert.rejects(() => api.execute(h.input), undefined, variant);
    assert.equal(h.calls.length, 0, variant); assert.equal(h.record, null, variant);
  }
});
test('NORMALIZER-02 principal: explicit known deny still wins over new observed-email reserve', async () => {
  const h = actualNormalizedShape(harness('folder')); h.parent.capabilities = {canAddChildren: false};
  await held('HOLD_CAPABILITY_EXPLICIT_DENY', () => api.execute(h.input));
  assert.equal(h.calls.length, 0); assert.equal(h.record, null);
});
test('NORMALIZER-02 root: bound normalized-null preflights before nativecreate/import and completes same newID', async () => {
  for (const kind of ['create', 'import_document', 'import_spreadsheet', 'import_presentation']) {
    const h = rootNull(actualNormalizedShape(harness(kind))); h.state.output.parents = [ROOT];
    const read = h.tools.readMetadata; let rootReads = 0;
    h.tools.readMetadata = async id => { if (id === 'root') rootReads++; return read(id); };
    const invoke = h.tools.invoke;
    h.tools.invoke = async (tool, args) => { if (tool !== 'update_file') assert.ok(rootReads >= 1, 'root qualified BEFORE first new artifact'); return invoke(tool, args); };
    const r = await api.execute(h.input);
    assert.equal(r.file_id, FILE); assert.equal(h.calls.length, 2); assert.ok(rootReads >= 2);
    assert.equal(h.record.root_lookup_observation.id, ROOT);
    assert.match(h.record.root_lookup_observation.parent_evidence, /NORMALIZED_NULL.*NOT_OBSERVED_EMPTY/);
  }
});
test('NORMALIZER-02 root: unbound/mismatch/nonempty/aliasconflict/wrongMIME/null-target reject BEFORE nativecreate', async () => {
  for (const variant of ['unbound', 'wrong-id', 'nonempty', 'alias-conflict', 'wrong-mime', 'wrong-lookup', 'missing-proof', 'ordinary-null']) {
    const value = {id: ROOT, mime_type: FOLDER, parent_ids: null};
    if (variant === 'nonempty') value.parent_ids = [ABOVE];
    if (variant === 'alias-conflict') value.parents = [];
    if (variant === 'wrong-mime') value.mime_type = DOC;
    const h = rootNull(actualNormalizedShape(harness('create')), value);
    if (variant === 'unbound') delete h.input.binding.root_lookup;
    if (variant === 'wrong-id') h.input.binding.root_lookup.expected_id = 'differentRoot001';
    if (variant === 'wrong-lookup') h.input.binding.root_lookup.lookup = 'folder_by_name';
    if (variant === 'missing-proof') delete h.input.binding.root_lookup.source_ref;
    if (variant === 'ordinary-null') h.parent.parent_ids = null;
    await assert.rejects(() => api.execute(h.input), undefined, variant);
    assert.equal(h.calls.length, 0, variant); assert.equal(h.record, null, variant);
  }
  assert.throws(() => api.metadata({id: ROOT, mime_type: FOLDER, parent_ids: null}), /HOLD_PARENT_METADATA_UNKNOWN/);
});
test('NORMALIZER-02 root: actual empty-parent branch and direct destinations remain, later root drift stops placement', async () => {
  const empty = harness('create'); empty.state.output.parents = [ROOT];
  await api.execute(empty.input); assert.equal(empty.calls.length, 2);
  assert.equal(empty.record.root_lookup_observation.parent_evidence, 'ACTUAL_OBSERVED_EMPTY_PARENT_LIST');
  const direct = actualNormalizedShape(harness('folder')); const read = direct.tools.readMetadata;
  direct.tools.readMetadata = async id => { assert.notEqual(id, 'root', 'direct destination does not need root branch'); return read(id); };
  await api.execute(direct.input); assert.equal(direct.calls.length, 1);
  const h = rootNull(actualNormalizedShape(harness('create'))); h.state.output.parents = [ROOT];
  const priorRead = h.tools.readMetadata; let reads = 0;
  h.tools.readMetadata = async id => id === 'root' && ++reads > 1 ? {id: 'differentRoot001', mime_type: FOLDER, parent_ids: null} : priorRead(id);
  await held('HOLD_ROOT_EXPECTED_ID_MISMATCH', () => api.execute(h.input));
  assert.equal(h.calls.length, 1); assert.equal(h.record.file_id, FILE, 'known ID retained; no repeated create or wrong-root move');
});
test('NORMALIZER-02 CLI prepare never emits private principal selector to stdout', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'normalizer-cli-'));
  try {
    const h = actualNormalizedShape(harness('folder'));
    for (const [key, value] of Object.entries({request: h.input.request, payload: h.input.payload, parent: h.parent}))
      await writeFile(join(dir, key + '.json'), JSON.stringify(value));
    await writeFile(join(dir, 'map.json'), h.input.routingMap.raw_json);
    await writeFile(join(dir, 'bindings.mjs'), 'export const binding=' + JSON.stringify(h.input.binding) + ';');
    const r = spawnSync(process.execPath, [new URL('./placement-cli.cjs', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'), 'prepare',
      '--request', join(dir, 'request.json'), '--payload', join(dir, 'payload.json'), '--routing', join(dir, 'map.json'),
      '--bindings', join(dir, 'bindings.mjs'), '--parent-metadata', join(dir, 'parent.json')], {encoding: 'utf8'});
    assert.equal(r.status, 0, r.stdout + r.stderr);
    assert.doesNotMatch(r.stdout, /synthetic-owner@|principal_selector|emailAddress|PRIVATE/);
  } finally { await rm(dir, {recursive: true}); }
});
async function observedLinkWithoutURL(value, kind = 'folder') {
  const h = harness(kind); h.state.output.webViewLink = value;
  const source = await readFile(new URL('./placement-factory.js', import.meta.url), 'utf8');
  const data = {request: h.input.request, payload: h.input.payload, routingMap: h.input.routingMap,
    binding: h.input.binding, parent: h.parent, output: h.state.output, fileId: FILE, parentId: PARENT, proof};
  const context = vm.createContext({sha256, DATA_JSON: JSON.stringify(data)});
  assert.equal(vm.runInContext('typeof URL', context), 'undefined');
  vm.runInContext(source, context);
  // Data, callbacks and journal records are created in the V8 realm itself;
  // only the qualified hash callback returns a primitive across that boundary.
  const raw = await vm.runInContext(`(async () => {
    const d=JSON.parse(DATA_JSON), api=CerebroDrivePlacement({sha256});
    const p=await api.prepare({...d,parentMetadata:d.parent});
    let record={schema:'cerebro-drive-placement-journal/v1',version:2,intent_hash:p.intentHash,
      operation_id:d.request.operation_id,task:d.request.task,purpose:d.request.purpose,kind:d.request.kind,
      parent_id:d.parentId,mime_type:d.request.mime_type,routing:d.request.routing,
      state:'KNOWN_ID',file_id:d.fileId,created_by_operation:true};
    let mutations=0,saves=0;
    const journal={qualification:d.proof,runExclusive:async(_,fn)=>fn({
      load:async()=>JSON.parse(JSON.stringify(record)),
      commit:async(version,next)=>{if(version!==record.version)throw Error('conflict');record=next;saves++;
        return {durable:true,version:next.version,evidence_ref:'synthetic-vm-commit'};}})};
    const tools={supports:()=>true,readMetadata:async id=>id===d.parentId?d.parent:d.output,
      invoke:async()=>{mutations++;throw Error('no new artifact is allowed');},
      readContent:async()=>({file_id:d.fileId,source_ref:'synthetic-content-readback',observed_sha256:d.request.content.sha256})};
    let result;
    try { result=await api.execute({...d,journal,tools}); }
    catch(e){result={status:e.code||e.message};}
    return JSON.stringify({result,mutations,saves,record,URL_type:typeof URL});
  })()`, context);
  return JSON.parse(raw);
}
test('ACTUAL-V8-URL: absent global releases observed folder/document/raw links from knownID without new create', async () => {
  for (const [url, kind] of [
    ['https://drive.google.com/drive/folders/' + FILE, 'folder'],
    ['https://docs.google.com/document/d/' + FILE + '/edit?tab=t.0', 'create'],
    ['https://drive.google.com/file/d/' + FILE + '/view?usp=drivesdk', 'upload'],
    ['https://docs.google.com/spreadsheets/d/' + FILE + '/edit#gid=0', 'create'],
    ['https://drive.google.com/drive/u/0/folders/' + FILE + '/', 'folder']]) {
    const r = await observedLinkWithoutURL(url, kind);
    assert.equal(r.URL_type, 'undefined'); assert.equal(r.result.status, 'VERIFIED_RELEASED', url);
    assert.equal(r.result.url, url, 'only exact observed URL released');
    assert.equal(r.mutations, 0); assert.equal(r.record.file_id, FILE); assert.equal(r.saves, 1);
  }
});
test('ACTUAL-V8-URL: absent global retains strict malformed/authority/path-ID negatives and knownID', async () => {
  const valid = 'https://drive.google.com/drive/folders/' + FILE;
  for (const value of [null, '', {}, 'http://drive.google.com/drive/folders/' + FILE,
    'https://user@drive.google.com/drive/folders/' + FILE,
    'https://user:synthetic-password@drive.google.com/drive/folders/' + FILE,
    'https://drive.google.com:443/drive/folders/' + FILE,
    'https://drive.google.com.evil.invalid/drive/folders/' + FILE,
    'https://drive.google.com./drive/folders/' + FILE,
    'https://drive.google.com\\@evil.invalid/drive/folders/' + FILE,
    'https://evil.invalid/drive.google.com/' + FILE,
    'https://drive.google.com/drive/folders/otherFile00001',
    valid + 'suffix', 'https://drive.google.com/open?id=' + FILE,
    'https://drive.google.com/drive/folders/%2F' + FILE,
    valid + '/../otherFile00001', valid + '\n', ' ' + valid,
    valid + '?malformed=%GG', 'https://drive.google.com//drive/folders/' + FILE]) {
    const r = await observedLinkWithoutURL(value);
    assert.notEqual(r.result.status, 'VERIFIED_RELEASED', String(value));
    assert.equal(r.mutations, 0); assert.equal(r.saves, 0);
    assert.equal(r.record.state, 'KNOWN_ID'); assert.equal(r.record.file_id, FILE);
  }
});
test('CLI prepare uses same contract and does not print private title/path or call tools', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'placement-cli-'));
  try {
    const h = harness();
    const files = {request: h.input.request, payload: h.input.payload, parent: h.parent};
    for (const [name, v] of Object.entries(files)) await writeFile(join(dir, name + '.json'), JSON.stringify(v));
    await writeFile(join(dir, 'map.json'), h.input.routingMap.raw_json);
    await writeFile(join(dir, 'bindings.mjs'), 'export const binding=' + JSON.stringify(h.input.binding) + ';');
    const r = spawnSync(process.execPath, [new URL('./placement-cli.cjs', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'), 'prepare',
      '--request', join(dir, 'request.json'), '--payload', join(dir, 'payload.json'), '--routing', join(dir, 'map.json'),
      '--bindings', join(dir, 'bindings.mjs'), '--parent-metadata', join(dir, 'parent.json')], {encoding: 'utf8'});
    assert.equal(r.status, 0, r.stderr + r.stdout); assert.equal(JSON.parse(r.stdout).status, 'PLAN_ONLY'); assert.doesNotMatch(r.stdout, /PRIVATE/);
  } finally { await rm(dir, {recursive: true}); }
});
