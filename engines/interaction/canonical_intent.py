#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
import yaml
OPS={"AND","OR","NOT","THEN","IF","ELSE"}
BINARY={"AND","OR","THEN"}
TOKEN=re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|\?|\b(?:AND|OR|NOT|THEN|IF|ELSE)\b|[^\s()]+')

def tokenize(text:str):
    return TOKEN.findall(text)

def _top_level_ops(tokens):
    depth=0; ops=[]
    for t in tokens:
        if t=='(': depth+=1
        elif t==')':
            depth-=1
            if depth<0: raise ValueError('UNBALANCED_GROUPING')
        elif depth==0 and t in OPS: ops.append(t)
    if depth!=0: raise ValueError('UNBALANCED_GROUPING')
    return ops

def parse(text:str):
    tokens=tokenize(text.strip())
    if not tokens: return {'mode':'NATURAL_LANGUAGE','result':'PASS','tokens':[]}
    # lowercase operator-looking words are ordinary natural language.
    canonical_signal=any(t in OPS or t in {'(',')','?'} for t in tokens)
    if not canonical_signal: return {'mode':'NATURAL_LANGUAGE','result':'PASS','tokens':tokens}
    if tokens[-1]=='?':
        if any(t in OPS for t in tokens[:-1]): raise ValueError('INTROSPECTION_MUST_BE_READ_ONLY_QUERY')
        return {'mode':'INTROSPECTION','result':'PASS','subject':' '.join(tokens[:-1]).strip() or 'ROOT'}
    ops=_top_level_ops(tokens)
    fam={x for x in ops if x in BINARY}
    if len(fam)>1: raise ValueError('EXPLICIT_GROUPING_REQUIRED_FOR_MIXED_OPERATOR_FAMILIES')
    if 'IF' in ops and 'THEN' not in ops: raise ValueError('INVALID_CONDITIONAL_EXPRESSION')
    if 'ELSE' in ops and not ('IF' in ops and 'THEN' in ops): raise ValueError('INVALID_CONDITIONAL_EXPRESSION')
    if tokens[0] in BINARY or tokens[-1] in OPS: raise ValueError('INVALID_CANONICAL_EXPRESSION')
    return {'mode':'CANONICAL_SYNTAX','result':'PASS','tokens':tokens,'top_level_operators':ops,'partial_execution_allowed':False}

def load_descriptors(root:Path):
    doc=yaml.safe_load((root/'modules/terminology/canonical-descriptors.yaml').read_text(encoding='utf-8'))
    return doc.get('descriptors',[])

def introspect(root:Path, subject:str):
    desc=load_descriptors(root)
    s=subject.strip().casefold()
    if s in {'','root'}:
        return {'categories':['terms','commands','operators','capabilities','locations','changes','version_axes']}
    kind_map={'operators':'OPERATOR','capabilities':'CAPABILITY','locations':'LOCATION','changes':'CHANGE','version_axes':'VERSION_AXIS'}
    if s in kind_map:
        return {'subject':subject,'descriptors':[d for d in desc if d.get('kind')==kind_map[s]]}
    hits=[]
    for d in desc:
        names=[d.get('identity'),d.get('canonical_name'),d.get('human_name'),*(d.get('aliases') or [])]
        if any(str(x).casefold()==s for x in names if x): hits.append(d)
    return {'subject':subject,'descriptors':hits}

def selftest(root:Path):
    tests=[]
    def check(name,fn):
        try: ok=bool(fn())
        except Exception: ok=False
        tests.append({'name':name,'result':'PASS' if ok else 'FAIL'})
    check('uppercase-and-accepted',lambda: parse('(A AND B)')['result']=='PASS')
    check('lowercase-and-natural',lambda: parse('A and B')['mode']=='NATURAL_LANGUAGE')
    check('quoted-operator-is-literal',lambda: parse('say "A AND B"').get('mode')=='NATURAL_LANGUAGE' or 'AND' not in parse('say "A AND B"').get('top_level_operators',[]))
    def mixed_rejected():
        try: parse('A AND B THEN C')
        except ValueError as e: return 'EXPLICIT_GROUPING_REQUIRED' in str(e)
        return False
    check('mixed-operator-family-rejected-without-grouping',mixed_rejected)
    check('grouped-mixed-expression-accepted',lambda: parse('(A AND B) THEN C')['result']=='PASS')
    check('question-mark-read-only',lambda: parse('Singularity ?')['mode']=='INTROSPECTION')
    check('three-environment-descriptors',lambda: len([d for d in load_descriptors(root) if d.get('kind')=='LOCATION' and d.get('human_name') in {'Temporaris','Singularity','Tranquility'}])==3)
    return {'schema':'cerebro-canonical-intent-selftest/v0.2','result':'PASS' if all(t['result']=='PASS' for t in tests) else 'FAIL','tests':tests}

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('parse'); p.add_argument('text')
    p=sub.add_parser('introspect'); p.add_argument('subject'); p.add_argument('--source-root',default=str(Path(__file__).resolve().parents[2]))
    p=sub.add_parser('selftest'); p.add_argument('--source-root',default=str(Path(__file__).resolve().parents[2]))
    a=ap.parse_args()
    try:
        if a.cmd=='parse': out=parse(a.text)
        elif a.cmd=='introspect': out=introspect(Path(a.source_root),a.subject)
        else: out=selftest(Path(a.source_root))
        print(json.dumps(out,indent=2,ensure_ascii=True)); return 0 if out.get('result','PASS')=='PASS' else 1
    except Exception as e:
        print(json.dumps({'result':'BLOCK','error':str(e)})); return 1
if __name__=='__main__': raise SystemExit(main())
