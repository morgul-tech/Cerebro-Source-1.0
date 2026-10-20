"""Rev2 A-F through actual prepare/read CLI; inert data, no actual send/work."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

BRIDGE = Path(__file__).resolve().parents[1]
SCRIPT = BRIDGE / 'bk05_continuation.py'
spec = importlib.util.spec_from_file_location('continuation_under_test', SCRIPT)
adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
sys.path.insert(0, str(BRIDGE.parents[1] / 'candidates/bk05-capsule-tool-v1/src'))
from bk05_capsule import synthetic


class OrdinaryPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bk05-continuation-')
        self.addCleanup(self.cleanup_fixture)
        self.root = Path(self.tmp.name).resolve()
        self.output = self.root / 'existing-task-home'; self.output.mkdir()
        f = synthetic.positive_fixture()
        parent = json.loads(f['parent_manifest.json'])
        self.roles = {who: {'actor_ref': 'SYN-' + name, 'generation_ref': 'SYN-GEN', 'carrier_ref': 'SYN-' + name + '-CARRIER'}
                      for who, name in [('issuer', 'A1'), ('reviewer', '655'), ('recipient', '1B')]}
        parent['actor_ref'] = self.roles['recipient']['actor_ref']
        review_delta = json.loads(f['verifier_delta.json'])
        review_delta.update(requested_actor_ref=parent['actor_ref'], verifier_ref='SYN-655-REVIEW')
        self.parent = parent
        self.write('original.txt', f['parent.txt']); self.write('review.txt', f['verifier.txt'])
        self.write('parent-manifest.json', parent); self.write('reviewer-delta.json', review_delta)
        self.write('producer-currentness.json', f['currentness.json'])
        self.write('producer-current.txt', b'SYN frozen producer current; not a memory assertion.\n')
        self.write('live-current.txt', b'SYN actual owner current observation source.\n')
        self.request = {'schema': adapter.SCHEMA, 'task_ref': parent['task_ref'], 'task_revision': parent['parent_revision'],
            'continuation_ref': 'SYN-GENUINE-REFINE-FIXTURE', 'kind': 'REFINE', **copy.deepcopy(self.roles),
            'review_ref': 'SYN-655-REVIEW', 'purpose': 'SYN bounded fixture continuation, never an actor task',
            'return_target': parent['return_target'], 'way_home': parent['way_home'],
            'code_basis': {'source_commit': 'a337dd6ad9efc75041b042d7d404b767d6bf9f6f',
                           'core_sha256': adapter.pin(adapter.CORE.read_bytes())['sha256']},
            'inputs': {key: str(self.root / name) for key, name in {
                'original': 'original.txt', 'review': 'review.txt', 'owner_decision': 'owner-decision.json',
                'owner_delta': 'owner-delta.json', 'current': 'producer-current.txt', 'parent_manifest': 'parent-manifest.json',
                'reviewer_delta': 'reviewer-delta.json', 'producer_currentness': 'producer-currentness.json'}.items()}}
        self.decision = {'schema': 'bk05-owner-decision/v1', **{key: self.request[key] for key in ('task_ref', 'task_revision', 'continuation_ref')},
            **copy.deepcopy(self.roles), 'original_sha256': adapter.pin(f['parent.txt'])['sha256'],
            'review_sha256': adapter.pin(f['verifier.txt'])['sha256'], 'decisions': [
                {'finding_ref': 'SYN-F1', 'disposition': 'ACCEPTED', 'reviewer_position': 'SYN reviewer asks correction A',
                 'owner_position': 'SYN owner selects correction A', 'dissent': 'SYN no dissent on A'},
                {'finding_ref': 'SYN-F2', 'disposition': 'REJECTED', 'reviewer_position': 'SYN reviewer asks broader B',
                 'owner_position': 'SYN owner rejects B within current mandate', 'dissent': 'SYN reviewer B remains attributed and unchanged'}]}
        self.write('owner-decision.json', self.decision)
        self.delta = {'schema': 'bk05-owner-delta/v1', **{key: self.request[key] for key in ('task_ref', 'task_revision', 'continuation_ref')},
            'owner_decision_sha256': adapter.pin((self.root / 'owner-decision.json').read_bytes())['sha256'],
            'action': 'SYN OPERATIONAL OWNER ACTION A ONLY',
            'scope': {key: parent[key] for key in ('effect_class', 'privacy_class', 'live_scope', 'authority_class', 'allowed_paths')},
            'critical_constraints': ['SYN no external effect; preserve reviewer dissent'],
            'done_criteria': ['SYN return corrected bytes to existing owner']}
        self.write('owner-delta.json', self.delta); self.write('request.json', self.request)
        self.observation = {'schema': 'bk05-recipient-current-observation/v1',
            **{key: self.request[key] for key in ('task_ref', 'task_revision', 'continuation_ref', 'recipient')},
            'owner_decision_sha256': adapter.pin((self.root / 'owner-decision.json').read_bytes())['sha256'],
            'original_sha256': self.decision['original_sha256'],
            'source_current': {'path': str(self.root / 'live-current.txt'),
                               'sha256': adapter.pin((self.root / 'live-current.txt').read_bytes())['sha256']},
            'selected_source_commit': self.request['code_basis']['source_commit'], 'observed_global_source_head': 'SYN-HEAD',
            'observed_at': '2026-10-20T10:00:00+02:00', 'provenance': 'SYN caller current read; not authenticated',
            'relevant_change': 'NONE', 'newer_direction': 'NONE', 'work_state': 'PARTIAL', 'send_outcome': 'SENT'}
        self.write('observation.json', self.observation)

    def cleanup_fixture(self):
        self.tmp.cleanup()
        self.assertFalse(self.root.exists(), 'Only this test-owned temporary root must be cleaned')

    def write(self, name, value):
        raw = value if isinstance(value, bytes) else adapter.canonical(value)
        (self.root / name).write_bytes(raw)

    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), *args], cwd=self.root, capture_output=True, timeout=15)
        self.assertEqual(p.stderr, b'')
        self.assertEqual(p.returncode, 0 if ok else 2, p.stdout.decode())
        obj = json.loads(p.stdout)
        if not ok:
            self.assertFalse(obj['payload_released']); self.assertFalse(obj['work_executed'])
            self.assertNotIn('delivery', obj)
        return obj

    def prepare(self, **options):
        return self.cli('prepare', '--request', str(self.root / 'request.json'), '--out-root', str(self.output), **options)

    def read(self, packet, mode='short', part=None, **options):
        args = ['read', '--manifest', packet['manifest'], '--expect-sha', packet['manifest_sha256'],
                '--mode', mode, '--current-observation', str(self.root / 'observation.json')]
        if part is not None:
            args += ['--part', str(part)]
        return self.cli(*args, **options)

    def snapshot(self, packet):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in Path(packet['manifest']).parent.rglob('*') if p.is_file()}

    def delivered_text(self, packet, mode):
        result = self.read(packet, mode)
        if result['delivery']['kind'] == 'COMPLETE_INLINE_TEXT':
            return result['delivery']['text']
        self.assertEqual(result['delivery']['kind'], 'EXACT_PARTS_REQUIRED_NOT_FULL_DELIVERY')
        result_text = ''
        for row in result['delivery']['parts']:
            chunk = self.read(packet, mode, row['index'])['delivery']
            self.assertFalse(chunk['complete_delivery_claim'])
            self.assertEqual(adapter.pin(chunk['text'].encode())['sha256'], row['sha256'])
            result_text += chunk['text']
        return result_text

    def test_A_same_owner_delta_short_full_and_outer_mutations(self):
        packet = self.prepare(); self.assertEqual(packet['state'], 'PREPARED_COMPLETE_PACKET')
        short, full = self.delivered_text(packet, 'short'), self.delivered_text(packet, 'full')
        self.assertIn(self.delta['action'], short); self.assertIn(self.delta['action'], full)
        for constraint in self.delta['critical_constraints']:
            self.assertIn(constraint, short); self.assertIn(constraint, full)
        self.assertTrue((self.root / 'original.txt').read_bytes().decode('utf-8') in full, 'Exact UTF8 original required')
        self.assertTrue((self.root / 'review.txt').read_bytes().decode('utf-8') in full, 'Exact UTF8 review required')
        for member in ('inputs/owner-delta.json', 'inputs/owner-decision.json', 'short.txt', 'full.txt'):
            p = Path(packet['manifest']).parent / member; before = p.read_bytes()
            p.write_bytes(before + b'PRIVATE-MUTATION-SENTINEL')
            bad = self.read(packet, ok=False)
            self.assertEqual(bad['reason'], 'PACKET_MEMBER_CHANGED')
            self.assertNotIn('PRIVATE-MUTATION-SENTINEL', str(bad)); p.write_bytes(before)

    def test_B_distinct_reviewer_owner_and_disagreement_never_rewritten(self):
        before = (self.root / 'reviewer-delta.json').read_bytes()
        packet = self.prepare()
        for mode in ('short', 'full'):
            result = self.delivered_text(packet, mode)
            for who in ('SYN-A1', 'SYN-655', 'SYN-1B'):
                self.assertIn(who, result)
            for row in self.decision['decisions']:
                self.assertIn(row['disposition'], result); self.assertIn(row['reviewer_position'], result)
                self.assertIn(row['owner_position'], result); self.assertIn(row['dissent'], result)
        self.assertEqual((Path(packet['manifest']).parent / 'inputs/reviewer-delta.json').read_bytes(), before)
        self.assertEqual((self.root / 'reviewer-delta.json').read_bytes(), before)

    def test_C_frozen_capsule_new_observation_full_short_full_no_global_head_gate(self):
        packet = self.prepare(); before = self.snapshot(packet)
        for n, mode in enumerate(('full', 'short', 'full')):
            self.observation['observed_at'] = '2026-10-20T1%d:00:00+02:00' % n
            self.observation['observed_global_source_head'] = 'f' * 40
            self.observation['relevant_change'] = 'UNRELATED_GLOBAL_HEAD'
            self.write('observation.json', self.observation)
            result = self.read(packet, mode)
            self.assertEqual(result['manifest_sha256'], packet['manifest_sha256'])
            self.assertEqual(result['authority'], 'NONE')
        self.assertEqual(self.snapshot(packet), before)
        self.assertEqual(self.prepare()['state'], 'EXACT_REUSE')
        self.observation['owner_decision_sha256'] = 'f' * 64; self.write('observation.json', self.observation)
        self.assertEqual(self.read(packet, 'full', ok=False)['reason'], 'CURRENT_OWNER_DECISION_CHANGED_RECONCILE')

    def test_D_missing_changed_recipient_paths_scope_observation_and_staging(self):
        packet = self.prepare(); folder = Path(packet['manifest']).parent
        p = folder / 'inputs/review.txt'; original = p.read_bytes(); p.unlink()
        self.read(packet, ok=False); p.write_bytes(original)
        for key in ('actor_ref', 'generation_ref', 'carrier_ref'):
            obs = copy.deepcopy(self.observation); obs['recipient'][key] = 'SYN-WRONG'
            self.write('observation.json', obs)
            self.assertEqual(self.read(packet, ok=False)['reason'], 'ACTUAL_RECIPIENT_BINDING_MISMATCH')
        for key, value in (('relevant_change', 'SCOPE_OR_ACCESS'), ('newer_direction', 'STOP'), ('newer_direction', 'CORRECTION')):
            obs = copy.deepcopy(self.observation); obs[key] = value; self.write('observation.json', obs)
            self.assertEqual(self.read(packet, 'full', ok=False)['reason'], 'RELEVANT_SCOPE_CODE_OR_DIRECTION_REQUIRES_OWNER')
        self.write('observation.json', self.observation)
        manifest_path = Path(packet['manifest']); raw = manifest_path.read_bytes(); m = json.loads(raw)
        m['files']['../FOREIGN.txt'] = {'bytes': 0, 'sha256': 'a' * 64}
        manifest_path.write_bytes(adapter.canonical(m)); mutated = dict(packet, manifest_sha256=adapter.pin(manifest_path.read_bytes())['sha256'])
        self.assertEqual(self.read(mutated, ok=False)['reason'], 'PACKET_MEMBER_SET_INVALID')
        manifest_path.write_bytes(raw)
        # Recreate a matching interrupted OWN stage, with a missing output; same identity repairs locally.
        stage = self.output / ('.bk05-stage-' + folder.name[7:]); folder.rename(stage)
        (stage / 'full.txt').unlink()
        repaired = self.prepare(); self.assertEqual(repaired['manifest_sha256'], packet['manifest_sha256'])
        self.assertFalse(stage.exists())
        folder.rename(stage); foreign = stage / 'unknown-private-file'; foreign.write_bytes(b'SYN preserve')
        self.assertEqual(self.prepare(ok=False)['reason'], 'UNKNOWN_STAGING_CONTENT_RECONCILE')
        self.assertEqual(foreign.read_bytes(), b'SYN preserve')

    def test_E_lost_basis_large_full_exact_parts_unknown_effect_and_no_new_send(self):
        self.write('original.txt', ('SYN large original with unicode Ø and emoji \U0001f642\n' * 700).encode())
        self.parent['parent_sha256'] = adapter.pin((self.root / 'original.txt').read_bytes())['sha256']
        self.write('parent-manifest.json', self.parent)
        self.decision['original_sha256'] = self.parent['parent_sha256']; self.write('owner-decision.json', self.decision)
        self.delta['owner_decision_sha256'] = adapter.pin((self.root / 'owner-decision.json').read_bytes())['sha256']; self.write('owner-delta.json', self.delta)
        self.observation['original_sha256'] = self.decision['original_sha256']; self.observation['owner_decision_sha256'] = self.delta['owner_decision_sha256']
        self.write('observation.json', self.observation)
        packet = self.prepare(); before = self.snapshot(packet)
        result = self.read(packet, 'full')
        self.assertEqual(result['delivery']['kind'], 'EXACT_PARTS_REQUIRED_NOT_FULL_DELIVERY')
        full = self.delivered_text(packet, 'full')
        self.assertEqual(full.encode(), (Path(packet['manifest']).parent / 'full.txt').read_bytes())
        self.assertTrue((self.root / 'original.txt').read_bytes().decode('utf-8') in full, 'Exact Unicode UTF8 original required')
        for work_state, send in (('COMPLETED', 'SENT'), ('UNKNOWN_EFFECT', 'UNKNOWN')):
            obs = dict(self.observation, work_state=work_state, send_outcome=send); self.write('observation.json', obs)
            result = self.read(packet, 'full')
            self.assertIn('NO_REPLAY' if work_state == 'COMPLETED' else 'RECONCILE_UNKNOWN', result['action'])
        self.assertEqual(self.snapshot(packet), before)

    def test_F_envelope_nonfit_identity_conflict_and_legacy_callback_compatibility(self):
        packet = self.prepare()
        # Local sender double just receives unchanged complete envelope; never real transport.
        sender = []; sender.append(packet['envelope'])
        self.assertEqual(sender[0]['identity']['reviewer'], self.roles['reviewer'])
        self.assertEqual(sender[0]['sha256'], packet['manifest_sha256'])
        self.read(packet, 'full'); self.assertEqual(len(sender), 1)
        self.request['purpose'] = 'SYN changed bytes SAME identity, not a new attempt'; self.write('request.json', self.request)
        self.assertEqual(self.prepare(ok=False)['reason'], 'CONTINUATION_IDENTITY_INPUT_CONFLICT')
        self.request['purpose'] = 'SYN bounded fixture continuation, never an actor task'; self.write('request.json', self.request)
        # Existing finalize direct callback seam, not its whole historical test suite.
        spec = importlib.util.spec_from_file_location('legacy_entry_regression', BRIDGE / 'rom_e_material_return.py')
        legacy = importlib.util.module_from_spec(spec); spec.loader.exec_module(legacy)
        task = {key: self.parent[key] for key in ('task_ref', 'actor_ref', 'effect_class', 'privacy_class', 'live_scope',
            'authority_class', 'allowed_paths', 'required_invariants', 'stop_edges', 'return_target', 'way_home', 'source_head')}
        task.update(task_revision=self.parent['parent_revision'], generation_ref='SYN-GEN', carrier_ref='SYN-CARRIER', arc_ref='SYN-ARC')
        self.write('legacy-request.json', {'schema': 'cerebro-rom-a-dispatch-capture/v1', 'task': task})
        cap = legacy.presend(request=str(self.root / 'legacy-request.json'), original=str(self.root / 'original.txt'), capture_root=str(self.root / 'legacy-captures'))
        self.write('legacy-return.json', {'schema': 'cerebro-rom-a-return-observation/v1', 'observation_ref': 'SYN-legacy-refine',
            'return': {**{key: task[key] for key in ('task_ref', 'task_revision', 'actor_ref', 'generation_ref', 'carrier_ref', 'arc_ref')},
                       'original_sha256': self.decision['original_sha256'], 'contract_kind': 'REFINE'}})
        kwargs = dict(capture_record=cap['capture_path'], return_input=str(self.root / 'legacy-return.json'),
                      parent_manifest=str(self.root / 'parent-manifest.json'), verifier=str(self.root / 'review.txt'),
                      verifier_delta=str(self.root / 'reviewer-delta.json'), currentness=str(self.root / 'producer-currentness.json'))
        full = legacy.finalize(**kwargs, output_root=str(self.root / 'legacy-full'))
        self.assertEqual(full['selection'], 'FULL_ORIGINAL_TASK')
        short = legacy.finalize(**kwargs, output_root=str(self.root / 'legacy-short'),
            held_basis_reader=lambda *args: ('SYN callback fixture NOT real semantic basis',
                                             (self.root / 'original.txt').read_bytes(), (self.root / 'review.txt').read_bytes()))
        self.assertEqual(short['selection'], 'BK05_STRUCTURAL_CAPSULE_CANDIDATE')
        # Genuine non-code fit: no made-up repo/test/profile fields, complete original+review+owner action.
        self.request['continuation_ref'] = 'SYN-FULL-NONFIT'
        self.decision['continuation_ref'] = self.request['continuation_ref']; self.write('owner-decision.json', self.decision)
        self.delta.update(continuation_ref=self.request['continuation_ref'], owner_decision_sha256=adapter.pin((self.root / 'owner-decision.json').read_bytes())['sha256'])
        self.write('owner-delta.json', self.delta)
        for key in adapter.OPTIONAL:
            self.request['inputs'][key] = None
        self.write('request.json', self.request)
        self.observation.update(continuation_ref=self.request['continuation_ref'], owner_decision_sha256=self.delta['owner_decision_sha256'])
        self.write('observation.json', self.observation)
        full_packet = self.prepare(); result = self.read(full_packet, 'short')
        self.assertEqual(result['selected_member'], 'full.txt')
        self.assertEqual(result['profile']['reason'], 'CODE_PROFILE_NOT_SELECTED')
        self.assertIn(self.delta['action'], self.delivered_text(full_packet, 'short'))

    def test_R1_completed_and_unknown_send_both_retained_same_packet(self):
        packet = self.prepare(); before = self.snapshot(packet)
        cases = [('COMPLETED', 'SENT', 'ALREADY_COMPLETED_NO_REPLAY'),
                 ('COMPLETED', 'UNKNOWN', 'ALREADY_COMPLETED_NO_REPLAY_AND_RECONCILE_UNKNOWN_SEND_BEFORE_ACTION'),
                 ('UNKNOWN_EFFECT', 'SENT', 'RECONCILE_UNKNOWN_SEND_OR_EFFECT_BEFORE_ACTION'),
                 ('PARTIAL', 'UNKNOWN', 'RECONCILE_UNKNOWN_SEND_OR_EFFECT_BEFORE_ACTION')]
        for work, send, expected in cases:
            with self.subTest(work_state=work, send_outcome=send):
                self.write('observation.json', dict(self.observation, work_state=work, send_outcome=send))
                result = self.read(packet)
                self.assertEqual(result['observation']['work_state'], work)
                self.assertEqual(result['observation']['send_outcome'], send)
                self.assertEqual(result['action'], expected)
                self.assertEqual(result['manifest_sha256'], packet['manifest_sha256'])
        self.assertEqual(self.snapshot(packet), before)

    def test_R2_partial_matching_full_but_supplied_stale_pairs_refuse(self):
        self.request['inputs']['reviewer_delta'] = None
        self.write('request.json', self.request)
        baseline = json.loads((self.root / 'producer-currentness.json').read_bytes())
        cases = [('matching', None, None),
                 ('stale_revision', 'observed_parent_revision', 'PRODUCER_PARENT_REVISION_STALE'),
                 ('stale_head', 'observed_source_head', 'PRODUCER_SOURCE_HEAD_STALE')]
        for name, field, expected in cases:
            with self.subTest(case=name):
                self.output = self.root / ('separate-inert-case-' + name); self.output.mkdir()
                value = dict(baseline)
                if field is not None:
                    value[field] = 'SYN-EXPLICIT-CONTRADICTION'
                self.write('producer-currentness.json', value)
                result = self.prepare(ok=field is None)
                if field is None:
                    read = self.read(result)
                    self.assertEqual(read['selected_member'], 'full.txt')
                    self.assertEqual(read['profile']['reason'], 'CODE_PROFILE_BASIS_INCOMPLETE')
                else:
                    self.assertEqual(result['reason'], expected)
                    self.assertEqual(list(self.output.iterdir()), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
