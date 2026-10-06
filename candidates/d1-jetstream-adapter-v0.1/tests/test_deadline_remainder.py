"""Changed-risk tests only. Synthetic transport/clock, no NATS or host effects."""
import asyncio
import sys
import types
from unittest.mock import patch

import test_unit_publisher_correction as correction
from _support import TmpCase, FakeClock
from d1js.publisher import LookupBudget
from d1js.nats_transport import NatsJetStreamTransport
from d1js.transport import LookupUnavailable, InMemoryStreamTransport


class DeadlineRemainder(TmpCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.inner = InMemoryStreamTransport(clock=self.clock.epoch)
        self.transport = correction.CountingTransport(self.inner)
    fixture = correction.PublisherCorrection.fixture
    _unknown = correction.PublisherCorrection._unknown


def scenario(self, *, state_delay=0, get_delay=0, match=False, hole=False):
    fx = self.fixture(lookup_time_budget_s=1, lookup_bound=5)
    f = fx.commit_hint()
    fx.owner.outbox_update(f['message_id'], state='UNKNOWN_PENDING')
    now = [0.0]
    calls = []
    def state(*, timeout=None):
        calls.append(('state', timeout))
        now[0] += state_delay
        return {'messages': 1, 'first_seq': 1, 'last_seq': 1, 'bytes': 1}
    def get(seq, *, timeout=None):
        calls.append(('get', timeout))
        now[0] += get_delay
        return None if hole else (f['pointer'], {'Nats-Msg-Id': f['message_id'] if match else 'other'})
    self.inner.stream_state, self.inner.get_msg = state, get
    with patch('d1js.publisher.time.monotonic', side_effect=lambda: now[0]):
        result = fx.publisher.reconcile_publication(f['message_id'])
    return fx, f, result, calls


def test_late_state(self):
    fx, f, result, calls = scenario(self, state_delay=1.1)
    self.assertEqual(result, 'RECONCILE_HOLD')
    self.assertEqual([c[0] for c in calls], ['state'])
    self.assertEqual(fx.publisher.publish_calls, 0)
    self.assertEqual(fx.owner.outbox(f['message_id'])[0]['state'], 'RECONCILE_HOLD')


def test_late_match(self):
    fx, _, result, _ = scenario(self, get_delay=1.1, match=True)
    self.assertEqual((result, fx.publisher.publish_calls), ('RECONCILE_HOLD', 0))


def test_late_absent(self):
    fx, _, result, _ = scenario(self, get_delay=1.1)
    self.assertEqual((result, fx.publisher.publish_calls), ('RECONCILE_HOLD', 0))


def test_late_hole(self):
    fx, _, result, _ = scenario(self, get_delay=1.1, hole=True)
    self.assertEqual((result, fx.publisher.publish_calls), ('RECONCILE_HOLD', 0))


def test_remaining_passed_and_match_kept(self):
    fx, _, result, calls = scenario(self, state_delay=.25, get_delay=.25, match=True)
    self.assertEqual((result, fx.publisher.publish_calls), ('PUBLISHED', 0))
    self.assertEqual(calls, [('state', 1.0), ('get', .75)])


def test_complete_absence_kept(self):
    fx, _, result, _ = scenario(self, state_delay=.25, get_delay=.25)
    self.assertEqual((result, fx.publisher.publish_calls), ('PUBLISHED', 1))


def test_no_call_with_exhausted_budget(self):
    fx = self.fixture()
    mid = self._unknown(fx, 'expired-budget')
    with patch('d1js.publisher.time.monotonic', return_value=0):
        budget = LookupBudget(4, 1)
    self.transport.calls.clear()
    with patch('d1js.publisher.time.monotonic', return_value=2):
        self.assertEqual(fx.publisher.reconcile_publication(mid, budget=budget), 'RECONCILE_HOLD')
    self.assertEqual(self.transport.calls, [])


def test_state_counts_in_call_bound(self):
    fx = self.fixture(lookup_bound=1)
    mid = self._unknown(fx, 'call-bound')
    self.inner.publish('cerebro.test.d1.pointer', b'{}', 'other', 1)
    self.transport.calls.clear()
    self.assertEqual(fx.publisher.reconcile_publication(mid), 'RECONCILE_HOLD')
    self.assertEqual(self.transport.calls, ['stream_state'])


def test_presence_shared_budget_and_late_reply(self):
    fx = self.fixture(lookup_time_budget_s=1)
    mids = [fx.commit_hint(owner_event_key=f'presence-{i}')['message_id'] for i in range(2)]
    for mid in mids:
        self.assertEqual(fx.publisher.publish(mid), 'PUBLISHED')
    now = [0.0]
    calls = []
    def get(seq, *, timeout=None):
        calls.append((seq, timeout))
        now[0] += 1.1
        return b'{}', {}
    self.inner.get_msg = get
    with patch('d1js.publisher.time.monotonic', side_effect=lambda: now[0]):
        rows = fx.reconcile_once(publish_unqueued=False)
    self.assertEqual(len(calls), 1)
    self.assertIn(calls[0][0], {1, 2})
    self.assertEqual(calls[0][1], 1.0)
    self.assertEqual([r['reason'] for r in rows], ['PENDING_LOOKUP_UNAVAILABLE', 'PENDING_LOOKUP_BUDGET_EXHAUSTED'])
    # Prior proven PubAck is retained, with unresolved owner obligations visible.
    self.assertEqual(len(fx.owner.outbox()), 2)
    self.assertEqual(fx.publisher.publish_calls, 2)


def test_expiry_during_claim_blocks_retry(self):
    fx = self.fixture()
    mid = fx.commit_hint()['message_id']
    fx.owner.outbox_update(mid, state='UNKNOWN_PENDING')
    now = [0.0]
    fx.faults.on('delay', lambda: now.__setitem__(0, 2.0))
    fx.faults.set('publish.after_claim_before_send', 'call:delay')
    with patch('d1js.publisher.time.monotonic', side_effect=lambda: now[0]):
        self.assertEqual(fx.publisher.reconcile_publication(mid, budget=LookupBudget(5, 1)), 'RECONCILE_HOLD')
    self.assertEqual(fx.publisher.publish_calls, 0)


def test_nats_lookup_bounds_without_broker(self):
    # Exercise the exact Nats transport methods on an offline fake async client.
    class JS:
        async def stream_info(self, stream):
            return types.SimpleNamespace(state=types.SimpleNamespace(messages=1, first_seq=1, last_seq=1, bytes=1))
        async def get_msg(self, stream, seq):
            return types.SimpleNamespace(data=b'abc', headers={})
    t = NatsJetStreamTransport.__new__(NatsJetStreamTransport)
    t._js = JS()
    t._cfg = types.SimpleNamespace(stream='offline')
    bounds = []
    def call(coro, timeout):
        bounds.append(timeout)
        return asyncio.run(coro)
    t._call = call
    errors = types.ModuleType('nats.js.errors')
    errors.NotFoundError = type('NotFoundError', (Exception,), {})
    with patch.dict(sys.modules, {'nats': types.ModuleType('nats'), 'nats.js': types.ModuleType('nats.js'), 'nats.js.errors': errors}):
        self.assertEqual(t.stream_state(timeout=.125)['messages'], 1)
        self.assertEqual(t.get_msg(1, timeout=.075), (b'abc', {}))
        self.assertEqual(bounds, [.125, .075])
        with self.assertRaises(LookupUnavailable):
            t.get_msg(1, timeout=0)
    self.assertEqual(bounds, [.125, .075])


def test_nats_async_wait_cancels_late_state(self):
    cancelled = []
    class JS:
        async def stream_info(self, stream):
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.append(True)
    t = NatsJetStreamTransport.__new__(NatsJetStreamTransport)
    t._js = JS()
    t._cfg = types.SimpleNamespace(stream='offline')
    t._call = lambda coro, timeout: asyncio.run(coro)
    with self.assertRaises(LookupUnavailable):
        t.stream_state(timeout=.005)
    self.assertEqual(cancelled, [True])


for name, function in list(globals().items()):
    if name.startswith('test_'):
        setattr(DeadlineRemainder, name, function)
