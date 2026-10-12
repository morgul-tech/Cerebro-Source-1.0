"use strict";
// Pure same-episode native data projection; no provider, cache, files or authority.
return (() => {
  const PROFILE = "FRESH_NATIVE_SUPPORTED_ORCHESTRATION/v2";
  const record = v => v !== null && typeof v === "object" && !Array.isArray(v);
  const text = v => typeof v === "string" && v.length > 0;
  const integer = v => Number.isSafeInteger(v) && v >= 0;
  const fail = code => ({profile:PROFILE,outcome:"UNRESOLVED",reason:code,pending:true,
    stage:"NO_PAYLOAD",strong_reuse:false,body:null,receiver_use:"UNKNOWN"});
  function utf8Length(s) {
    let n=0;
    for(let i=0;i<s.length;i++) {
      const c=s.charCodeAt(i);
      if(c>=0xd800&&c<=0xdbff) {
        const low=s.charCodeAt(++i);if(!(low>=0xdc00&&low<=0xdfff))throw Error("INVALID_UTF16");n+=4;
      } else if(c>=0xdc00&&c<=0xdfff)throw Error("INVALID_UTF16");
      else n+=c<128?1:c<2048?2:3;
    }
    return n;
  }
  function project(callResult, request) {
    try {
      if(!record(request)||!text(request.documentId)||!text(request.tabId)||!record(request.selector)
          || (request.strongReuse!==undefined&&request.strongReuse!==false)) return fail("REQUEST_INVALID_OR_STRONG_REUSE_UNSUPPORTED");
      if(!record(callResult)||callResult.isError===true||!record(callResult.structuredContent))return fail("READER_ERROR_OR_UNKNOWN_RESPONSE");
      const doc=callResult.structuredContent;
      if(doc.documentId!==request.documentId)return fail("DOCUMENT_MISMATCH");
      const maxInput=request.maxInputNodes===undefined?50000:request.maxInputNodes;
      const maxOutput=request.maxOutputBytes===undefined?200000:request.maxOutputBytes;
      if(!integer(maxInput)||maxInput<1||maxInput>100000||!integer(maxOutput)||maxOutput<1||maxOutput>1000000)return fail("LIMIT_INVALID");
      if(request.expectedRevision!==undefined&&doc.revisionId!==request.expectedRevision)return fail("REVISION_MISMATCH_OR_UNKNOWN");
      if(request.expectedView!==undefined&&(!text(request.expectedView)||doc.suggestionsViewMode!==request.expectedView))return fail("VIEW_MISMATCH_OR_UNKNOWN");
      let nodes=0;
      const count=()=>{if(++nodes>maxInput)throw Error("INPUT_NODE_LIMIT");};
      const tabs=[];
      function visit(items,depth=0) {
        if(!Array.isArray(items)||depth>16)throw Error("TABS_SHAPE");
        for(const t of items) {
          count();if(!record(t))throw Error("TABS_SHAPE");
          if(text(t.tabId)&&record(t.body)) {
            if(t.tabProperties!==undefined||t.documentTab!==undefined)throw Error("MIXED_TAB_SHAPE");
            tabs.push({id:t.tabId,data:t,shape:"CONNECTOR_FLAT"});
          } else if(record(t.tabProperties)&&text(t.tabProperties.tabId)&&record(t.documentTab)) {
            tabs.push({id:t.tabProperties.tabId,data:t.documentTab,shape:"GOOGLE_NATIVE"});
            if(t.childTabs!==undefined)visit(t.childTabs,depth+1);
          } else throw Error("TABS_SHAPE");
        }
      }
      visit(doc.tabs);
      const matches=tabs.filter(t=>t.id===request.tabId);
      if(matches.length!==1)return fail("TAB_MISSING_OR_AMBIGUOUS");
      const tab=matches[0],atoms=[];let chars=0;
      if(doc.suggestionsViewMode!=null&&!["SUGGESTIONS_INLINE","PREVIEW_WITHOUT_SUGGESTIONS",
          "PREVIEW_SUGGESTIONS_ACCEPTED","DEFAULT_FOR_CURRENT_ACCESS","SUGGESTIONS_VIEW_MODE_UNSPECIFIED"].includes(doc.suggestionsViewMode))return fail("VIEW_SHAPE");
      function add(value,start,end,unsupported=false) {
        chars+=value.length;if(chars>2000000)throw Error("INPUT_TEXT_LIMIT");
        atoms.push({value,start,end,unsupported});
      }
      function body(content,depth=0,footnote=false) {
        if(!Array.isArray(content)||depth>16)throw Error("BODY_SHAPE");
        for(const item of content) {
          count();if(!record(item))throw Error("BODY_SHAPE");
          if(record(item.paragraph)&&Array.isArray(item.paragraph.elements)) {
            for(const e of item.paragraph.elements) {
              count();if(!record(e))throw Error("ELEMENT_SHAPE");
              if(record(e.textRun)&&typeof e.textRun.content==="string") {
                const value=e.textRun.content;utf8Length(value);
                if(e.startIndex!==undefined||e.endIndex!==undefined) {
                  if(!integer(e.startIndex)||!integer(e.endIndex)||e.endIndex-e.startIndex!==value.length)throw Error("NATIVE_INDEX_SHAPE");
                }
                add(value,e.startIndex,e.endIndex);
              } else if(record(e.footnoteReference)&&text(e.footnoteReference.footnoteId)&&!footnote) {
                const id=e.footnoteReference.footnoteId,notes=tab.data.footnotes||doc.footnotes;
                if(!record(notes)||!record(notes[id]))add("\uFFFC",e.startIndex,e.endIndex,true);
                else {add(" [footnote: ");body(notes[id].content,depth+1,true);add("] ");}
              } else add("\uFFFC",e.startIndex,e.endIndex,true);
            }
          } else if(record(item.table)&&Array.isArray(item.table.tableRows)) {
            add("\n");
            for(const row of item.table.tableRows) {
              count();if(!record(row)||!Array.isArray(row.tableCells))throw Error("TABLE_SHAPE");
              for(let i=0;i<row.tableCells.length;i++) {
                count();const cell=row.tableCells[i];if(!record(cell))throw Error("TABLE_SHAPE");
                if(i)add("\t");body(cell.content,depth+1,footnote);
              }
              add("\n");
            }
          } else if(record(item.sectionBreak)) { /* structural boundary with no selected text */ }
          else add("\uFFFC",item.startIndex,item.endIndex,true);
        }
      }
      body(tab.data.body&&tab.data.body.content);
      let joined="",spans=[];
      for(const a of atoms) {const from=joined.length;joined+=a.value;spans.push({...a,from,to:joined.length});}
      let from=0,to=joined.length;const sel=request.selector;
      if(sel.kind==="MARKER") {
        if(!text(sel.start)||!text(sel.end)||sel.start===sel.end||sel.start.includes("\n")||sel.end.includes("\n"))return fail("MARKER_INVALID");
        const occurrences=marker=>{
          const found=[];let position=0;
          for(const line of joined.split("\n")) {if(line.replace(/\r$/,"")===marker)found.push(position);position+=line.length+1;}
          return found;
        };
        const starts=occurrences(sel.start),ends=occurrences(sel.end);
        if(starts.length!==1||ends.length!==1||ends[0]<=starts[0])return fail("ORIGINAL_MARKERS_MISSING_OR_AMBIGUOUS");
        from=starts[0];to=ends[0]+sel.end.length;
      } else if(sel.kind==="UTF16_RANGE"||sel.kind==="NAMED_RANGE") {
        let start=sel.start,end=sel.end;
        if(sel.kind==="NAMED_RANGE") {
          if(!text(sel.namedRangeId))return fail("NAMED_RANGE_INVALID");
          const ranges=[];const tabScoped=Object.prototype.hasOwnProperty.call(tab.data,"namedRanges");
          const registry=tabScoped?tab.data.namedRanges:doc.namedRanges;
          if(!record(registry))return fail("NAMED_RANGE_MISSING_OR_AMBIGUOUS");
          for(const group of Object.values(registry)) {
            count();if(!record(group)||!Array.isArray(group.namedRanges))throw Error("NAMED_RANGE_SHAPE");
            for(const entry of group.namedRanges) {
              count();if(record(entry)&&entry.namedRangeId===sel.namedRangeId&&Array.isArray(entry.ranges))ranges.push(...entry.ranges);
            }
          }
          if(ranges.length!==1||!record(ranges[0])
              || !(ranges[0].tabId===request.tabId||(tabScoped&&ranges[0].tabId===undefined))
              || (ranges[0].segmentId!==undefined&&ranges[0].segmentId!==""))return fail("NAMED_RANGE_MISSING_OR_AMBIGUOUS");
          start=ranges[0].startIndex;end=ranges[0].endIndex;
        }
        if(sel.segmentId||!integer(start)||!integer(end)||start>=end)return fail("NATIVE_RANGE_INVALID");
        const selected=spans.filter(a=>integer(a.start)&&integer(a.end)&&a.end>start&&a.start<end);
        if(!selected.length)return fail("NATIVE_RANGE_NOT_COVERED");
        // Only directly mapped text runs qualify as an exact native range.
        if(selected.some(a=>a.unsupported||a.value.length!==a.end-a.start)||selected[0].start>start||selected.at(-1).end<end)return fail("NATIVE_RANGE_UNSUPPORTED_STRUCTURE");
        for(let i=1;i<selected.length;i++)if(selected[i-1].end!==selected[i].start)return fail("NATIVE_RANGE_GAP_OR_OVERLAP");
        from=selected[0].from+start-selected[0].start;to=selected.at(-1).to-(selected.at(-1).end-end);
        if(spans.some(a=>a.from>=from&&a.to<=to&&!integer(a.start)&&a.value!==""))return fail("NATIVE_RANGE_UNSUPPORTED_STRUCTURE");
      } else if(sel.kind!=="FULL_TAB")return fail("SELECTOR_UNSUPPORTED");
      if(spans.some(a=>a.unsupported&&a.to>from&&a.from<to))return fail("REQUIRED_NATIVE_STRUCTURE_UNSUPPORTED");
      const output=joined.slice(from,to);
      let outputBytes;try {outputBytes=utf8Length(output);}catch {return fail("SURROGATE_BOUNDARY_INVALID");}
      if(!output||outputBytes>maxOutput)return fail("SUFFICIENT_ORIGINAL_EXCEEDS_BUDGET_OR_EMPTY");
      return {profile:PROFILE,outcome:"NATIVE_PROJECTION_PREPARED",input_origin:"UNVERIFIED_PURE_API",stage:"PREPARED",pending:true,strong_reuse:false,
        body:output,source:{documentId:doc.documentId,tabId:request.tabId,
          revisionId:text(doc.revisionId)?doc.revisionId:null,revision_basis:text(doc.revisionId)?"OBSERVED_RESPONSE":"UNKNOWN",
          view:doc.suggestionsViewMode||"UNKNOWN",shape:tab.shape,selector_kind:sel.kind,projection:"native-text-table-footnote/v2"},
        counters:{projection_utf8_bytes:outputBytes,input_nodes:nodes},authorization_attestation:"NONE",
        protected_context:"UNKNOWN",end_access_lease:"UNPROVEN",receiver_use:"UNKNOWN"};
    } catch {return fail("NATIVE_DATA_SHAPE_OR_BOUND_FAILURE");}
  }
  async function readAndRelease(request, read, emit, options={}) {
    const clock=options.clock||Date.now;
    if(typeof clock!=="function")return fail("LOCAL_CLOCK_INVALID");
    let start;try {start=clock();}catch {return fail("LOCAL_CLOCK_INVALID");}
    if(!Number.isFinite(start))return fail("LOCAL_CLOCK_INVALID");
    const budget=options.budgetMs===undefined?null:options.budgetMs;
    if(typeof read!=="function"||typeof emit!=="function"||(budget!==null&&(!Number.isSafeInteger(budget)||budget<=0)))return fail("CALLSITE_INVALID");
    let clockFailed=false;
    const now=()=>{try {const value=clock();if(!Number.isFinite(value)||value<start)throw Error();return value;}
      catch {clockFailed=true;return Infinity;}};
    const late=()=>{const elapsed=now()-start;return clockFailed||(budget!==null&&elapsed>=budget);};
    let response;
    try {response=await read();}catch {return fail("READER_CALL_FAILED");}
    if(late())return fail("LOCAL_OPERATION_BUDGET_EXPIRED_AFTER_READ");
    const prepared=project(response,request);
    if(prepared.body===null)return prepared;
    if(late())return fail("LOCAL_OPERATION_BUDGET_EXPIRED_BEFORE_RELEASE");
    try {await emit(prepared.body,prepared.source);}catch {return {...fail("RELEASE_OUTCOME_UNKNOWN"),stage:"RELEASE_UNKNOWN"};}
    // No payload or persistence retained in the returned observation.
    return {...prepared,body:null,stage:"EMIT_CALL_COMPLETED",input_origin:"CALL_RESULT_ARGUMENT_THIS_INVOCATION",
      release_local_budget_status:late()?"COMPLETED_AFTER_LOCAL_BUDGET_OR_CLOCK_UNKNOWN_NO_LEASE_CLAIM":
        budget===null?"NO_LOCAL_BUDGET_CONFIGURED":"COMPLETED_WITHIN_OBSERVED_LOCAL_BUDGET",
      pending:true,receiver_use:"UNKNOWN",
      local_elapsed_ms:clockFailed?null:now()-start,local_budget_ms:budget,Google_check_time_attestation:"NONE"};
  }
  return Object.freeze({profile:PROFILE,project,readAndRelease});
})();
