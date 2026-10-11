"""Changed-risk checks only. Synthetic offline fixtures; no network or old-suite rerun."""
import dataclasses, json, os, subprocess, sys, unittest
from pathlib import Path
from context_slice_support import TmpRoot, request, ORIGINAL, SECRET
from signalvev_sensing.context_slice import (ResultEnvelopeUnavailable, FALLBACK_REQUIRED, SLICE_READY,
                                            ContextSliceResolver)
import test_context_slice_owner as composition_fixture
from test_context_slice_owner import FULL_SHA, OWNER
from fixtures import raw_event
from signalvev_sensing import ResolveRequest, ResolverUnavailable

class EnvelopeAndDeadline(TmpRoot):
    def test_impossible_caps_fail_before_any_provider_or_content_for_all_entrypoints(self):
        for cap in (1, 1024):
            for entry in ('slice', 'metadata', 'full'):
                with self.subTest(cap=cap,entry=entry):
                    req=request(limits={'max_result_bytes':cap})
                    out=self.dir/f'{cap}-{entry}.bin'
                    before=self.provider.stats.describes
                    with self.assertRaises(ResultEnvelopeUnavailable):
                        if entry=='slice': self.resolver().resolve(req)
                        elif entry=='metadata': self.resolver().describe_only(req)
                        else: self.resolver().full_original(req,max_full_output_bytes=131072,out_path=out)
                    self.assertFalse(out.exists())
                    self.assertEqual(self.provider.stats.content_opens,0)
                    self.assertLessEqual(self.provider.stats.describes-before,1)

    def test_every_outcome_serializes_within_usable_cap(self):
        cases=[request(),request(subject_ref='actor:other'),request(expected_revision='old'),
               request(need='COMPLETE_ORIGINAL')]
        for req in cases:
            with self.subTest(req=req):
                req=dataclasses.replace(req,limits=dataclasses.replace(req.limits,max_result_bytes=4096))
                res=self.resolver().resolve(req)
                self.assertLessEqual(len(res.to_json()),4096)
                self.assertNotIn(SECRET,res.to_json().decode())
                self.assertEqual(res.request['content_ref'],req.content_ref)
        req=request(limits={'max_result_bytes':4096})
        meta,d=self.resolver().describe_only(req)
        self.assertIsNotNone(d);self.assertLessEqual(len(meta.to_json()),4096)
        full=self.resolver().full_original(req,max_full_output_bytes=131072,out_path=self.dir/'full.bin')
        self.assertEqual(full.outcome,'FULL_ORIGINAL_READY')
        self.assertLessEqual(len(full.to_json()),4096)
        self.assertEqual((self.dir/'full.bin').read_bytes(),ORIGINAL)

    def test_impossible_provenance_or_fallback_is_not_opened_or_truncated(self):
        req=request(limits={'max_result_bytes':1500})
        with self.assertRaises(ResultEnvelopeUnavailable):self.resolver().resolve(req)
        self.assertEqual(self.provider.stats.content_opens,0)
        original=self.provider.describe_authorized
        self.provider.describe_authorized=lambda *a,**kw:dataclasses.replace(original(*a,**kw),assertion='x'*5000)
        req=request()
        with self.assertRaises(ResultEnvelopeUnavailable):self.resolver().resolve(req)
        self.assertEqual(self.provider.stats.content_opens,0)

    def test_serialization_rechecks_after_mutation(self):
        res=self.resolver().resolve(request())
        self.assertEqual(res.outcome,SLICE_READY)
        res.reason='x'*5000
        with self.assertRaises(ResultEnvelopeUnavailable):res.to_json()

    def test_oversized_success_falls_back_with_identity_and_no_content(self):
        req=request(selector={'start':0,'end':1024,'expected_slice_sha256':__import__('hashlib').sha256(ORIGINAL[:1024]).hexdigest()},limits={'max_result_bytes':2600})
        res=self.resolver().resolve(req)
        self.assertEqual(res.outcome,FALLBACK_REQUIRED)
        self.assertIsNone(res.slice_text)
        self.assertLessEqual(len(res.to_json()),2600)
        self.assertEqual((res.fallback['canonical_ref'],res.way_home),(req.content_ref,req.way_home))
        self.assertFalse(res.currentness['receiver_acceptable'])

    def test_cli_impossible_cap_emits_zero_stdout(self):
        doc=json.loads((Path(__file__).parent/'context_slice_fixtures/request.json').read_bytes())
        doc['limits']['max_result_bytes']=1024
        reqfile=self.dir/'request.json';reqfile.write_text(json.dumps(doc))
        env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')}
        for mode in ('resolve','full-original'):
            cp=subprocess.run([sys.executable,'-m','signalvev_sensing.context_slice_cli',mode,'--registry',str(self.registry_path),'--root',str(self.root),'--request',str(reqfile)],capture_output=True,env=env)
            self.assertEqual(cp.returncode,2,cp.stderr)
            self.assertEqual(cp.stdout,b'')
            self.assertEqual(cp.stderr.decode().strip(),ResultEnvelopeUnavailable.code)

    def test_late_describe_gives_no_descriptor_or_content(self):
        orig=self.provider.describe_authorized
        def late(*a,**kw):
            d=orig(*a,**kw);self.clock.advance(6);return d
        self.provider.describe_authorized=late
        res,d=self.resolver().describe_only(request())
        self.assertIsNone(d)
        self.assertEqual((res.outcome,res.reason),('UNREADABLE','DEADLINE_EXPIRED_AFTER_DESCRIBE'))
        self.assertEqual(self.provider.stats.content_opens,0)
        self.assertIsNone(res.observed_revision)

class ChangedComposition(unittest.TestCase):
    def fixture(self):
        g=composition_fixture.G_ReceiverComposition();g.setUp()
        self.addCleanup(g.doCleanups);self.addCleanup(g.tearDown)
        return g

    def test_late_metadata_is_hold_in_existing_receiver_no_same(self):
        g=self.fixture();orig=g.provider.describe_authorized
        def late(*a,**kw):
            d=orig(*a,**kw);g.clock.advance(6);return d
        g.provider.describe_authorized=late
        res=g.ingest(raw_event(revision_basis={'before':'rev-5','after':'rev-6'},owner_seq=6))
        self.assertEqual(res.disposition,'HOLD_UNREADABLE')
        self.assertEqual((res.resolver_calls,g.provider.stats.content_opens),(1,0))
        self.assertEqual(g.adapter.last_result.reason,'DEADLINE_EXPIRED_AFTER_DESCRIBE')

    def test_ontime_metadata_and_slice_still_work(self):
        g=self.fixture()
        req=ResolveRequest(event_id='ontime',owner_ref=OWNER,referent_type='doc',referent_id='doc-a',depth='REVISION_CHECK',pointer_ref=None,expected_revision='rev-6',expected_sha256=FULL_SHA,owner_seq=6)
        res=g.adapter.resolve(req)
        self.assertEqual(res.revision_relation,'SAME');self.assertIsNone(res.grounding)
        self.assertEqual(g.provider.stats.content_opens,0)
        self.assertEqual(g.ingest(g.event()).disposition,'ACK_READ')

    def test_tiny_receiver_cap_is_hold_with_no_stale_last_result(self):
        g=self.fixture();g.adapter._by_pointer={k:dataclasses.replace(v,limits=dataclasses.replace(v.limits,max_result_bytes=1024)) for k,v in g.adapter._by_pointer.items()}
        self.assertEqual(g.ingest(g.event()).disposition,'HOLD_UNREADABLE')
        self.assertEqual(g.provider.stats.content_opens,0)
        self.assertIsNone(g.adapter.last_result)
