"""Offline contract tests. Fake ports are test doubles, never PM or channel authority."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("x9_channel_ingress", ROOT / "adapters" / "x9_channel_ingress.py")
assert spec is not None and spec.loader is not None
x9 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = x9
spec.loader.exec_module(x9)

from signalvev_sensing.activation import decide_activation  # noqa: E402
from signalvev_sensing.return_sink import make_closure  # noqa: E402

NOW = datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc)
PM = "pm:producer"
X9 = "x9:consumer"
SESSION = "codex:local/x9-session-test"
SHA = "a" * 64
PACKET_SHA = "b" * 64
MATERIAL_SHA = "c" * 64


def context():
    return x9.PointerContext(
        event_id="event:test1", attempt_id="attempt:test1", owner_ref="owner:pm",
        referent_type="pm-ready",
        revision="rev:1", expected_sha256=SHA, claim_ref="claim:test1",
        packet_ref="packet:test1", packet_sha256=PACKET_SHA, queue_ref="queue:test1",
        producer_id=PM, receiver_ref=SESSION, source_cut="cut:test1",
        expires_at="2026-10-04T22:00:00Z",
        way_home=("way:pm", "way:x9"),
    )


def closure(ctx=None):
    ctx = ctx or context()
    return make_closure(
        event_id=ctx.event_id, owner_ref=ctx.owner_ref, referent_type=ctx.referent_type,
        referent_id=ctx.claim_ref, revision_after=ctx.revision, disposition="ACK_READ",
        reason="OWNER_READ_MATCHES_EVENT", observed_sha256=ctx.expected_sha256,
        activation=decide_activation("ACK_READ", "SEMANTIC"), way_home=ctx.way_home)


def cut(ctx=None, *, relation="SAME", material_ready=True):
    ctx = ctx or context()
    return x9.PMOwnerCut(
        owner_ref=ctx.owner_ref, claim_ref=ctx.claim_ref, packet_ref=ctx.packet_ref,
        packet_sha256=ctx.packet_sha256, queue_ref=ctx.queue_ref, revision=ctx.revision,
        relation_to_hint=relation, material_sha256=MATERIAL_SHA, source_cut=ctx.source_cut,
        committed_readback=True, authenticated=True, material_ready=material_ready)


class FakeChannel:
    """The atomic append and authenticated readback semantics are only simulated."""

    def __init__(self):
        self.principal = PM
        self.authenticated = True
        self.append_allowed = True
        self.read_allowed = True
        self.pointer = None
        self.disposition = None
        self.pointer_sends = 0
        self.disposition_sends = 0
        self.pointer_state = "ACCEPTED"
        self.drop_unknown_pointer = False
        self.bad_readback = False
        self.disposition_readback = True
        self.disposition_readback_override = None

    def identity(self):
        return x9.ChannelIdentity(x9.CHANNEL, self.principal, self.authenticated,
                                  self.append_allowed, self.read_allowed)

    def append_pointer_once(self, record):
        self.pointer_sends += 1
        if self.pointer_state != "NOT_SENT" and not (
                self.pointer_state == "UNKNOWN_SEND" and self.drop_unknown_pointer):
            self.pointer = record
        return self.pointer_state

    def read_pointer_by_event_id(self, event_id):
        if self.pointer is None or self.pointer.event_id != event_id:
            return None
        digest = "f" * 64 if self.bad_readback else self.pointer.content_sha256
        return x9.Readback(self.pointer, digest, PM, "sheet:rev1")

    def append_disposition_once(self, record):
        self.disposition_sends += 1
        self.disposition = record
        return "ACCEPTED"

    def read_disposition_by_event_id(self, event_id):
        if self.disposition_readback_override is not None:
            return self.disposition_readback_override
        if not self.disposition_readback or self.disposition is None or self.disposition.event_id != event_id:
            return None
        return x9.Readback(self.disposition, self.disposition.content_sha256, X9, "sheet:rev2")


class FakePM:
    def __init__(self, state=None):
        self.state = state or cut()
        self.calls = 0

    def read_current(self, claim_ref, packet_ref, queue_ref):
        self.calls += 1
        assert (claim_ref, packet_ref, queue_ref) == (
            self.state.claim_ref, self.state.packet_ref, self.state.queue_ref)
        return self.state


class X9ChannelIngressTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context()
        self.channel = FakeChannel()
        self.pm = FakePM()
        self.bridge = x9.X9ChannelIngress(
            enabled=True, channel=self.channel, pm_reader=self.pm,
            producer_principal=PM, x9_principal=X9, x9_session_ref=SESSION)

    def deposited(self):
        result = self.bridge.deposit(closure(self.ctx), self.ctx, now=NOW)
        self.assertEqual(result.state, "DEPOSITED_READBACK")
        self.channel.principal = X9
        return result

    def test_default_off_and_missing_auth_fail_closed(self):
        bridge = x9.X9ChannelIngress(channel=self.channel, pm_reader=self.pm,
                                     producer_principal=PM, x9_principal=X9, x9_session_ref=SESSION)
        self.assertEqual(bridge.deposit(closure(), self.ctx, now=NOW).state, x9.REFINE)
        self.assertEqual(self.channel.pointer_sends, 0)
        self.channel.authenticated = False
        self.assertEqual(self.bridge.deposit(closure(), self.ctx, now=NOW).state, x9.REFINE)
        self.assertEqual(self.channel.pointer_sends, 0)

    def test_forged_closure_and_expiry_never_append(self):
        forged = replace(closure(), owner_ref="owner:other")
        self.assertEqual(self.bridge.deposit(forged, self.ctx, now=NOW).state, x9.HOLD)
        expired = replace(self.ctx, expires_at="2026-10-04T20:00:00Z")
        self.assertEqual(self.bridge.deposit(closure(expired), expired, now=NOW).reason,
                         "EXPIRED_OR_INVALID_CLOCK")
        wrong_receiver = replace(self.ctx, receiver_ref="codex:local/other-session")
        self.assertEqual(self.bridge.deposit(closure(wrong_receiver), wrong_receiver, now=NOW).reason,
                         "PRODUCER_OR_RECEIVER_BINDING_MISMATCH")
        self.assertEqual(self.channel.pointer_sends, 0)

    def test_append_ack_without_exact_readback_is_not_delivery(self):
        self.channel.bad_readback = True
        result = self.bridge.deposit(closure(), self.ctx, now=NOW)
        self.assertEqual(result.state, x9.COLLISION)
        self.assertEqual(self.channel.pointer_sends, 1)
        self.assertEqual(self.pm.calls, 0)

    def test_unknown_send_reconciles_once_by_event_and_hash(self):
        self.channel.pointer_state = "UNKNOWN_SEND"
        result = self.bridge.deposit(closure(), self.ctx, now=NOW)
        self.assertEqual(result.state, "DEPOSITED_READBACK")
        self.assertEqual(self.channel.pointer_sends, 1)
        again = self.bridge.deposit(closure(), self.ctx, now=NOW)
        self.assertEqual(again.reason, "DUPLICATE_SAME_EVENT")
        self.assertEqual(self.channel.pointer_sends, 1)

    def test_unknown_send_without_readback_never_replays(self):
        self.channel.pointer_state = "UNKNOWN_SEND"
        self.channel.drop_unknown_pointer = True
        result = self.bridge.deposit(closure(), self.ctx, now=NOW)
        self.assertEqual(result.state, x9.HOLD)
        self.assertEqual(result.reason, "UNKNOWN_SEND_NO_REPLAY")
        again = self.bridge.deposit(closure(), self.ctx, now=NOW)
        self.assertEqual(again.reason, "UNKNOWN_SEND_NO_REPLAY")
        self.assertEqual(self.channel.pointer_sends, 1)

    def test_changed_content_under_same_event_is_collision(self):
        self.deposited()
        self.channel.principal = PM
        changed = replace(self.ctx, packet_sha256="d" * 64)
        result = self.bridge.deposit(closure(changed), changed, now=NOW)
        self.assertEqual(result.state, x9.COLLISION)
        self.assertEqual(self.channel.pointer_sends, 1)

    def test_missing_pm_reread_and_missed_d0_are_holds(self):
        self.deposited()
        unbound = x9.X9ChannelIngress(
            enabled=True, channel=self.channel, producer_principal=PM, x9_principal=X9,
            x9_session_ref=SESSION)
        self.assertEqual(unbound.consume(self.ctx.event_id, now=NOW).state, x9.REFINE)
        self.assertEqual(self.bridge.consume(None, now=NOW).reason, "MISSED_D0_OWNER_PULSE_REQUIRED")
        self.assertEqual(self.channel.disposition_sends, 0)

    def test_stale_no_delta_material_and_duplicate_disposition(self):
        self.deposited()
        self.pm.state = cut(relation="SUPERSEDED")
        stale = self.bridge.consume(self.ctx.event_id, now=NOW)
        self.assertEqual(stale.disposition.disposition, x9.STALE)
        self.assertFalse(stale.disposition.work_consumed)
        self.assertEqual(stale.disposition.effect, "NONE_CLAIMED")
        same = self.bridge.consume(self.ctx.event_id, now=NOW)
        self.assertEqual(same.state, "ALREADY_DISPOSED")
        self.assertEqual(self.channel.disposition_sends, 1)
        self.channel.disposition = None
        self.pm.state = cut(material_ready=False)
        no_delta = self.bridge.consume(self.ctx.event_id, now=NOW)
        self.assertEqual(no_delta.disposition.disposition, x9.NO_DELTA)
        self.channel.disposition = None
        self.pm.state = cut()
        material = self.bridge.consume(self.ctx.event_id, now=NOW)
        self.assertEqual(material.disposition.disposition, x9.MATERIAL)
        self.assertEqual(material.state, "DISPOSITION_READBACK")

    def test_preexisting_disposition_requires_exact_no_effect_readback(self):
        self.deposited()
        valid = x9.X9Disposition(
            x9.DISPOSITION_SCHEMA, self.ctx.event_id, self.ctx.attempt_id,
            self.channel.pointer.content_sha256, "rev:1", MATERIAL_SHA, x9.MATERIAL,
            "NEW_CURRENT_PM_MATERIAL")

        invalid_records = (
            replace(valid, event_id="event:foreign"),
            replace(valid, schema="signalvev.x9.channel.disposition/v9"),
            replace(valid, attempt_id="attempt:foreign"),
            replace(valid, pointer_sha256="d" * 64),
            replace(valid, disposition="UNRECOGNIZED"),
            replace(valid, owner_revision="invalid revision"),
            replace(valid, owner_material_sha256="not-a-sha256"),
            replace(valid, reason=""),
            replace(valid, work_consumed=True),
            replace(valid, effect="APPLIED"),
        )
        for record in invalid_records:
            with self.subTest(record=record):
                self.channel.disposition_readback_override = x9.Readback(
                    record, record.content_sha256, X9, "sheet:rev2")
                result = self.bridge.consume(self.ctx.event_id, now=NOW)
                self.assertEqual(result.state, x9.COLLISION)
                self.assertEqual(self.pm.calls, 0)
                self.assertEqual(self.channel.disposition_sends, 0)

        bad_provenance = (
            x9.Readback(valid, "f" * 64, X9, "sheet:rev2"),
            x9.Readback(valid, valid.content_sha256, PM, "sheet:rev2"),
            x9.Readback(valid, valid.content_sha256, X9, ""),
        )
        for readback in bad_provenance:
            with self.subTest(readback=readback):
                self.channel.disposition_readback_override = readback
                result = self.bridge.consume(self.ctx.event_id, now=NOW)
                self.assertEqual(result.state, x9.COLLISION)
                self.assertEqual(self.pm.calls, 0)
                self.assertEqual(self.channel.disposition_sends, 0)

        self.channel.disposition_readback_override = x9.Readback(
            valid, valid.content_sha256, X9, "sheet:rev2")
        same = self.bridge.consume(self.ctx.event_id, now=NOW)
        self.assertEqual(same.state, "ALREADY_DISPOSED")
        self.assertEqual(same.disposition, valid)
        self.assertEqual(self.pm.calls, 0)
        self.assertEqual(self.channel.disposition_sends, 0)

    def test_pm_collision_or_missing_disposition_readback_never_routes(self):
        self.deposited()
        self.pm.state = replace(cut(), packet_sha256="e" * 64)
        self.assertEqual(self.bridge.consume(self.ctx.event_id, now=NOW).state, x9.COLLISION)
        self.assertEqual(self.channel.disposition_sends, 0)
        self.pm.state = replace(cut(), source_cut="cut:other")
        self.assertEqual(self.bridge.consume(self.ctx.event_id, now=NOW).state, x9.COLLISION)
        self.assertEqual(self.channel.disposition_sends, 0)
        self.pm.state = cut()
        self.channel.disposition_readback = False
        result = self.bridge.consume(self.ctx.event_id, now=NOW)
        self.assertEqual(result.state, x9.HOLD)
        self.assertEqual(result.reason, "DISPOSITION_UNKNOWN_SEND_NO_REPLAY")
        again = self.bridge.consume(self.ctx.event_id, now=NOW)
        self.assertEqual(again.reason, "DISPOSITION_UNKNOWN_SEND_NO_REPLAY")
        self.assertEqual(self.channel.disposition_sends, 1)

    def test_exhausted_pointer_requires_owner_pulse(self):
        self.deposited()
        late = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc)
        result = self.bridge.consume(self.ctx.event_id, now=late)
        self.assertEqual(result.reason, "EXPIRED_POINTER_OWNER_PULSE_REQUIRED")
        self.assertEqual(self.pm.calls, 0)


if __name__ == "__main__":
    unittest.main()
