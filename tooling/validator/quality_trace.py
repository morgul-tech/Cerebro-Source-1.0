#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json
STAGES=['UNDERSTAND_FRAME','EXPLORE_RESEARCH','REFINE','CRITIQUE','COMPARE_CONVERGE','DECIDE','EXECUTE_GENERATE','VERIFY','LEARN']
REQ={'LIGHT':['UNDERSTAND_FRAME','EXECUTE_GENERATE','VERIFY'],'STANDARD':['UNDERSTAND_FRAME','EXPLORE_RESEARCH','REFINE','CRITIQUE','COMPARE_CONVERGE','EXECUTE_GENERATE','VERIFY'],'DEEP':STAGES}
def fp(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def new(work_item,depth,basis):
    return {'schema':'cerebro-quality-trace/v0.2','work_item_ref':work_item,'required_depth':depth,'basis_fingerprint':basis,'stages':{s:{'state':'PENDING','basis_fingerprint':basis,'evidence_refs':[]} for s in STAGES},'overall_assurance':'IN_PROGRESS'}
def pass_stage(trace,stage,basis,evidence):
    if stage not in STAGES: raise ValueError('UNKNOWN_STAGE')
    if basis!=trace['basis_fingerprint']: raise ValueError('STALE_BASIS')
    if not evidence: raise ValueError('PASS_REQUIRES_EVIDENCE')
    trace['stages'][stage]={'state':'PASS','basis_fingerprint':basis,'evidence_refs':sorted(set(evidence))}
    req=REQ[trace['required_depth']]
    trace['overall_assurance']='PASS' if all(trace['stages'][s]['state']=='PASS' for s in req) else 'IN_PROGRESS'
    return trace
def rebase(trace,new_basis):
    if new_basis==trace['basis_fingerprint']: return trace
    trace['basis_fingerprint']=new_basis
    for s in STAGES:
        if trace['stages'][s]['state']=='PASS': trace['stages'][s]['state']='STALE'
        trace['stages'][s]['basis_fingerprint']=new_basis
    trace['overall_assurance']='STALE'; return trace
def selftest():
    tests=[]
    def c(n,v): tests.append({'name':n,'result':'PASS' if v else 'FAIL'})
    b=fp({'x':1}); t=new('X','DEEP',b)
    try: pass_stage(t,'REFINE',b,[]); noev=False
    except ValueError: noev=True
    c('pass-without-evidence-rejected',noev)
    pass_stage(t,'REFINE',b,['E1']); c('evidence-bound-pass-accepted',t['stages']['REFINE']['state']=='PASS')
    rebase(t,fp({'x':2})); c('basis-change-invalidates-pass',t['stages']['REFINE']['state']=='STALE' and t['overall_assurance']=='STALE')
    return {'schema':'cerebro-quality-trace-selftest/v0.2','result':'PASS' if all(x['result']=='PASS' for x in tests) else 'FAIL','tests':tests}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('command',choices=['selftest']); a=ap.parse_args(); out=selftest(); print(json.dumps(out,indent=2)); return 0 if out['result']=='PASS' else 1
if __name__=='__main__': raise SystemExit(main())
