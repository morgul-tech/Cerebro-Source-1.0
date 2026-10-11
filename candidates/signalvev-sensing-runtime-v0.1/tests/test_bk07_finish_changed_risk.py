"""C1180 changed-risk regressions only; frozen SYNTHETIC fixtures, no live reads."""
import dataclasses,unittest
from unittest.mock import patch
from bk07_read_front_support import FrontCase,ReadFrontState,read_front
from signalvev_sensing.bk07_read_front import ResultUnavailable,REP_NATIVE_DOCS

class ChangedFinish(FrontCase):
 def test_expiry_during_warm_reuse_no_body_and_cache_invalidated(self):
  self.call();self.access=dataclasses.replace(self.access,expires_at=self.wall.t+1)
  res=self.call(wrap=lambda w:w.after_describe.update({1:lambda:self.wall.advance(2)}))
  self.assertEqual(res.outcome,'DENIED');self.assert_no_content(res)
  self.assertFalse(self.stored()['valid'])
 def test_each_final_access_context_change_or_unknown_has_no_body(self):
  for change in ('principal_ref','scope_fingerprint','provider_context','unknown'):
   with self.subTest(change=change):
    p=self.ports();original=p.access;calls=[]
    def changed():
     calls.append(1)
     if len(calls)==1:return original()
     return None if change=='unknown' else dataclasses.replace(self.access,**{change:'changed:context'})
    p.access=changed
    res=read_front(self.request(),p,ReadFrontState.load(self.state_path))
    self.assertEqual(res.outcome,'DENIED');self.assert_no_content(res)
    self.assertFalse(self.stored()['valid'])
 def test_late_dni_canonical_native_and_missing_cap26_never_emit(self):
  for mode in ('canonical','native','missing_cap26'):
   with self.subTest(mode=mode):
    if mode=='native':
     self.nav_entry()['representation']=REP_NATIVE_DOCS;self.nav_entry()['section']['selector']=None
    elif mode=='missing_cap26':
     self.nav_entry()['representation']='CANONICAL_UTF8_EXPORT_V1'
    p=self.ports(cap26=mode!='missing_cap26');orig=p.dni
    def late(doc):self.clock.advance(6);return orig(doc)
    p.dni=late
    res=read_front(self.request(),p,ReadFrontState.load(self.state_path))
    self.assertEqual(res.outcome,'UNREADABLE');self.assert_no_content(res)
    self.assertEqual(res.persisted,'NOT_ATTEMPTED')
 def test_denied_then_authorized_pending_rows_once_then_unchanged(self):
  allowed=self.cap26_registry['bindings'][0]['authorized_subjects']
  self.cap26_registry['bindings'][0]['authorized_subjects']=['actor:other'];self.write_cap26()
  denied=self.call();self.assertEqual(denied.outcome,'DENIED');self.assert_no_content(denied)
  self.cap26_registry['bindings'][0]['authorized_subjects']=allowed;self.write_cap26()
  delivered=self.call()
  self.assertTrue(all(c.get('content') for c in delivered.rows['changes']))
  self.assertTrue(all(c['change']=='PENDING_DELIVERY' for c in delivered.rows['changes']))
  unchanged=self.call()
  self.assertTrue(all(c['change']=='UNCHANGED' and 'content' not in c for c in unchanged.rows['changes']))
 def test_result_envelope_failure_keeps_pending_rows_for_next_delivery(self):
  res=read_front(self.request(max_result_bytes=256),self.ports(),ReadFrontState.load(self.state_path))
  with self.assertRaises(ResultUnavailable):res.to_json()
  self.assertTrue(all(m.get('delivered_digest') is None for m in ReadFrontState.load(self.state_path).rows(self.request().scope['scope'])['messages'].values()))
  delivered=self.call();self.assertTrue(all('content' in c for c in delivered.rows['changes']))
  unchanged=self.call();self.assertTrue(all('content' not in c and c['change']=='UNCHANGED' for c in unchanged.rows['changes']))
 def test_access_expiry_during_persistence_rolls_back_delivery(self):
  p=self.ports();orig=p.access;calls=[]
  def expiry():
   calls.append(1)
   if len(calls)>=4:self.wall.advance(2)
   return dataclasses.replace(self.access,expires_at=1_790_000_001.0)
  p.access=expiry
  res=read_front(self.request(),p,ReadFrontState.load(self.state_path))
  self.assertEqual(res.outcome,'DENIED');self.assert_no_content(res)
  self.assertEqual(res.persisted,'ROLLED_BACK_FINAL_PUBLISH_REJECTED')
  self.access=dataclasses.replace(self.access,expires_at=4_102_444_800.0)
  self.assertTrue(all('content' in c for c in self.call().rows['changes']))
 def test_late_atomic_replace_rolls_back_delivery_and_next_read_delivers(self):
  import signalvev_sensing.read_front_state as module
  original=module.os.replace;calls=[]
  def late(*a,**kw):
   original(*a,**kw);calls.append(1)
   if len(calls)==1:self.clock.advance(6)
  with patch.object(module.os,'replace',late):
   res=read_front(self.request(),self.ports(),ReadFrontState.load(self.state_path))
  self.assertEqual(res.outcome,'UNREADABLE');self.assert_no_content(res)
  self.assertFalse(self.state_path.exists())
  self.assertTrue(all('content' in c for c in self.call().rows['changes']))

 def test_final_serialization_failure_after_atomic_publish_retains_pending(self):
  import signalvev_sensing.bk07_read_front as module
  original=module.FrontResult.to_json;calls=[]
  def fail_final(result):
   calls.append(1)
   if len(calls)==5:raise ResultUnavailable(ResultUnavailable.code)
   return original(result)
  with patch.object(module.FrontResult,'to_json',fail_final):
   res=read_front(self.request(),self.ports(),ReadFrontState.load(self.state_path))
  self.assertEqual(res.outcome,'UNREADABLE');self.assert_no_content(res)
  self.assertFalse(self.state_path.exists())
  self.assertTrue(all('content' in c for c in self.call().rows['changes']))
