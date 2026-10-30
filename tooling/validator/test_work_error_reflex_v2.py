"""Bounded real evaluate/retrieve consumers; no provider/target effects or recipient-use claim."""
from pathlib import Path
import copy
import hashlib
import importlib.util
import json
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[2]
def module(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
reflex=module('v2_reflex',ROOT/'tooling/return-bridge/work_error_reflex.py')
relevance=module('v2_relevance',ROOT/'tooling/context/relevance_engine.py')

class LearningV2(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'source'
        for name in ('tooling/context/relevance_engine.py',
                     'standards/development/implementation-learning-loop.yaml',
                     'standards/development/reflective-adjustment-loop.yaml',
                     'engines/context/knowledge.yaml','engines/context/working-context.yaml',
                     'engines/context/wisdom-evidence.yaml','engines/context/development-history.yaml'):
            destination=self.root/name;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/name,destination)
        self.task={k:'same-'+k for k in reflex.IDENTITY}
        self.task.update(task_ref='same-work',task_revision='r1',live_scope='ordinary candidate input decision')
        self.sha='a'*64;self.returned={**{k:self.task[k] for k in reflex.IDENTITY},'original_sha256':self.sha}
    def event(self,v2=True):
        event={'schema':reflex.SCHEMA_V2 if v2 else reflex.SCHEMA,'binding':dict(self.returned),
            'profile_binding_sha256':None,'trigger':'FIRST_UNEXPECTED_ERROR','observation_ref':'public:original',
            'attempt_ref':'same-attempt','reached_stage':'LOCAL_START','effect_state':'NONE_OBSERVED',
            'cause':{'state':'UNKNOWN','layer':'UNKNOWN','evidence_ref':None},
            'challenged_premise':{'ref':'public:premise','state':'CHALLENGED'},
            'inherited_countermodel_refs':[],
            'next_step':{'kind':'DIAGNOSE','justification_ref':'public:choice','proof_limit_ref':'public:limit','attempt_ref':'same-attempt','uses_premise_ref':None},
            'retry':None,'runtime_evidence':{'code':'UNKNOWN','startup':'UNKNOWN','operation':'UNKNOWN','cleanup':'UNKNOWN','evidence_refs':[]}}
        if v2:
            event['learning_context']={'schema':'cerebro-learning-context/v1',
                'sanitized_failure':'bounded read cap overflow sixth binding beyond limit',
                'challenged_premise':'exhausting a cap proves no candidates remain',
                'considered_decision':'retain overflow and partial coverage before next candidate selection',
                'epistemic_state':'HYPOTHESIS','contract_ref':'public:bounded-reader','edition_ref':'fixture-r1',
                'applicability':'bounded candidate reconciliation','original_ref':'public:legitimate-original-not-fetched',
                'tags':['bounded','overflow','coverage']}
        return event
    def consume(self,e):return reflex.evaluate(self.task,self.sha,None,self.returned,e,root=self.root)
    def catalog(self,change):
        p=self.root/'engines/context/knowledge.yaml';v=yaml.safe_load(p.read_text(encoding='utf8'));change(v['knowledge']['records']);p.write_text(yaml.safe_dump(v,sort_keys=False),encoding='utf8')
    def test_two_actual_errors_same_task_select_different_content(self):
        first=self.consume(self.event());e=self.event();e['learning_context'].update(
            sanitized_failure='handler output schema omitted ABSENT UNKNOWN enum legal states',
            challenged_premise='successful existing handler status fits schema',
            considered_decision='repair output enum without coercing UNKNOWN to success',
            applicability='handler output schema',tags=['handler','schema','absent'])
        second=self.consume(e)
        self.assertNotEqual(first['source_learning_encounter']['items'][0]['failure_family'],second['source_learning_encounter']['items'][0]['failure_family'])
        self.assertIn('BOUNDED_READ',first['source_learning_encounter']['items'][0]['failure_family'])
        self.assertEqual(second['source_learning_encounter']['items'][0]['failure_family'],'HANDLER_OUTPUT_SCHEMA_COMPLETENESS')
        self.assertNotEqual(first['learning_material'],second['learning_material'])
    def test_v1_output_preserves_exact_assessment_contract(self):
        e=self.event(False);result=self.consume(e)
        self.assertEqual(set(result),{'schema','binding','profile_binding_sha256','event','method_refs',
            'disposition','next_material_step_qualified','source_learning_encounter','solution_escalation',
            'assertion','authority','runtime_closed','reported_checkpoints','native_effect'})
        self.assertEqual(result['schema'],'cerebro-work-error-reflex/v1-assessment')
        self.assertEqual(result['event'],e);self.assertEqual(result['binding'],self.returned)
        self.assertEqual(result['disposition'],'OBSERVE_CHEAP_DISCRIMINATOR')
        for item in result['source_learning_encounter']['items']:
            self.assertNotIn('record_revision',item)
    def test_v1_rejects_v2_fields_and_v2_rejects_missing_or_extra(self):
        e=self.event();e['schema']=reflex.SCHEMA
        with self.assertRaises(ValueError):self.consume(e)
        for key in ('schema','sanitized_failure','tags'):
            e=self.event();del e['learning_context'][key]
            with self.assertRaises(ValueError):self.consume(e)
        e=self.event();e['learning_context']['grant']='admin'
        with self.assertRaises(ValueError):self.consume(e)
    def test_bounds_types_unicode_and_epistemic_unknown_preserved(self):
        for change in ({'sanitized_failure':'x'*2049},{'tags':['a']*17},{'tags':['same','same']},{'edition_ref':42},{'epistemic_state':'PROVEN'},{'sanitized_failure':'\ud800'}):
            e=self.event();e['learning_context'].update(change)
            with self.assertRaises((ValueError,TypeError)):self.consume(e)
        e=self.event();e['learning_context'].update(epistemic_state='UNKNOWN',challenged_premise='UNKNOWN',considered_decision='UNKNOWN')
        result=self.consume(e);self.assertEqual(result['event']['cause']['state'],'UNKNOWN');self.assertFalse(result['runtime_closed']);self.assertEqual(result['learning_material']['learning_context']['epistemic_state'],'UNKNOWN')
    def test_direct_current_family_wins_over_surface_score(self):
        e=self.event();e['inherited_countermodel_refs']=['HANDLER_OUTPUT_SCHEMA_COMPLETENESS']
        r=self.consume(e);self.assertEqual(r['source_learning_encounter']['items'][0]['failure_family'],'HANDLER_OUTPUT_SCHEMA_COMPLETENESS')
    def test_expired_refuted_edition_or_contract_mismatch_cannot_be_direct(self):
        family='HANDLER_OUTPUT_SCHEMA_COMPLETENESS'
        def mutate(records):
            for row in records:
                if row['actor_encounter']['failure_family']==family:row.update(validity={'state':'EXPIRED'})
        self.catalog(mutate);e=self.event();e['inherited_countermodel_refs']=[family]
        self.assertNotIn(family,[x['failure_family'] for x in self.consume(e)['source_learning_encounter']['items']])
        for kind in ('refuted','edition','contract','revoked'):
            shutil.copyfile(ROOT/'engines/context/knowledge.yaml',self.root/'engines/context/knowledge.yaml')
            def mutate(rows):
                for row in rows:
                    if row['actor_encounter']['failure_family']!=family:continue
                    if kind=='refuted':row['contradiction']['state']='REFUTED'
                    elif kind=='revoked':row['relations']={'revoked_by_ref':'public:correction'}
                    else:row['validity'][kind+'_refs']=['different-'+kind]
            self.catalog(mutate)
            self.assertNotIn(family,[x['failure_family'] for x in self.consume(e)['source_learning_encounter']['items']])
    def test_irrelevant_task_title_or_unmatched_reference_is_not_learning(self):
        e=self.event();e['learning_context'].update(sanitized_failure='zzqqxy',challenged_premise='zzqqxy',considered_decision='zzqqxy',applicability='zzqqxy',tags=[])
        e['inherited_countermodel_refs']=['https://unread.invalid/no-remote-read']
        self.assertEqual(self.consume(e)['source_learning_encounter']['items'],[])
    def test_current_direct_ref_retains_counterevidence_and_original_edition(self):
        e=self.event();e['inherited_countermodel_refs']=['BOUNDED_READ_OVERFLOW_COVERAGE'];r=self.consume(e);item=r['source_learning_encounter']['items'][0]
        self.assertTrue(item['counterevidence']);self.assertEqual(item['validity']['state'],'CURRENT');self.assertTrue(item['source_revision']);self.assertEqual(len(item['record_revision']),64)
        self.assertEqual(r['learning_material']['binding'],e['binding']);self.assertEqual(r['learning_material']['authority'],'EVIDENCE_ONLY')
    def test_v2_material_and_encounter_counts_bind_complete_items(self):
        r=self.consume(self.event());enc=r['learning_material']['source_learning_encounter']
        self.assertEqual(enc['projection_utf8_bytes'],sum(len(reflex.canonical(item)) for item in enc['items']))
        self.assertLessEqual(len(reflex.canonical(enc)),16384);self.assertLessEqual(len(reflex.canonical(r['learning_material'])),32768)
        for item in enc['items']:self.assertEqual(set(item['validity']),{'state','as_of','scope'})
    def test_cheap_known_and_transient_routes_never_open_relevance(self):
        e=self.event();e['cause']={'state':'PROVEN','layer':'LOCAL_ENVIRONMENT','evidence_ref':'public:exact-defect'};e['next_step']['kind']='QUICK_FIX'
        with patch.object(reflex,'module',side_effect=AssertionError('lookup on cheap path')):
            r=self.consume(e)
        self.assertEqual(r['disposition'],'DIRECT_KNOWN_FIX');self.assertIsNone(r['learning_material'])
        e=self.event();e['next_step']['kind']='BOUNDED_RETRY';e['retry']={'contract_ref':'public:transient','evidence_ref':'public:evidence','qualified':True,'idempotent':True,'attempt_number':1,'max_attempts':2,'expires_at':time.time()+60,'failure_class':'KNOWN_TRANSIENT'}
        with patch.object(reflex,'module',side_effect=AssertionError('lookup on cheap path')):r=self.consume(e)
        self.assertEqual(r['disposition'],'QUALIFIED_BOUNDED_TRANSIENT_RETRY');self.assertIsNone(r['learning_material'])
    def test_unknown_effect_still_blocks_qualification_and_falsified_premise_refused(self):
        e=self.event();e['effect_state']='UNKNOWN';r=self.consume(e);self.assertFalse(r['next_material_step_qualified']);self.assertEqual(r['disposition'],'RECONCILE_SAME_ATTEMPT')
        e=self.event();e['challenged_premise']['state']='FALSIFIED';e['next_step']['uses_premise_ref']=e['challenged_premise']['ref']
        with self.assertRaisesRegex(ValueError,'FALSIFIED_PREMISE'):self.consume(e)
    def test_catalogue_change_changes_bound_material_not_legacy_original(self):
        e=self.event();before=json.dumps(e,sort_keys=True);first=self.consume(e)
        self.catalog(lambda records:records[0]['tags'].append('new-admitted-tag'))
        second=self.consume(e);self.assertNotEqual(first['source_learning_encounter']['source_catalog_sha256'],second['source_learning_encounter']['source_catalog_sha256']);self.assertEqual(before,json.dumps(e,sort_keys=True))
    def test_material_bound_is_not_only_projection_item_size(self):
        e=self.event();context=reflex.validate_learning_context(e['learning_context'])
        with self.assertRaisesRegex(ValueError,'MATERIAL_BOUND'):
            reflex.learning_material(e,context,{'untrusted_oversize':'x'*32768})
        e['learning_context']['original_ref']='file:/not-readable-or-fetched'
        self.assertEqual(self.consume(e)['learning_material']['learning_context']['original_ref'],e['learning_context']['original_ref'])

if __name__=='__main__':unittest.main()
