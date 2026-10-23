'use strict';
// No IO, imports, credentials, timers or provider calls at load time. The same
// factory is CommonJS-loadable or explicitly evaluable in a functions isolate.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else root.CerebroDrivePlacement = factory;
})(globalThis, function createDrivePlacement({sha256}) {
  const FOLDER = 'application/vnd.google-apps.folder';
  const NATIVE = ['application/vnd.google-apps.document', 'application/vnd.google-apps.spreadsheet', 'application/vnd.google-apps.presentation'];
  const KINDS = ['upload', 'create', 'import_document', 'import_spreadsheet', 'import_presentation', 'copy', 'folder', 'update'];
  class Hold extends Error {
    constructor(code, fileId = null) { super(code); this.name = 'PlacementHold'; this.code = code; this.fileId = fileId; }
  }
  const fail = (code, id = null) => { throw new Hold(code, id); };
  function text(v) { return typeof v === 'string' && v.trim().length > 0; }
  function id(v) { if (!text(v) || !/^[A-Za-z0-9_-]{10,}$/.test(v) || v === 'root') fail('HOLD_INVALID_ID'); return v; }
  function hash(v) { if (typeof v !== 'string' || !/^[0-9a-f]{64}$/.test(v)) fail('HOLD_INVALID_HASH'); return v; }
  function ref(v) { if (!v || !text(v.source_ref) || !text(v.revision)) fail('HOLD_MISSING_PROVENANCE'); }
  function canonical(v) {
    if (v === null || typeof v === 'string' || typeof v === 'boolean') return JSON.stringify(v);
    if (typeof v === 'number' && Number.isFinite(v)) return JSON.stringify(v);
    if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
    if (v && typeof v === 'object' && Object.getPrototypeOf(v) === Object.prototype)
      return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
    fail('HOLD_NON_JSON_INPUT');
  }
  const same = (a, b) => canonical(a) === canonical(b);
  const sorted = a => Array.isArray(a) && a.every(x => typeof x === 'string') ? [...a].sort() : fail('HOLD_PARENT_METADATA_UNKNOWN');
  async function digest(v) {
    if (typeof sha256 !== 'function') fail('HOLD_HASH_PORT_UNQUALIFIED');
    return hash(await sha256(canonical(v)));
  }
  function alias(m, a, b) {
    if (a in m && b in m && !same(m[a], m[b])) fail('HOLD_METADATA_ALIAS_CONFLICT');
    return a in m ? m[a] : m[b];
  }
  function metadata(m) {
    if (!m || typeof m !== 'object') fail('HOLD_METADATA_UNKNOWN');
    const normalized = {id: id(m.id), mime: alias(m, 'mimeType', 'mime_type'),
      parents: sorted(alias(m, 'parents', 'parent_ids')), url: alias(m, 'webViewLink', 'url'),
      name: alias(m, 'name', 'title'), permissions: m.permissions, capabilities: m.capabilities,
      trashed: m.trashed, visibility: m.source_visibility_status};
    if (!text(normalized.mime)) fail('HOLD_MIME_METADATA_UNKNOWN');
    if (normalized.trashed === true) fail('HOLD_TRASHED');
    return normalized;
  }
  function resolve(request, map) {
    if (!request || request.schema !== 'cerebro-drive-placement-request/v1' || !text(request.operation_id) ||
        !/^[A-Za-z0-9_-]{1,120}$/.test(request.operation_id) || !text(request.task) || !text(request.purpose) ||
        !KINDS.includes(request.kind) || !text(request.mime_type)) fail('HOLD_INVALID_REQUEST');
    ref(request.routing); hash(request.routing.sha256); hash(request.payload_sha256);
    if (!map || !text(map.raw_json) || map.source_ref !== request.routing.source_ref) fail('HOLD_ROUTING_MAP_UNKNOWN');
    let source;
    try { source = JSON.parse(map.raw_json); } catch (_) { fail('HOLD_ROUTING_MAP_UNKNOWN'); }
    if (source.schema !== 'cerebro-drive-owner-routing-map/v1' ||
        String(source.revision) !== request.routing.revision || source.status !== 'FIXED_ROUTE_IDS_FRESH_METADATA_QUALIFIED') fail('HOLD_ROUTE_REVISION');
    if (!source.routes || typeof source.routes !== 'object' || Array.isArray(source.routes)) fail('HOLD_AMBIGUOUS_ROUTE');
    const selected = source.routes[request.purpose];
    if (!selected) fail('HOLD_MISSING_ROUTE');
    if (Array.isArray(selected) || !text(selected.parent_id)) fail('HOLD_AMBIGUOUS_ROUTE');
    const tasks = Array.isArray(selected.tasks) ? selected.tasks : [selected.task || source.task];
    if (selected.task && selected.tasks || tasks.some(t => !text(t) || t === '*') ||
        tasks.filter(t => t === request.task).length !== 1) fail('HOLD_TASK_ROUTE_SCOPE');
    // Destination ID comes ONLY from the exact executor-owned raw map, never
    // from an alternate live policy list. Destination contains task-local scope
    // and fresh-metadata expectations, not another ID mapping.
    if (!request.destination || 'parent_id' in request.destination) fail('HOLD_DESTINATION_SCOPE_UNKNOWN');
    const route = {...request.destination, task: request.task, purpose: request.purpose, parent_id: selected.parent_id};
    if (route.disposition !== 'QUALIFIED_OWNER_HOME' || route.fallback !== false ||
        !Array.isArray(route.allowed_operations) || !route.allowed_operations.includes(request.kind)) fail('HOLD_ROUTE_NOT_QUALIFIED');
    id(route.parent_id); ref(route.owner_basis);
    if (!text(route.access_boundary_ref) || !Array.isArray(route.expected_parent_ids) || !route.expected_parent_ids.length)
      fail('HOLD_PARENT_BASIS_UNKNOWN');
    route.expected_parent_ids.forEach(id);
    if (route.expected_parent_ids.includes(route.parent_id)) fail('HOLD_ROOT_OR_FORBIDDEN_DESTINATION');
    if (request.kind === 'folder' ? request.mime_type !== FOLDER : request.mime_type === FOLDER) fail('HOLD_OPERATION_MIME');
    if (request.kind === 'create' && !NATIVE.includes(request.mime_type)) fail('HOLD_OPERATION_MIME');
    const index = ['import_document', 'import_spreadsheet', 'import_presentation'].indexOf(request.kind);
    if (index >= 0 && request.mime_type !== NATIVE[index]) fail('HOLD_OPERATION_MIME');
    if (request.kind !== 'folder') { ref(request.content); hash(request.content.sha256); }
    if (request.kind === 'update') {
      if (!request.existing || request.existing.content_update_authorized !== true) fail('HOLD_EXISTING_UPDATE_NOT_AUTHORIZED');
      id(request.existing.file_id); ref(request.existing);
    }
    return route;
  }
  function qualifyParent(route, value) {
    const m = metadata(value);
    if (m.id !== route.parent_id || m.mime !== FOLDER || !same(m.parents, sorted(route.expected_parent_ids))) fail('HOLD_PARENT_BASIS_MISMATCH');
    const a = route.access;
    if (!a) fail('HOLD_ACCESS_BASIS_UNKNOWN');
    // A reserve for omitted fields cannot erase a fresh negative or malformed
    // provider fact, regardless of the caller's selected access mode.
    if (m.capabilities?.canAddChildren === false) fail('HOLD_CAPABILITY_EXPLICIT_DENY');
    if (m.capabilities && 'canAddChildren' in m.capabilities && m.capabilities.canAddChildren !== true)
      fail('HOLD_CAPABILITY_CONFLICT');
    if (a.mode === 'provider_capability') {
      if (m.capabilities?.canAddChildren !== true || m.trashed !== false) fail('HOLD_CAPABILITY_UNEXPOSED_OR_DENIED');
      return 'PROVIDER_CAN_ADD_CHILDREN_OBSERVED';
    }
    // This explicit branch qualifies owner scope, NOT a provider canWrite claim.
    // Real connector errors remain holds. current_user_can_share is never used.
    if (a.mode !== 'owner_qualified_permission' || a.unexposed_capabilities !== 'OWNER_SCOPED_NOT_PROVIDER_ATTESTATION' ||
        a.unexposed_trashed !== 'OWNER_DISPOSITION_REQUIRED_IF_UNKNOWN') fail('HOLD_ACCESS_BASIS_UNKNOWN');
    ref(a);
    const hasId = Object.prototype.hasOwnProperty.call(a, 'permission_id');
    const hasPrincipal = Object.prototype.hasOwnProperty.call(a, 'principal_selector');
    if (hasId === hasPrincipal) fail('HOLD_PERMISSION_SELECTOR_MISSING_OR_CONFLICTING');
    if (m.visibility !== 'permission_metadata_available' || !Array.isArray(m.permissions)) fail('HOLD_PERMISSION_METADATA_UNKNOWN');
    let ps;
    if (hasId) {
      id(a.permission_id);
      ps = m.permissions.filter(p => p.id === a.permission_id && p.type === 'user');
    } else {
      const selector = a.principal_selector;
      if (!selector || selector.kind !== 'observed_user_email' || typeof selector.value !== 'string' ||
          selector.value.length > 320 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(selector.value)) fail('HOLD_PRINCIPAL_SELECTOR_MALFORMED');
      ref(selector);
      // Exact privately observed value, no casing/name/account inference and no
      // manufactured provider permission ID. Conflicting aliases remain HOLD.
      ps = m.permissions.filter(p => p.type === 'user' && alias(p, 'emailAddress', 'email_address') === selector.value);
      if (ps.some(p => 'id' in p || 'permission_id' in p)) fail('HOLD_PRINCIPAL_RESERVE_ID_EXPOSED_USE_ID_SELECTOR');
    }
    if (ps.length !== 1 || !['owner', 'writer', 'organizer', 'fileOrganizer'].includes(ps[0].role) || ps[0].deleted === true)
      fail('HOLD_OWNER_PERMISSION_DENIED');
    return 'OWNER_QUALIFIED_PERMISSION_ROUTE_NOT_PROVIDER_CAPABILITY';
  }
  function qualifyRoot(value, binding, route) {
    if (!value || typeof value !== 'object') fail('HOLD_ROOT_METADATA_UNKNOWN');
    const rootId = id(value.id);
    if (alias(value, 'mimeType', 'mime_type') !== FOLDER || value.trashed === true || rootId === route.parent_id)
      fail('HOLD_ROOT_ID_OR_TYPE');
    const parents = alias(value, 'parents', 'parent_ids');
    const admitted = binding.root_lookup;
    if (admitted) {
      if (admitted.lookup !== 'literal_root') fail('HOLD_ROOT_LOOKUP_BINDING');
      ref(admitted);
      if (id(admitted.expected_id) !== rootId) fail('HOLD_ROOT_EXPECTED_ID_MISMATCH');
    }
    if (Array.isArray(parents)) {
      if (parents.length) fail('HOLD_ROOT_HAS_PARENTS');
      return {id: rootId, parent_evidence: 'ACTUAL_OBSERVED_EMPTY_PARENT_LIST'};
    }
    if (parents !== null || !admitted || admitted.null_parent_disposition !== 'ACTUAL_LITERAL_ROOT_NORMALIZER_OMITS_PARENTS')
      fail('HOLD_ROOT_NULL_BINDING_REQUIRED');
    // This narrowly qualified root fact is NOT an observed empty list. Generic
    // metadata() remains strict and cannot consume null parent metadata.
    return {id: rootId, parent_evidence: 'ACTUAL_LITERAL_ROOT_NORMALIZED_NULL_OWNER_BOUND_NOT_OBSERVED_EMPTY'};
  }
  function toolSpec(request, payload, route, binding) {
    if (!payload || typeof payload !== 'object') fail('HOLD_PAYLOAD_UNKNOWN');
    if (!binding || !['oauth_my_drive', 'direct_shared_drive'].includes(binding.connection)) fail('HOLD_CONNECTION_BINDING_UNKNOWN');
    ref(binding);
    const parent = route.parent_id;
    switch (request.kind) {
      case 'upload':
        if (!text(payload.file_uri) || NATIVE.includes(request.mime_type) || !text(payload.file_name)) fail('HOLD_PAYLOAD_TYPE');
        return {tool: 'upload_file', args: {file_uri: payload.file_uri, file_name: payload.file_name, mime_type: request.mime_type, parent_folder_id: parent}};
      case 'folder':
        if (!text(payload.name)) fail('HOLD_PAYLOAD_TYPE');
        return {tool: 'create_folder', args: {name: payload.name, parent_folder: parent}};
      case 'copy':
        id(payload.source_id);
        return {tool: 'copy_file', args: {url: 'https://drive.google.com/file/d/' + payload.source_id + '/view', parent_folder: parent,
          ...(text(payload.new_title) ? {new_title: payload.new_title} : {})}};
      case 'create': {
        if (!text(payload.title)) fail('HOLD_PAYLOAD_TYPE');
        return {tool: 'create_file', args: {title: payload.title, mime_type: request.mime_type,
          ...(binding.connection === 'direct_shared_drive' ? {parent_folder_id: parent} : {})}};
      }
      case 'import_document': case 'import_spreadsheet': case 'import_presentation': {
        if (!text(payload.source_file)) fail('HOLD_PAYLOAD_TYPE');
        const modes = {import_document: 'native_google_docs', import_spreadsheet: 'native_google_sheets', import_presentation: 'native_google_slides'};
        return {tool: request.kind, args: {source_file: payload.source_file, upload_mode: modes[request.kind],
          ...(text(payload.title) ? {title: payload.title} : {}), ...(binding.connection === 'direct_shared_drive' ? {parent_folder_id: parent} : {})}};
      }
      case 'update':
        if (!text(payload.file_uri) || NATIVE.includes(request.mime_type)) fail('HOLD_NATIVE_UPDATE_REQUIRES_QUALIFIED_COMPONENT_PORT');
        return {tool: 'update_file', args: {fileId: request.existing.file_id, file_uri: payload.file_uri, mime_type: request.mime_type}};
      default: fail('HOLD_OPERATION_UNSUPPORTED');
    }
  }
  async function prepare({request, routingMap, payload, binding, parentMetadata}) {
    const route = resolve(request, routingMap);
    if (hash(await sha256(routingMap.raw_json)) !== request.routing.sha256) fail('HOLD_ROUTING_BYTES');
    if (await digest(payload) !== request.payload_sha256) fail('HOLD_PAYLOAD_BINDING');
    const access = qualifyParent(route, parentMetadata);
    const spec = toolSpec(request, payload, route, binding);
    const transient = binding.connection === 'oauth_my_drive' && ['create', 'import_document', 'import_spreadsheet', 'import_presentation'].includes(request.kind);
    if (transient && binding.transient_staging !== 'EXPLICIT_NEW_ID_MY_DRIVE_ROOT_COMPLETE_BEFORE_LINK') fail('HOLD_TRANSIENT_STAGING_NOT_AUTHORIZED');
    const intentHash = await digest({request, route, binding});
    return {route, spec, transient, access, intentHash};
  }
  function safeIntent(request, p) {
    return {schema: 'cerebro-drive-placement-journal/v1', version: 1, intent_hash: p.intentHash,
      operation_id: request.operation_id, task: request.task, purpose: request.purpose,
      kind: request.kind, parent_id: p.route.parent_id, mime_type: request.mime_type,
      routing: request.routing, access_qualification: p.access, state: request.kind === 'update' ? 'UPDATE_INTENT' : 'CREATE_INTENT',
      file_id: request.kind === 'update' ? request.existing.file_id : null, created_by_operation: request.kind !== 'update'};
  }
  async function execute(input) {
    const {request, routingMap, payload, binding, tools, journal} = input;
    const route = resolve(request, routingMap); // Before any provider call.
    if (!journal || typeof journal.runExclusive !== 'function' || !tools || typeof tools.readMetadata !== 'function' ||
        typeof tools.invoke !== 'function') fail('HOLD_CALLER_PORT_UNQUALIFIED');
    ref(journal.qualification);
    if (!text(journal.qualification.durability_evidence_ref) || !text(journal.qualification.exclusivity_evidence_ref)) fail('HOLD_JOURNAL_PROOF_PORT_UNKNOWN');
    return journal.runExclusive(request.operation_id, async storage => {
      if (!storage || typeof storage.load !== 'function' || typeof storage.commit !== 'function') fail('HOLD_DURABLE_JOURNAL_PORT_UNKNOWN');
      const p = await prepare({...input, parentMetadata: await tools.readMetadata(route.parent_id)});
      if (typeof tools.supports !== 'function' || !tools.supports(p.spec.tool) ||
          (p.transient && !tools.supports('update_file')) || (request.kind !== 'folder' && typeof tools.readContent !== 'function'))
        fail('HOLD_TOOL_OR_CONTENT_PORT_UNSUPPORTED');
      if (request.kind === 'copy') {
        const origin = metadata(await tools.readMetadata(payload.source_id));
        if (origin.id !== payload.source_id || origin.mime !== request.mime_type) fail('HOLD_COPY_SOURCE_MISMATCH');
      }
      let record = await storage.load();
      if (record && (record.schema !== 'cerebro-drive-placement-journal/v1' || record.intent_hash !== p.intentHash ||
          record.operation_id !== request.operation_id || !Number.isInteger(record.version))) fail('HOLD_JOURNAL_BINDING_CONFLICT');
      const terminal = record?.state === 'VERIFIED_RELEASED';
      // Known unsupported root normalization must hold BEFORE the first new
      // native artifact is created, not strand an avoidable root-staged ID.
      const initialRoot = p.transient && !record ? qualifyRoot(await tools.readMetadata('root'), binding, route) : null;
      async function save(next) {
        const expected = record ? record.version : 0;
        const value = {...next, version: expected + 1};
        let ack;
        try { ack = await storage.commit(expected, value); }
        catch (_) { fail('HOLD_JOURNAL_COMMIT_UNKNOWN', next.file_id); }
        if (!ack || ack.durable !== true || ack.version !== value.version || !text(ack.evidence_ref)) fail('HOLD_JOURNAL_DURABILITY_UNKNOWN', next.file_id);
        if (!same(await storage.load(), value)) fail('HOLD_JOURNAL_READBACK_MISMATCH', next.file_id);
        record = value;
      }
      let fresh = false;
      if (!record) {
        const intent = safeIntent(request, p);
        if (initialRoot) intent.root_lookup_observation = initialRoot;
        await save(intent); fresh = true;
      }
      if (!record.file_id) {
        if (!fresh) fail('HOLD_UNKNOWN_CREATE_RECONCILE_EXACT_RECEIPT_NO_RETRY');
        let observedId;
        try { observedId = id((await tools.invoke(p.spec.tool, p.spec.args)).id); }
        catch (_) { fail('HOLD_UNKNOWN_CREATE_RECONCILE_EXACT_RECEIPT_NO_RETRY'); }
        // No downstream effect/read/link until returned ID is durably committed.
        await save({...record, file_id: observedId, state: 'KNOWN_ID'});
      } else if (request.kind === 'update' && fresh) {
        const before = metadata(await tools.readMetadata(record.file_id));
        if (before.id !== record.file_id || before.mime !== request.mime_type || !same(before.parents, [route.parent_id])) fail('HOLD_EXISTING_UPDATE_SCOPE');
        try { await tools.invoke(p.spec.tool, p.spec.args); }
        catch (_) { fail('HOLD_UNKNOWN_UPDATE_RECONCILE_SAME_ID', record.file_id); }
        await save({...record, state: 'CONTENT_UPDATED'});
      }
      let m = metadata(await tools.readMetadata(record.file_id));
      if (m.id !== record.file_id || m.mime !== request.mime_type) fail('HOLD_ID_OR_MIME_READBACK', record.file_id);
      if (!same(m.parents, [route.parent_id])) {
        if (terminal) fail('HOLD_RELEASED_ARTIFACT_DRIFT_OWNER_RECONCILE', record.file_id);
        if (!record.created_by_operation || !p.transient) fail('HOLD_PARENT_READBACK', record.file_id);
        // A previous uncertain placement is only read, never repeated automatically.
        if (['PLACE_INTENT', 'PLACEMENT_RETURNED'].includes(record.state)) fail('HOLD_UNKNOWN_PLACEMENT_RECONCILE_SAME_ID', record.file_id);
        const root = qualifyRoot(await tools.readMetadata('root'), binding, route);
        if ((record.root_lookup_observation && record.root_lookup_observation.id !== root.id) || !same(m.parents, [root.id]))
          fail('HOLD_TRANSIENT_PARENT_NOT_OBSERVED_MY_DRIVE_ROOT', record.file_id);
        const args = {fileId: record.file_id, addParents: route.parent_id, removeParents: root.id};
        // Requalify the destination immediately before the actual placement.
        qualifyParent(route, await tools.readMetadata(route.parent_id));
        await save({...record, state: 'PLACE_INTENT', observed_parent_ids: m.parents});
        try { await tools.invoke('update_file', args); }
        catch (_) { fail('HOLD_UNKNOWN_PLACEMENT_RECONCILE_SAME_ID', record.file_id); }
        await save({...record, state: 'PLACEMENT_RETURNED'});
        m = metadata(await tools.readMetadata(record.file_id));
      }
      if (m.id !== record.file_id || m.mime !== request.mime_type || !same(m.parents, [route.parent_id])) fail('HOLD_PARENT_READBACK', record.file_id);
      if (request.kind === 'folder') {
        if (m.name !== payload.name) fail('HOLD_FOLDER_NAME_READBACK', record.file_id);
      } else {
        if (typeof tools.readContent !== 'function') fail('HOLD_CONTENT_PORT_UNKNOWN', record.file_id);
        const c = await tools.readContent({fileId: record.file_id, mimeType: request.mime_type, expectation: request.content});
        if (!c || c.file_id !== record.file_id || !text(c.source_ref) || c.observed_sha256 !== request.content.sha256)
          fail('HOLD_CONTENT_READBACK', record.file_id);
      }
      // A fresh final metadata read guards link release, without claiming CAS.
      m = metadata(await tools.readMetadata(record.file_id));
      if (m.id !== record.file_id || m.mime !== request.mime_type || !same(m.parents, [route.parent_id])) fail('HOLD_FINAL_PARENT_READBACK', record.file_id);
      if (request.kind === 'folder' && m.name !== payload.name) fail('HOLD_FOLDER_NAME_READBACK', record.file_id);
      let url;
      try { url = new URL(m.url); } catch (_) { fail('HOLD_OBSERVED_LINK_UNKNOWN', record.file_id); }
      if (url.protocol !== 'https:' || !['drive.google.com', 'docs.google.com'].includes(url.hostname) ||
          !url.pathname.split('/').includes(record.file_id)) fail('HOLD_OBSERVED_LINK_ID_MISMATCH', record.file_id);
      if (!terminal) await save({...record, state: 'VERIFIED_RELEASED', content_readback: request.kind === 'folder' ? 'FOLDER_NAME_AND_TYPE' : 'EXPECTED_DIGEST_MATCH'});
      return {status: 'VERIFIED_RELEASED', file_id: record.file_id, parent_id: route.parent_id,
        url: m.url, mime_type: request.mime_type, access_qualification: p.access, authority: 'NONE_ADDED',
        concurrency_limit: 'READ_WRITE_NOT_CAS; caller must preserve exclusive producer and honest access limits'};
    });
  }
  // Only an exact positive tool-result receipt can recover an intent lacking ID.
  // Receipt provenance is a qualified caller port, never a filename search.
  async function recoverKnownCreate({operationId, evidence, journal, verifyReceipt}) {
    if (!evidence || evidence.operation_id !== operationId || !text(evidence.source_ref) || !text(evidence.revision) ||
        typeof verifyReceipt !== 'function' || !journal?.runExclusive) fail('HOLD_RECOVERY_BASIS_UNKNOWN');
    const fileId = id(evidence.file_id);
    return journal.runExclusive(operationId, async storage => {
      const r = await storage.load();
      if (!r || r.state !== 'CREATE_INTENT' || r.file_id || r.intent_hash !== evidence.intent_hash ||
          r.operation_id !== operationId || await verifyReceipt(evidence, r) !== true) fail('HOLD_RECOVERY_NOT_EXACT');
      const next = {...r, file_id: fileId, state: 'KNOWN_ID', recovery_source_ref: evidence.source_ref, version: r.version + 1};
      const ack = await storage.commit(r.version, next);
      if (!ack || ack.durable !== true || ack.version !== next.version || !text(ack.evidence_ref)) fail('HOLD_JOURNAL_DURABILITY_UNKNOWN', fileId);
      if (!same(await storage.load(), next)) fail('HOLD_JOURNAL_READBACK_MISMATCH', fileId);
      return {status: 'KNOWN_ID_RECOVERED_NOT_RELEASED', file_id: fileId};
    });
  }
  function managedConnector(callbacks, {decode, readContent}) {
    if (typeof decode !== 'function') fail('HOLD_CONNECTOR_DECODER_UNQUALIFIED');
    return {
      supports(tool) { return typeof callbacks['google_drive_' + tool] === 'function'; },
      async readMetadata(fileId) {
        const fn = callbacks.google_drive_get_file_metadata;
        if (typeof fn !== 'function') fail('HOLD_METADATA_PORT_UNKNOWN');
        const raw = await fn({fileId, fields: 'id,name,mimeType,parents,permissions,capabilities,webViewLink,trashed'});
        return decode(raw, 'metadata');
      },
      async invoke(tool, args) {
        const fn = callbacks['google_drive_' + tool];
        if (typeof fn !== 'function') fail('HOLD_TOOL_PORT_UNSUPPORTED');
        return decode(await fn(args), tool);
      }, readContent
    };
  }
  return {Hold, canonical, metadata, resolve, prepare, execute, recoverKnownCreate, managedConnector};
});
