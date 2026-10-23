'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const {constants} = require('node:fs');

// Private caller-owned append-only snapshots. An interrupted process leaves its
// lock in place. Never steal a lock or truncate a partial WAL automatically.
module.exports = function localJournal(root) {
  root = path.resolve(root);
  return {
    qualification: {source_ref: __filename, revision: 'v1',
      durability_evidence_ref: 'fsync-each-append-and-load-readback; caller-qualified-filesystem:' + root,
      exclusivity_evidence_ref: 'exclusive-mkdir-lock-no-stale-stealing:' + root},
    async runExclusive(operationId, fn) {
      if (!/^[A-Za-z0-9_-]{1,120}$/.test(operationId)) throw new Error('HOLD_INVALID_OPERATION_ID');
      const parent = await fs.lstat(root);
      if (!parent.isDirectory() || parent.isSymbolicLink()) throw new Error('HOLD_JOURNAL_ROOT_UNQUALIFIED');
      // A single caller-preinstalled WAL avoids claiming that fsync(file)
      // alone makes a newly created directory entry crash durable. Installation
      // and filesystem qualification precede effect use, owned by the caller.
      const lock = path.join(root, 'placement.lock');
      const wal = path.join(root, 'placement-journal.jsonl');
      try { await fs.mkdir(lock); } catch (_) { throw new Error('HOLD_JOURNAL_LOCKED_OR_UNAVAILABLE'); }
      try {
        async function load() {
          let b;
          try {
            const st = await fs.lstat(wal);
            if (!st.isFile() || st.isSymbolicLink()) throw new Error('HOLD_JOURNAL_PATH_UNQUALIFIED');
            b = await fs.readFile(wal, 'utf8');
          } catch (e) { if (e.code === 'ENOENT') throw new Error('HOLD_PREINSTALLED_JOURNAL_REQUIRED'); throw e; }
          if (!b) return null;
          if (!b.endsWith('\n')) throw new Error('HOLD_PARTIAL_JOURNAL_RECONCILE_NO_TRUNCATION');
          const latest = new Map();
          for (const line of b.slice(0, -1).split('\n')) {
            let row;
            try { row = JSON.parse(line); } catch (_) { throw new Error('HOLD_PARTIAL_JOURNAL_RECONCILE_NO_TRUNCATION'); }
            const last = latest.get(row.operation_id);
            if (!/^[A-Za-z0-9_-]{1,120}$/.test(row.operation_id || '') || row.version !== (last ? last.version + 1 : 1) ||
                (last && row.intent_hash !== last.intent_hash)) throw new Error('HOLD_JOURNAL_CHAIN_CONFLICT');
            latest.set(row.operation_id, row);
          }
          return latest.get(operationId) || null;
        }
        const storage = {load, async commit(expected, record) {
          const old = await load();
          if ((old?.version || 0) !== expected || record.version !== expected + 1 || record.operation_id !== operationId)
            throw new Error('HOLD_JOURNAL_CAS_CONFLICT');
          let handle;
          try { handle = await fs.open(wal, constants.O_WRONLY | constants.O_APPEND | (constants.O_NOFOLLOW || 0));
            await handle.writeFile(JSON.stringify(record) + '\n'); await handle.sync();
          } finally { await handle?.close(); }
          if (JSON.stringify(await load()) !== JSON.stringify(record)) throw new Error('HOLD_JOURNAL_READBACK_MISMATCH');
          return {durable: true, version: record.version, evidence_ref: wal + '#version=' + record.version};
        }};
        return await fn(storage);
      } finally { await fs.rmdir(lock); }
    }
  };
};
