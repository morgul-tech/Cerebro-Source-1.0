'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {createHash} = require('node:crypto');
const factory = require('./placement-factory.js');
const localJournal = require('./local-journal.cjs');
const api = factory({sha256: s => createHash('sha256').update(s, 'utf8').digest('hex')});

async function main() {
  const args = process.argv.slice(2); const command = args.shift();
  if (!['prepare', 'execute'].includes(command) || args.length % 2) throw new Error('HOLD_CLI_ARGUMENTS');
  const flags = {};
  for (let i = 0; i < args.length; i += 2) {
    if (!['--request', '--routing', '--payload', '--bindings', '--journal-root', '--parent-metadata'].includes(args[i]) || args[i] in flags)
      throw new Error('HOLD_CLI_ARGUMENTS');
    flags[args[i]] = args[i + 1];
  }
  const read = async f => JSON.parse(await fs.readFile(f, 'utf8'));
  const request = await read(flags['--request']);
  const payload = await read(flags['--payload']);
  // Import ONLY an explicit rightful caller-owned binding. No credentials,
  // shell execution, HTTP implementation or env-based default is supplied.
  const bindings = await import(pathToFileURL(path.resolve(flags['--bindings'])).href);
  const input = {request, payload, routingMap: {source_ref: request.routing.source_ref,
    raw_json: await fs.readFile(flags['--routing'], 'utf8')}, binding: bindings.binding};
  if (command === 'prepare') {
    const p = await api.prepare({...input, parentMetadata: await read(flags['--parent-metadata'])});
    // No private title, payload URI/body, permissions or tool arguments on stdout.
    return {status: 'PLAN_ONLY', parent_id: p.route.parent_id, tool: p.spec.tool,
      intent_hash: p.intentHash, transient: p.transient, authority: 'NONE_ADDED'};
  }
  return api.execute({...input, tools: api.managedConnector(bindings.callbacks, {
    decode: bindings.decode, readContent: bindings.readContent}), journal: localJournal(flags['--journal-root'])});
}
main().then(result => process.stdout.write(JSON.stringify(result) + '\n')).catch(e => {
  // Tool exception messages can contain private payload; never print them here.
  const code = /^(HOLD_[A-Z0-9_]+)$/.test(e.code || e.message) ? (e.code || e.message) : 'HOLD_CALLER_PORT_OR_INPUT_FAILURE';
  process.stdout.write(JSON.stringify({status: code, file_id: e.fileId || null, final_url: null}) + '\n');
  process.exitCode = 2;
});
