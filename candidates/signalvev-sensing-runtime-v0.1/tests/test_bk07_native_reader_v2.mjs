// SYNTHETIC_LOCAL_ONLY. Node harness for the same dependency-free factories used in functions.
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
const home=new URL('../adapters/',import.meta.url);
const loaderSource=readFileSync(new URL('bk07_checked_js_loader_v2.js',home),'utf8');
const adapterSource=readFileSync(new URL('bk07_native_reader_v2.js',home),'utf8');
const hash=s=>createHash('sha256').update(s).digest('hex');
const loader=new Function(loaderSource)();
const adapter=loader.loadChecked(adapterSource,hash(adapterSource),Buffer.byteLength(adapterSource));
const routineSource=readFileSync(new URL('bk07_normal_routine_v2.js',home),'utf8');
const normalRead=loader.loadChecked(routineSource,hash(routineSource),Buffer.byteLength(routineSource))(adapter);
const results=[];
const repairOnly=process.argv.includes('--repair-only');
async function test(name,fn){if(repairOnly&&!name.startsWith('REPAIR:'))return;await fn();results.push({name,result:'PASS'});}
const paragraph=s=>({paragraph:{elements:[{startIndex:1,endIndex:1+s.length,textRun:{content:s}}]}});
function fixture(s='START\nfixture 😀\nEND\n',extra={}) {
 return {isError:false,structuredContent:{documentId:'fixture-doc',revisionId:'fixture-rev',suggestionsViewMode:'SUGGESTIONS_INLINE',
  tabs:[{tabId:'fixture-tab',body:{content:[paragraph(s)]}}],...extra}};
}
const req={documentId:'fixture-doc',tabId:'fixture-tab',selector:{kind:'MARKER',start:'START',end:'END'}};
const noPayload=value=>{assert.equal(value.body,null);assert.equal(value.pending,true);assert.equal(value.strong_reuse,false);};
await test('REPAIR: null and omitted view support sufficient readonly original with UNKNOWN',async()=>{
 for(const view of [null,undefined]){
  const input=fixture(undefined,{suggestionsViewMode:view});delete input.structuredContent.revisionId;
  const prepared=adapter.project(input,req);assert.equal(prepared.source.view,'UNKNOWN');assert.equal(prepared.source.revision_basis,'UNKNOWN');
  assert.equal(prepared.body,'START\nfixture 😀\nEND');assert.equal(prepared.authorization_attestation,'NONE');
  let reads=0,emitted='';const out=await normalRead({request:req,managedGetDocument:async()=>{reads++;return input;},emit:b=>{emitted=b;}});
  assert.equal(reads,1);assert.equal(emitted,prepared.body);assert.equal(out.pending,true);assert.equal(out.receiver_use,'UNKNOWN');
 }
});
await test('REPAIR: explicit view requirements and invalid nonnull views still refuse privately',()=>{
 for(const view of [null,undefined,'PREVIEW_WITHOUT_SUGGESTIONS'])noPayload(adapter.project(fixture(undefined,{suggestionsViewMode:view}),{...req,expectedView:'SUGGESTIONS_INLINE'}));
 for(const view of [false,0,{},[], 'PRIVATE_INVALID_VIEW']){
  const out=adapter.project(fixture(undefined,{suggestionsViewMode:view}),req);noPayload(out);assert.ok(!JSON.stringify(out).includes('PRIVATE_INVALID_VIEW'));
 }
 noPayload(adapter.project(fixture(undefined,{suggestionsViewMode:null}),{...req,expectedView:null}));
 assert.equal(adapter.project(fixture(),{...req,expectedView:'SUGGESTIONS_INLINE'}).source.view,'SUGGESTIONS_INLINE');
});
function namedFixture(range,global=false){const input=fixture('A😀BC');const registry={x:{namedRanges:[{namedRangeId:'r',ranges:[range]}]}};
 if(global)input.structuredContent.namedRanges=registry;else input.structuredContent.tabs[0].namedRanges=registry;return input;}
