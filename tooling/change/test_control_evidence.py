"""Bounded A7 evidence-binding journey. No target/SSH/private data or historical rerun.

Run with the selected interpreter, PyYAML and jsonschema on its ordinary import path.
The counterexamples are sanitized synthetic models attributed in COUNTEREXAMPLES.json;
they prove the binding/claim ceiling, not the VPS qualifier or target state.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('change_engine_under_test', HERE / 'change_engine.py')
engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine
spec.loader.exec_module(engine)


def counterexamples():
    # r9: distinguish the concrete diagnostic reader mismatch from unknown primary cause.
    writer = 'kind|stage|message|status|payload'.split('|')
    assert len(writer) == 5 and len(writer) != 4
    assert dict(zip(('kind', 'stage', 'message', 'status', 'payload'), writer))['payload'] == 'payload'
    # r14: equal held fixtures cannot demonstrate historical inventory completeness.
    held = {'known.sources': 'known body'}
    current = {'known.sources': 'known body', 'unseen.sources': 'unseen body'}
    assert all(current[path] == body for path, body in held.items())
    historical_complete = False
    unknown_current = set(current) - set(held)
    assert not historical_complete and unknown_current
    # Neither invent target drift nor authorize the unrecorded current source.
    verdict = {'target_changed': 'UNKNOWN', 'unseen_source_admitted': False}
    assert verdict['target_changed'] == 'UNKNOWN' and not verdict['unseen_source_admitted']
    # r15: successful inventory continuity cannot satisfy a separate signer/scope need.
    inventory_ok, signer_qualified = True, False
    assert inventory_ok and not signer_qualified
    return {
        'r9-format': {'writer_fields': 5, 'legacy_reader_fields': 4, 'primary_runtime_cause': 'UNKNOWN'},
        'r14-prefix': verdict | {'known_continuity': True, 'historical_inventory_complete': False},
        'r15-signer': {'inventory_ok': True, 'signer_qualified': False, 'target_effect': 'NOT_PROVEN'},
    }


class EvidenceJourney(unittest.TestCase):
    def test_connected_journey(self):
        import jsonschema
        results = counterexamples()
        artifacts = []
        with tempfile.TemporaryDirectory(prefix='a7-evidence-') as tmp:
            root = Path(tmp); repo = root / 'repo'; capsule = root / 'capsule'
            repo.mkdir(); (capsule / 'payload').mkdir(parents=True)
            for args in [('init',), ('config','user.name','A7 Local Fixture'),
                         ('config','user.email','a7@example.invalid'),
                         ('config','core.autocrlf','false'),
                         ('remote','add','origin','https://github.com/morgul-tech/Cerebro-Source-1.0.git')]:
                engine.git(repo, *args)
            (repo / 'fixture.json').write_text('{"stage":"before"}\n', encoding='utf-8', newline='\n')
            engine.git(repo,'add','fixture.json'); engine.git(repo,'commit','-m','local fixture baseline')
            payload = capsule / 'payload' / 'fixture.json'
            payload.write_text(json.dumps(results, sort_keys=True)+'\n', encoding='utf-8', newline='\n')
            manifest = {'schema':engine.SCHEMA_ID,
                'change':{'id':'A7-CONTROL-EVIDENCE-FIXTURE','title':'Sanitized r9/r14/r15 binding'},
                'authority':{'repository':'morgul-tech/Cerebro-Source-1.0','branch':'main',
                             'base_commit':engine.git(repo,'rev-parse','HEAD')},
                'assurance':{'profile':'FAST','control_needs':[
                    {'control_id':key,'reason':'Bounded changed-risk counterexample',
                     'context':{'kind':'SANITIZED_SYNTHETIC_ONLY','fixture_sha256':engine.sha256_file(Path(__file__)),
                                'target_effect':'NOT_PROVEN'}} for key in results] + [
                    {'control_id':'git-diff-check','reason':'Local diff hygiene',
                     'context':{'execution_scope':'LOCAL_CANDIDATE'}}]},
                'files':[{'path':'fixture.json','operation':'replace','payload':'payload/fixture.json',
                          'sha256':engine.sha256_file(payload),
                          'baseline':{'state':'present','git_blob_sha':engine.git(repo,'rev-parse','HEAD:fixture.json')}}]}
            jsonschema.validate(manifest,json.loads((HERE/'capsule-schema.json').read_text()))
            def cli(label, expected):
                (capsule/'capsule.json').write_text(json.dumps(manifest),encoding='utf-8')
                report_path=root/(label+'.json'); knowledge=root/(label+'-knowledge.json')
                env=os.environ.copy()
                env.pop('CEREBRO_ACTIVE_DIAGNOSTIC_CAPSULE',None)
                env.pop('CEREBRO_DIAGNOSTIC_CONTEXT_STATUS',None)
                result=subprocess.run([sys.executable,'-B',str(HERE/'change_engine.py'),'test',
                    '--capsule-root',str(capsule),'--repository-root',str(repo),'--profile','FAST',
                    '--report',str(report_path),'--knowledge',str(knowledge)],capture_output=True,text=True,env=env)
                self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
                report=json.loads(report_path.read_text())
                jsonschema.validate(json.loads(knowledge.read_text()),json.loads((HERE/'failure-record-schema.json').read_text()))
                artifacts.append({'case':label,'exit_code':result.returncode,'report':report})
                return report
            missing=cli('selected-names-uncovered',2)
            self.assertTrue(missing['control_coverage']['uncovered'])
            self.assertFalse(missing['ready_to_lock'])
            self.assertEqual(len(missing['control_coverage']['executed']),6,missing['runs'])
            self.assertTrue(missing['applicable_regressions']) # broad JSON traits are advisory

            subject=engine.control_subject(manifest)
            descriptors=[]
            for need in manifest['assurance']['control_needs']:
                if need['control_id'] not in results:
                    continue
                record={'schema':engine.CONTROL_EVIDENCE_SCHEMA,'control_id':need['control_id'],
                    'candidate_fingerprint':subject,'context':need['context'],'result':'PASS','executed':True,
                    'execution_ref':'test_control_evidence.py:counterexamples:'+need['control_id'],
                    'producer':{'id':'tooling/change/test_control_evidence.py',
                                'sha256':engine.sha256_file(Path(__file__))},
                    'observations':results[need['control_id']],
                    'proof_ceiling':'SYNTHETIC_COUNTEREXAMPLE_ASSERTIONS_ONLY_NOT_TARGET_QUALIFICATION'}
                path=capsule/(need['control_id']+'.json'); path.write_text(json.dumps(record),encoding='utf-8')
                descriptors.append({'path':path.name,'sha256':engine.sha256_file(path),
                    'reuse_basis':'Same executed local fixture, exact candidate/context; no target claim'})
            manifest['assurance']['control_evidence']=descriptors
            covered=cli('valid-reuse',0)
            self.assertTrue(covered['ready_to_lock'])
            self.assertEqual({n['state'] for n in covered['control_coverage']['selected_needs']},{'REUSED_VALID','EXECUTED'})
            self.assertFalse(covered['control_coverage']['uncovered'])
            self.assertNotEqual(missing['learning_basis_fingerprint'],covered['learning_basis_fingerprint'])

            # Remaining binding checks reuse the same executed evidence, without campaign reruns.
            def reconcile(m): return engine.reconcile_control_evidence(capsule,m,[])
            stale=copy.deepcopy(manifest); stale['files'][0]['sha256']='a'*64
            self.assertEqual(len(reconcile(stale)['uncovered']),4)
            context=copy.deepcopy(manifest); context['assurance']['control_needs'][0]['context']['kind']='TARGET'
            self.assertTrue(reconcile(context)['uncovered'])
            tamper=copy.deepcopy(manifest); tamper['assurance']['control_evidence'][0]['sha256']='0'*64
            self.assertIn('r9-format',reconcile(tamper)['uncovered'])
            escape=copy.deepcopy(manifest); escape['assurance']['control_evidence'][0]['path']='../outside.json'
            self.assertTrue(reconcile(escape)['rejected'])
            bad=capsule/'bad.json'; bad.write_text('[]')
            malformed=copy.deepcopy(manifest)
            malformed['assurance']['control_evidence'][0]={'path':bad.name,'sha256':engine.sha256_file(bad),'reuse_basis':'negative fixture'}
            self.assertTrue(reconcile(malformed)['rejected'])
            name_only=copy.deepcopy(manifest)
            bad.write_text(json.dumps({'control_id':'r9-format','result':'PASS'}))
            name_only['assurance']['control_evidence'][0]={'path':bad.name,'sha256':engine.sha256_file(bad),'reuse_basis':'negative fixture'}
            self.assertIn('r9-format',reconcile(name_only)['uncovered'])
            duplicate=copy.deepcopy(manifest); duplicate['assurance']['control_needs'].append(duplicate['assurance']['control_needs'][0])
            with self.assertRaises(engine.ChangeError): reconcile(duplicate)
            no_needs=copy.deepcopy(manifest); no_needs['assurance']={'profile':'FAST'}
            self.assertFalse(reconcile(no_needs)['uncovered'])
            self.assertEqual(covered['control_coverage']['selected_needs'][-1]['state'],'EXECUTED')
            legacy=root/'legacy.json'; legacy.write_text(json.dumps({'schema':engine.LEGACY_KNOWLEDGE_SCHEMA,'generation':1,'events':[],'families':{}}))
            jsonschema.validate(engine.load_knowledge(legacy),json.loads((HERE/'failure-record-schema.json').read_text()))
            self.assertEqual(engine.git(repo,'status','--porcelain'),'')
            self.assertEqual(len(engine.git(repo,'worktree','list','--porcelain').split('worktree '))-1,1)
        output=os.environ.get('A7_FIXTURE_OUTPUT')
        if output:
            Path(output).write_text(json.dumps({'result':'PASS','proof_ceiling':'LOCAL_SYNTHETIC_ONLY',
                'counterexamples':results,'cli_journey':artifacts,'additional_checks':[
                'stale-candidate','wrong-context','tampered-hash','escaped-path','malformed-receipt',
                'name-only-receipt','duplicate-need','no-universal-catalog-gate','executed-vs-reused','legacy-knowledge',
                'clean-source-and-worktree-cleanup']},indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    unittest.main(verbosity=2)
