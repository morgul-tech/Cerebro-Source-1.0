#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, subprocess, sys
from pathlib import Path
import yaml

README_COUNT_LABELS = {
    'components': 'Komponenter',
    'rules': 'Regler',
    'terms': 'Autoritative begreper',
}
COMPONENT_INVENTORY = 'standards/source-component-inventory.yaml'

def active_yaml_paths(root:Path):
    return [
        path for path in sorted(root.rglob('*.yaml'))
        if 'history' not in {part.lower() for part in path.relative_to(root).parts}
    ]

def classify_components(root:Path):
    errors=[]
    inventory_path=root/COMPONENT_INVENTORY
    try:
        inventory=yaml.safe_load(inventory_path.read_text(encoding='utf-8-sig'))
    except (OSError,yaml.YAMLError) as exc:
        return {'stable':[],'candidates':[],'bounded_canaries':[],'all':[]},[
            'SOURCE_COMPONENT_INVENTORY_UNREADABLE:'+type(exc).__name__]
    if not isinstance(inventory,dict) or inventory.get('schema')!='cerebro-source-component-inventory/v1':
        return {'stable':[],'candidates':[],'bounded_canaries':[],'all':[]},['SOURCE_COMPONENT_INVENTORY_SCHEMA_INVALID']
    stable=inventory.get('stable_components')
    candidate=inventory.get('isolated_candidate_manifest')
    canaries=inventory.get('bounded_canaries')
    if (not isinstance(stable,dict) or not isinstance(candidate,dict) or not isinstance(canaries,dict)
            or set(candidate)!={'type','status','authority'}):
        return {'stable':[],'candidates':[],'bounded_canaries':[],'all':[]},['SOURCE_COMPONENT_INVENTORY_SHAPE_INVALID']
    if set(stable)&set(canaries): errors.append('SOURCE_COMPONENT_INVENTORY_CLASS_OVERLAP')
    paths=[p for p in sorted(root.rglob('component.yaml'))
           if 'history' not in {part.lower() for part in p.relative_to(root).parts}]
    found={p.relative_to(root).as_posix():p for p in paths}
    for rel in sorted((set(stable)|set(canaries))-set(found)):
        errors.append('SOURCE_COMPONENT_INVENTORY_MISSING:'+rel)
    classified={'stable':[],'candidates':[],'bounded_canaries':[],'all':sorted(found)}
    for rel,path in found.items():
        try:
            document=yaml.safe_load(path.read_text(encoding='utf-8-sig'))
        except (OSError,yaml.YAMLError):
            errors.append('SOURCE_COMPONENT_MANIFEST_UNREADABLE:'+rel); continue
        component=document.get('component') if isinstance(document,dict) else None
        if (not isinstance(document,dict) or document.get('schema')!='cerebro-component/v1'
                or not isinstance(component,dict) or not isinstance(component.get('id'),str)):
            errors.append('SOURCE_COMPONENT_MANIFEST_INVALID:'+rel); continue
        if rel in stable:
            if component['id']!=stable[rel]: errors.append('SOURCE_COMPONENT_INVENTORY_ID_DRIFT:'+rel)
            classified['stable'].append(rel)
        elif rel in canaries:
            expected=canaries[rel]
            if not isinstance(expected,dict) or any(component.get(k)!=v for k,v in expected.items()):
                errors.append('SOURCE_BOUNDED_CANARY_CLASS_DRIFT:'+rel)
            classified['bounded_canaries'].append(rel)
        elif all(component.get(k)==v for k,v in candidate.items()):
            classified['candidates'].append(rel)
        else:
            errors.append('SOURCE_COMPONENT_UNCLASSIFIED:'+rel)
    return classified,errors

def derive_source_counts(root:Path):
    errors=[]
    classified,component_errors=classify_components(root); errors.extend(component_errors)
    rule_count=0; rule_paths=[]
    for path in active_yaml_paths(root):
        try: document=yaml.safe_load(path.read_text(encoding='utf-8-sig'))
        except (OSError,yaml.YAMLError):
            errors.append('SOURCE_YAML_UNREADABLE:'+path.relative_to(root).as_posix()); continue
        if not isinstance(document,dict) or not isinstance(document.get('rules'),list): continue
        relative=path.relative_to(root).as_posix()
        rule_count+=len(document['rules']); rule_paths.append(relative)
    terms_document=yaml.safe_load((root/'modules/terminology/terms.yaml').read_text(encoding='utf-8-sig'))
    terms=terms_document.get('terms') if isinstance(terms_document,dict) else None
    if not isinstance(terms,dict): errors.append('TERMINOLOGY_TERMS_NOT_MAPPING')
    return {
        'components':len(classified['stable']),
        'rules':rule_count,
        'terms':len(terms) if isinstance(terms,dict) else 0,
    },errors,{
        'component_inventory':COMPONENT_INVENTORY,
        'component_paths':classified['all'],
        'stable_component_paths':classified['stable'],
        'candidate_component_paths':classified['candidates'],
        'bounded_canary_component_paths':classified['bounded_canaries'],
        'rule_paths':rule_paths,
        'terms_path':'modules/terminology/terms.yaml',
    }

def read_readme_counts(root:Path):
    lines=(root/'README.md').read_text(encoding='utf-8-sig').splitlines()
    counts={}; errors=[]
    for key,label in README_COUNT_LABELS.items():
        matches=[line for line in lines if re.match(r'^-\s*'+re.escape(label)+r'\s*:',line)]
        if not matches:
            errors.append('README_METADATA_COUNT_MISSING:'+key); continue
        if len(matches)!=1:
            errors.append('README_METADATA_COUNT_DUPLICATE:'+key); continue
        raw=matches[0].split(':',1)[1].strip()
        if not re.fullmatch(r'\d+',raw):
            errors.append('README_METADATA_COUNT_NON_NUMERIC:'+key); continue
        counts[key]=int(raw)
    return counts,errors

def validate_readme_counts(root:Path):
    derived,errors,inputs=derive_source_counts(root)
    declared,readme_errors=read_readme_counts(root); errors.extend(readme_errors)
    for key,value in derived.items():
        if key in declared and declared[key]!=value:
            errors.append(f'README_METADATA_COUNT_DRIFT:{key}:DECLARED={declared[key]}:DERIVED={value}')
    return errors,{'validator':'source_metadata_counts','result':'PASS' if not errors else 'FAIL','declared':declared,'derived':derived,'inputs':inputs}

def run(root:Path,rel,*args):
    p=root/rel
    cp=subprocess.run([sys.executable,'-B',str(p),*args],text=True,capture_output=True)
    try: out=json.loads(cp.stdout)
    except Exception: out={'result':'FAIL','raw':cp.stdout,'stderr':cp.stderr}
    return cp.returncode,out

def validate(root:Path):
    errors=[]; evidence=[]
    count_errors,count_evidence=validate_readme_counts(root)
    errors.extend(count_errors); evidence.append(count_evidence)
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
