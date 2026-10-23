'use strict';
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory;
  else root.CerebroDriveDocJournal = factory;
})(globalThis, function createDocJournal({read, write, newLockToken, qualification}) {
  // Native bodies remain inside the caller's same managed-tool isolate. The
  // caller decodes ONE dedicated JSON-body journal doc, never arbitrary prose.
  const hold = code => { throw new Error(code); };
  const plain = v => v && typeof v === 'object' && !Array.isArray(v);
  const serial = v => JSON.stringify(v);
  if (typeof read !== 'function' || typeof write !== 'function' || typeof newLockToken !== 'function' ||
      !qualification?.source_ref || !qualification.revision || !qualification.durability_evidence_ref || !qualification.exclusivity_evidence_ref)
    hold('HOLD_DOC_JOURNAL_PORT_UNQUALIFIED');
  async function snapshot() {
    const response = await read();
    if (!response || typeof response.revisionId !== 'string' || !response.revisionId || typeof response.text !== 'string') hold('HOLD_DOC_REVISION_OR_BODY_UNKNOWN');
    let body;
    try { body = JSON.parse(response.text); } catch (_) { hold('HOLD_DOC_JOURNAL_BODY_NOT_JSON'); }
    if (body.schema !== 'cerebro-drive-doc-journal/v1' || !Number.isInteger(body.version) || body.version < 1 ||
        !plain(body.operations) || (body.lock !== null && (!plain(body.lock) || !body.lock.token || !body.lock.operation_id)))
      hold('HOLD_DOC_JOURNAL_SCHEMA');
    return {revisionId: response.revisionId, body};
  }
  async function cas(old, next) {
    // write MUST bind this exact provider requiredRevisionId, no retry/rebase.
    await write({requiredRevisionId: old.revisionId, text: serial(next)});
    const after = await snapshot();
    if (serial(after.body) !== serial(next) || after.revisionId === old.revisionId) hold('HOLD_DOC_COMMIT_READBACK_UNKNOWN');
    return after;
  }
  return {qualification, async runExclusive(operationId, fn) {
    if (!/^[A-Za-z0-9_-]{1,120}$/.test(operationId)) hold('HOLD_INVALID_OPERATION_ID');
    const token = await newLockToken();
    if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{20,200}$/.test(token)) hold('HOLD_DOC_LOCK_TOKEN_UNQUALIFIED');
    let current = await snapshot();
    if (current.body.lock !== null) hold('HOLD_DOC_JOURNAL_LOCKED_NO_STEAL');
    current = await cas(current, {...current.body, version: current.body.version + 1, lock: {token, operation_id: operationId}});
    async function owned() {
      const s = await snapshot();
      if (s.body.lock?.token !== token || s.body.lock.operation_id !== operationId) hold('HOLD_DOC_JOURNAL_OWNERSHIP_LOST');
      return s;
    }
    try {
      const storage = {
        async load() { const s = await owned(); return s.body.operations[operationId] || null; },
        async commit(expectedVersion, record) {
          const s = await owned(); const old = s.body.operations[operationId];
          if ((old?.version || 0) !== expectedVersion || record.version !== expectedVersion + 1 || record.operation_id !== operationId ||
              (old && old.intent_hash !== record.intent_hash)) hold('HOLD_DOC_JOURNAL_CAS_CONFLICT');
          current = await cas(s, {...s.body, version: s.body.version + 1, operations: {...s.body.operations, [operationId]: record}});
          return {durable: true, version: record.version,
            evidence_ref: qualification.source_ref + '#provider-revision=' + current.revisionId + '/operation-version=' + record.version};
        }
      };
      return await fn(storage);
    } finally {
      // This is a separate known lock release, never a replay of a content or
      // creation effect. Unknown release stays a hold; no automatic CAS retry.
      const s = await owned();
      await cas(s, {...s.body, version: s.body.version + 1, lock: null});
    }
  }};
});
