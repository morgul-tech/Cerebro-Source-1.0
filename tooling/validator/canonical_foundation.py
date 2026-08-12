#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path
import yaml

def run(root:Path,rel,*args):
    p=root/rel
    cp=subprocess.run([sys.executable,'-B',str(p),*args],text=True,capture_output=True)
    try: out=json.loads(cp.stdout)
    except Exception: out={'result':'FAIL','raw':cp.stdout,'stderr':cp.stderr}
    return cp.returncode,out

def validate(root:Path):
    errors=[]; evidence=[]
    cds=yaml.safe_load((root/'standards/canonical-definition-system.yaml').read_text(encoding='utf-8'))['canonical_definition_system']
    if cds.get('version')!='0.2': errors.append('CANONICAL_DEFINITION_VERSION')
    desc=yaml.safe_load((root/'modules/terminology/canonical-descriptors.yaml').read_text(encoding='utf-8'))['descriptors']
    env=[d for d in desc if d.get('kind')=='LOCATION']
    if sorted(d.get('human_name') for d in env)!=sorted(['Temporaris','Singularity','Tranquility']): errors.append('ENVIRONMENT_NAME_SET_INVALID')
    if len(env)!=3: errors.append('ENVIRONMENT_NAME_COUNT_INVALID')
    ov=yaml.safe_load((root/'modules/terminology/canonical-overrides.yaml').read_text(encoding='utf-8'))['overrides']
    for k in ('dialog_engine','collaboration_engine'):
        if ov.get(k,{}).get('lifecycle')!='SUPERSEDED' or ov.get(k,{}).get('active_owner')!='interaction': errors.append('LEGACY_INTERACTION_OWNER_NOT_SUPERSEDED:'+k)
    for rel in ['engines/interaction/canonical_intent.py','tooling/validator/quality_trace.py','tooling/validator/assurance_continuity.py']:
        code,out=run(root,rel,'selftest','--source-root',str(root)) if 'canonical_intent' in rel else run(root,rel,'selftest')
        evidence.append({'validator':rel,'result':out.get('result')})
        if code!=0 or out.get('result')!='PASS': errors.append('SELFTEST_FAILED:'+rel)
    mp=yaml.safe_load((root/'mcp/manifest.yaml').read_text(encoding='utf-8'))
    if 'mcp/assurance-continuity.yaml' not in mp['mcp'].get('required_extensions',[]): errors.append('MCP_ASSURANCE_EXTENSION_NOT_REGISTERED')
    kernel=(root/'tooling/delivery/Cerebro.StandardDeliveryKernel.ps1').read_text(encoding='utf-8-sig')
    for token in ['BEFORE_MUTATION','BEFORE_PUBLICATION','BEFORE_COMPLETION_CLAIM','Invoke-AssuranceContinuityGate']:
        if token not in kernel: errors.append('KERNEL_CONTINUITY_WIRING_MISSING:'+token)
    return {'schema':'cerebro-c02-p001-foundation-validation/v0.2','result':'PASS' if not errors else 'FAIL','errors':errors,'evidence':evidence,'waves':['WAVE-A','WAVE-B','WAVE-C','WAVE-D','WAVE-E']}
def selftest(root:Path): return validate(root)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('command',choices=['selftest','validate']); ap.add_argument('--source-root',default=str(Path(__file__).resolve().parents[2])); a=ap.parse_args(); out=validate(Path(a.source_root)); print(json.dumps(out,indent=2)); return 0 if out['result']=='PASS' else 1
if __name__=='__main__': raise SystemExit(main())