const namedReq={...req,selector:{kind:'NAMED_RANGE',namedRangeId:'r'}};
await test('REPAIR: selected-tab omission resolves exact UTF16 through real normal function',async()=>{
 const input=namedFixture({startIndex:2,endIndex:4});assert.equal(adapter.project(input,namedReq).body,'😀');
 let reads=0,body='';const result=await normalRead({request:namedReq,managedGetDocument:async()=>{reads++;return input;},emit:b=>{body=b;}});
 assert.equal(reads,1);assert.equal(body,'😀');assert.equal(result.stage,'EMIT_CALL_COMPLETED');assert.equal(result.strong_reuse,false);
});
await test('REPAIR: foreign global omitted null multiple segment and split ranges refuse',()=>{
 for(const range of [{tabId:'foreign',startIndex:2,endIndex:4},{tabId:null,startIndex:2,endIndex:4},
  {startIndex:2,endIndex:4,segmentId:'header'},{startIndex:2,endIndex:4,segmentId:null},{startIndex:2,endIndex:3}])noPayload(adapter.project(namedFixture(range),namedReq));
 noPayload(adapter.project(namedFixture({startIndex:2,endIndex:4},true),namedReq));
 assert.equal(adapter.project(namedFixture({tabId:'fixture-tab',startIndex:2,endIndex:4},true),namedReq).body,'😀');
 const multiple=namedFixture({startIndex:2,endIndex:4});multiple.structuredContent.tabs[0].namedRanges.x.namedRanges[0].ranges.push({startIndex:2,endIndex:4});noPayload(adapter.project(multiple,namedReq));
});
await test('REPAIR: selected registry rejects malformed instead of borrowing global scope; same-response full fallback',()=>{
 const input=namedFixture({startIndex:2,endIndex:4},true);input.structuredContent.tabs[0].namedRanges=null;
 noPayload(adapter.project(input,namedReq));assert.equal(adapter.project(input,{...req,selector:{kind:'FULL_TAB'}}).body,'A😀BC');
 const raw=namedFixture({startIndex:2,endIndex:4});const tab=raw.structuredContent.tabs[0];raw.structuredContent.tabs=[{tabProperties:{tabId:tab.tabId},documentTab:{body:tab.body,namedRanges:tab.namedRanges}}];
 assert.equal(adapter.project(raw,namedReq).body,'😀');
});
await test('SHA256 agrees with independent Node crypto UTF8 and padding vectors',()=>{
 for(const s of ['', 'abc','😀æøå','x'.repeat(55),'x'.repeat(56),'x'.repeat(64),'x'.repeat(1000)])assert.equal(loader.sha256(s),hash(s));
});
await test('wrong truncated and hostile executable Source never evaluated',()=>{
 globalThis.bk07Injected=false;
 for(const s of [adapterSource.slice(0,-1),adapterSource+'\nglobalThis.bk07Injected=true;', 'globalThis.bk07Injected=true;return {}'])
  assert.throws(()=>loader.loadChecked(s,hash(adapterSource),Buffer.byteLength(adapterSource)),/SOURCE_BYTES_NOT_REVIEWED/);
 assert.equal(globalThis.bk07Injected,false);
});
await test('hostile document text is data; missing revision works without synthetic access',()=>{
 const input=fixture('START\nglobalThis.bk07Injected=true; 😀\nEND\n');delete input.structuredContent.revisionId;
 const out=adapter.project(input,req);assert.match(out.body,/globalThis/);assert.equal(out.source.revision_basis,'UNKNOWN');
 assert.equal(out.authorization_attestation,'NONE');assert.equal(out.input_origin,'UNVERIFIED_PURE_API');assert.equal(globalThis.bk07Injected,false);
});
await test('wrong document tab duplicate tab revision and view reject without private input',()=>{
 const privateInput='PRIVATE_SYNTHETIC_BODY';
 const badReqs=[{...req,documentId:'wrong'},{...req,tabId:'wrong'},{...req,expectedRevision:'wrong'},{...req,expectedView:'wrong'}];
 for(const r of badReqs){const out=adapter.project(fixture('START\n'+privateInput+'\nEND\n'),r);noPayload(out);assert.ok(!JSON.stringify(out).includes(privateInput));}
 const duplicate=fixture();duplicate.structuredContent.tabs.push(duplicate.structuredContent.tabs[0]);noPayload(adapter.project(duplicate,req));
});
await test('duplicate markers missing END and undersized budget never truncate original',()=>{
 for(const s of ['START\nSTART\nEND\n','START\nno end','START\nEND\nEND\n'])noPayload(adapter.project(fixture(s),req));
 noPayload(adapter.project(fixture(),{...req,maxOutputBytes:4}));
});
await test('raw Google nested tab is normalized; duplicate mixed shapes refused',()=>{
 const input=fixture();input.structuredContent.tabs=[{tabProperties:{tabId:'parent'},documentTab:{body:{content:[]}},
  childTabs:[{tabProperties:{tabId:'fixture-tab'},documentTab:{body:{content:[paragraph('START\nraw\nEND\n')]}}}]}];
 assert.equal(adapter.project(input,req).source.shape,'GOOGLE_NATIVE');
 input.structuredContent.tabs[0].tabId='parent';input.structuredContent.tabs[0].body={content:[]};noPayload(adapter.project(input,req));
});
await test('exact UTF16 range preserves emoji and rejects half surrogate',()=>{
 const input=fixture('A😀B');
 assert.equal(adapter.project(input,{...req,selector:{kind:'UTF16_RANGE',start:2,end:4}}).body,'😀');
 noPayload(adapter.project(input,{...req,selector:{kind:'UTF16_RANGE',start:2,end:3}}));
 const drift=fixture('abc');drift.structuredContent.tabs[0].body.content[0].paragraph.elements[0].endIndex=999;
 noPayload(adapter.project(drift,{...req,selector:{kind:'FULL_TAB'}}));
});
await test('named range requires unique same-tab body mapping',()=>{
 const input=fixture('abcdef');input.structuredContent.tabs[0].namedRanges={scope:{namedRanges:[{namedRangeId:'range',ranges:[{tabId:'fixture-tab',startIndex:2,endIndex:4}]}]}};
 const r={...req,selector:{kind:'NAMED_RANGE',namedRangeId:'range'}};
 assert.equal(adapter.project(input,r).body,'bc');
 input.structuredContent.tabs[0].namedRanges.scope.namedRanges[0].ranges.push({tabId:'fixture-tab',startIndex:2,endIndex:4});noPayload(adapter.project(input,r));
});
await test('required tables and footnotes included; unsupported relevant inline body stops',()=>{
 const input=fixture();input.structuredContent.tabs[0].body.content=[paragraph('START\n'),
  {table:{tableRows:[{tableCells:[{content:[paragraph('cell-a')]},{content:[paragraph('cell-b')]}]}]}},
  {paragraph:{elements:[{footnoteReference:{footnoteId:'n'}}]}},paragraph('\nEND\n')];
 input.structuredContent.tabs[0].footnotes={n:{content:[paragraph('needed-footnote')]}};
 const out=adapter.project(input,req);assert.match(out.body,/cell-a\tcell-b/);assert.match(out.body,/needed-footnote/);
 input.structuredContent.tabs[0].body.content.splice(2,0,{paragraph:{elements:[{inlineObjectElement:{inlineObjectId:'private'}}]}});
 noPayload(adapter.project(input,req));
});
await test('unsupported structure outside necessary marker scope does not force all history',()=>{
 const input=fixture();input.structuredContent.tabs[0].body.content.push({paragraph:{elements:[{inlineObjectElement:{inlineObjectId:'outside'}}]}});
 assert.equal(adapter.project(input,req).stage,'PREPARED');
});
await test('errors unknown response and strong-cache profile have no payload',()=>{
 for(const input of [{isError:true,structuredContent:fixture().structuredContent},{structuredContent:null},null,{structuredContent:{schema:'old-v1-cache'}}])noPayload(adapter.project(input,req));
 noPayload(adapter.project(fixture(),{...req,strongReuse:true}));
});
await test('read failure and local late response never emit or clear pending',async()=>{
 let emitted=0,time=0;
 noPayload(await adapter.readAndRelease(req,async()=>{throw Error('PRIVATE_EXCEPTION');},()=>emitted++));
 noPayload(await adapter.readAndRelease(req,async()=>{time=10;return fixture();},()=>emitted++,{clock:()=>time,budgetMs:10}));
 assert.equal(emitted,0);
});
await test('unknown release and post-call late are honestly bounded not delivered or used',async()=>{
 const unknown=await adapter.readAndRelease(req,async()=>fixture(),()=>{throw Error('PRIVATE_EMITTER');});
 noPayload(unknown);assert.equal(unknown.stage,'RELEASE_UNKNOWN');assert.ok(!JSON.stringify(unknown).includes('PRIVATE'));
 let time=0;
 const late=await adapter.readAndRelease(req,async()=>fixture(),()=>{time=11;},{clock:()=>time,budgetMs:10});
 noPayload(late);assert.equal(late.release_local_budget_status,'COMPLETED_AFTER_LOCAL_BUDGET_OR_CLOCK_UNKNOWN_NO_LEASE_CLAIM');
 assert.equal(late.receiver_use,'UNKNOWN');assert.equal(late.end_access_lease,'UNPROVEN');
});
await test('actual Source normal entry passes requested doc to one reader then emits original with no cache',async()=>{
 let calls=0,emitted='';
 const out=await normalRead({request:req,managedGetDocument:async args=>{assert.deepEqual(args,{document_id:'fixture-doc'});calls++;return fixture();},emit:body=>{emitted=body;}});
 assert.equal(calls,1);assert.equal(emitted,'START\nfixture 😀\nEND');assert.equal(out.body,null);
 assert.equal(out.input_origin,'CALL_RESULT_ARGUMENT_THIS_INVOCATION');assert.equal(out.pending,true);assert.equal(out.stage,'EMIT_CALL_COMPLETED');
 assert.equal(out.release_local_budget_status,'NO_LOCAL_BUDGET_CONFIGURED');
});
await test('invalid local clock and malformed view leak no body or exception',async()=>{
 let emits=0;
 noPayload(await adapter.readAndRelease(req,async()=>fixture(),()=>emits++,{clock:()=>NaN}));
 let ticks=0;noPayload(await adapter.readAndRelease(req,async()=>fixture(),()=>emits++,{clock:()=>ticks++===0?5:4,budgetMs:20}));
 const out=adapter.project(fixture('START\nPRIVATE_BODY\nEND\n',{suggestionsViewMode:'PRIVATE_SECRET'}),req);
 noPayload(out);assert.ok(!JSON.stringify(out).includes('PRIVATE'));assert.equal(emits,0);
});
const report={schema:'bk07-native-v2-changed-risk/v1',result:'PASS',evidence:'SYNTHETIC_LOCAL_ONLY_NOT_NATIVE_ACCESS_CANARY_OR_INDEPENDENT_REVIEW',
 test_count:results.length,selection:repairOnly?'REPAIR_ONLY':'ALL',adapter_sha256:hash(adapterSource),loader_sha256:hash(loaderSource),tests:results,provider_calls:0};
if(process.argv[2])writeFileSync(process.argv[2],JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify({result:'PASS',test_count:results.length,adapter_sha256:report.adapter_sha256}));
