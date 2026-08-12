#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
import yaml
STAGES={'BEFORE_ARTIFACT_DESIGN','BEFORE_HUMAN_HANDOFF','BEFORE_MUTATION','BEFORE_PUBLICATION','BEFORE_COMPLETION_CLAIM'}
def validate(manifest,stage,evidence=None):
    errors=[]
    if stage not in STAGES: errors.append('UNKNOWN_CONTINUITY_BOUNDARY')
    if manifest.get('delivery_profile')!='STANDARD': errors.append('GOVERNING_STANDARD_PROFILE_MISSING')
    b=manifest.get('delivery_control_binding') or {}
    if b.get('resolved_profile')!='STANDARD' or b.get('requested_profile')!='STANDARD': errors.append('SEALED_STANDARD_CONTROL_BINDING_MISSING')
    h=manifest.get('human_execution_handoff') or {}
    if not h.get('required') or h.get('profile')!='HASH_BOUND_POWERSHELL': errors.append('STANDARD_HANDOFF_ASSURANCE_MISSING')
    if stage=='BEFORE_COMPLETION_CLAIM':
        e=evidence or {}
        if e.get('source_equality')!='VERIFIED': errors.append('SOURCE_EQUALITY_NOT_VERIFIED')
        if e.get('working_tree')!='CLEAN': errors.append('WORKING_TREE_NOT_CLEAN')
        if e.get('cerebro_sync_verified') is not True: errors.append('CEREBRO_SYNC_NOT_VERIFIED')
    return {'schema':'cerebro-assurance-continuity-validation/v0.2','result':'PASS' if not errors else 'BLOCK','stage':stage,'errors':errors,'governing_profile':'STANDARD'}
def selftest():
    m={'delivery_profile':'STANDARD','delivery_control_binding':{'requested_profile':'STANDARD','resolved_profile':'STANDARD'},'human_execution_handoff':{'required':True,'profile':'HASH_BOUND_POWERSHELL'}}
    tests=[]
    def c(n,v): tests.append({'name':n,'result':'PASS' if v else 'FAIL'})
    c('standard-before-mutation-pass',validate(m,'BEFORE_MUTATION')['result']=='PASS')
    bad=json.loads(json.dumps(m)); bad['delivery_profile']='LIMITED'; c('profile-drop-blocked',validate(bad,'BEFORE_MUTATION')['result']=='BLOCK')
    c('completion-without-evidence-blocked',validate(m,'BEFORE_COMPLETION_CLAIM')['result']=='BLOCK')
    ev={'source_equality':'VERIFIED','working_tree':'CLEAN','cerebro_sync_verified':True}; c('verified-completion-accepted',validate(m,'BEFORE_COMPLETION_CLAIM',ev)['result']=='PASS')
    return {'schema':'cerebro-assurance-continuity-selftest/v0.2','result':'PASS' if all(t['result']=='PASS' for t in tests) else 'FAIL','tests':tests}
def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('selftest')
    p=sub.add_parser('validate'); p.add_argument('--manifest',required=True); p.add_argument('--stage',required=True); p.add_argument('--evidence')
    a=ap.parse_args()
    if a.cmd=='selftest': out=selftest()
    else:
        m=json.load(open(a.manifest,encoding='utf-8')); e=json.load(open(a.evidence,encoding='utf-8')) if a.evidence else None; out=validate(m,a.stage,e)
    print(json.dumps(out,indent=2)); return 0 if out['result']=='PASS' else 1
if __name__=='__main__': raise SystemExit(main())
