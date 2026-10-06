"""Offline adapter-contract checks; these doubles are not session/auth proof."""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from adapters.x9_channel_ingress import (  # noqa: E402
    CHANNEL,
    DISPOSITION_SCHEMA,
    MATERIAL,
    PMOwnerCut,
    SCHEMA,
    PointerRecord,
    Readback,
    X9ChannelIngress,
    X9Disposition,
)
from providers.x9_session_channel import (  # noqa: E402
    REQUIRED_SCOPES,
    ProviderSessionIdentity,
    X9SessionChannelPort,
    consume_current_x9_pulse,
)

PM = "CURRENT_PM_PROJECT_MANAGER_C1A05B39"
X9_PRINCIPAL = "test:receiver-principal"
X9_SESSION_REF = "test:receiver-session"
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def pointer(*, receiver: str = X9_SESSION_REF) -> PointerRecord:
    return PointerRecord(
        SCHEMA, "closure:1", "event:1", "attempt:1", "owner:pm", "work-packet",
        "revision:1", "a" * 64, "claim:1918", "packet:2629", "b" * 64,
        "queue:633", PM, receiver, "cut:1", "2026-10-05T15:00:00Z", ("way:pm", "way:x9"),
    )


def disposition(ptr: PointerRecord) -> X9Disposition:
    return X9Disposition(DISPOSITION_SCHEMA, ptr.event_id, ptr.attempt_id, ptr.content_sha256,
                         "revision:1", "c" * 64, MATERIAL, "NEW_CURRENT_PM_MATERIAL")


@dataclass
class FakeSessionAPI:
    """A local protocol double only; never evidence for authenticated provider behavior."""

    identity_value: ProviderSessionIdentity
    pointer_value: PointerRecord | None = None
    disposition_value: X9Disposition | None = None

    def __post_init__(self):
        self.calls: list[tuple] = []

    def identity(self):
        self.calls.append(("identity",))
        return self.identity_value

    def append_pointer_once(self, event_id, content_sha256, record):
        self.calls.append(("append_pointer_once", event_id, content_sha256))
        if self.pointer_value is None:
            self.pointer_value = record
        return "ACCEPTED"

    def read_pointer_by_event_id(self, event_id):
        self.calls.append(("read_pointer_by_event_id", event_id))
        if self.pointer_value is None or self.pointer_value.event_id != event_id:
            return None
        p = self.pointer_value
        return Readback(p, p.content_sha256, p.producer_id, "provider:rev1")

    def append_disposition_once(self, event_id, content_sha256, record):
        self.calls.append(("append_disposition_once", event_id, content_sha256))
        if self.disposition_value is None:
            self.disposition_value = record
        return "ACCEPTED"

    def read_disposition_by_event_id(self, event_id):
        self.calls.append(("read_disposition_by_event_id", event_id))
        if self.disposition_value is None or self.disposition_value.event_id != event_id:
            return None
        d = self.disposition_value
        return Readback(d, d.content_sha256, X9_PRINCIPAL, "provider:rev2")


def authorized_identity(**overrides) -> ProviderSessionIdentity:
    values = dict(channel=CHANNEL, principal=X9_PRINCIPAL, session_ref=X9_SESSION_REF,
                  authenticated=True, current=True, scopes=REQUIRED_SCOPES)
    values.update(overrides)
    return ProviderSessionIdentity(**values)


class CaptureIngress:
    def __init__(self):
        self.calls = []

    def consume(self, event_id, *, now, prior_material_sha256=None):
        self.calls.append((event_id, now, prior_material_sha256))
        return "CAPTURED"


class FakePMOwnerRead:
    """A local protocol double only; never Principal/provider readback evidence."""

    def __init__(self, ptr: PointerRecord):
        self.calls = 0
        self.ptr = ptr

    def read_current(self, claim_ref, packet_ref, queue_ref):
        self.calls += 1
        return PMOwnerCut(
            owner_ref=self.ptr.owner_ref,
            claim_ref=claim_ref,
            packet_ref=packet_ref,
            packet_sha256=self.ptr.packet_sha256,
            queue_ref=queue_ref,
            revision=self.ptr.revision,
            relation_to_hint="SAME",
            material_sha256="c" * 64,
            source_cut=self.ptr.source_cut,
            committed_readback=True,
            authenticated=True,
            material_ready=True,
        )


class X9SessionChannelPortTests(unittest.TestCase):
    def test_default_off_and_wrong_or_stale_session_fail_closed(self):
        api = FakeSessionAPI(authorized_identity())
        disabled = X9SessionChannelPort(api=api)
        self.assertFalse(disabled.identity().authenticated)
        self.assertEqual(api.calls, [])

        for bad in (authorized_identity(principal=""),
                    authorized_identity(session_ref=""),
                    authorized_identity(current=False),
                    authorized_identity(authenticated=False),
                    authorized_identity(scopes=frozenset({"pointer:read"}))):
            api = FakeSessionAPI(bad)
            port = X9SessionChannelPort(api=api, enabled=True)
            self.assertFalse(port.identity().authenticated)
            self.assertIsNone(port.read_pointer_by_event_id("event:1"))
            self.assertFalse(any(call[0] == "read_pointer_by_event_id" for call in api.calls))

    def test_exact_session_identity_is_fresh_and_scope_bound(self):
        api = FakeSessionAPI(authorized_identity())
        port = X9SessionChannelPort(api=api, enabled=True)
        identity = port.identity()
        self.assertEqual((identity.channel, identity.principal), (CHANNEL, X9_PRINCIPAL))
        self.assertTrue(identity.authenticated and identity.append_allowed and identity.read_allowed)
        self.assertEqual(api.calls, [("identity",)])

    def test_x9_session_cannot_forge_pm_sender_pointer(self):
        api = FakeSessionAPI(authorized_identity())
        port = X9SessionChannelPort(api=api, enabled=True)
        self.assertEqual(port.append_pointer_once(pointer()), "NOT_SENT")
        self.assertFalse(any(call[0] == "append_pointer_once" for call in api.calls))

    def test_pointer_readback_requires_bound_receiver_and_exact_provider_hash(self):
        api = FakeSessionAPI(authorized_identity(), pointer_value=pointer())
        port = X9SessionChannelPort(api=api, enabled=True)
        readback = port.read_pointer_by_event_id("event:1")
        self.assertIsNotNone(readback)
        self.assertEqual(readback.record.receiver_ref, X9_SESSION_REF)

        api.pointer_value = pointer(receiver="codex:local/other-session")
        self.assertIsNone(port.read_pointer_by_event_id("event:1"))
        self.assertIsNone(port.read_pointer_by_event_id("event:other"))

    def test_disposition_append_requires_exact_fresh_pointer_and_readback(self):
        ptr = pointer()
        api = FakeSessionAPI(authorized_identity(), pointer_value=ptr)
        port = X9SessionChannelPort(api=api, enabled=True)
        disp = disposition(ptr)
        self.assertEqual(port.append_disposition_once(disp), "ACCEPTED")
        self.assertEqual([c[0] for c in api.calls].count("read_pointer_by_event_id"), 1)
        self.assertEqual(port.read_disposition_by_event_id("event:1").record, disp)

        api.calls.clear()
        api.pointer_value = pointer(receiver="codex:local/other-session")
        api.disposition_value = None
        self.assertEqual(port.append_disposition_once(disp), "NOT_SENT")
        self.assertFalse(any(c[0] == "append_disposition_once" for c in api.calls))

    def test_missed_d0_rereads_one_owner_tuple_without_consuming(self):
        ptr = pointer()
        api = FakeSessionAPI(authorized_identity())
        channel = X9SessionChannelPort(api=api, enabled=True)
        ingress = CaptureIngress()
        pm = FakePMOwnerRead(ptr)
        result = consume_current_x9_pulse(ingress, channel, event_id=None, now=NOW,
                                          pm_reader=pm,
                                          recovery_scope=(ptr.claim_ref, ptr.packet_ref, ptr.queue_ref))
        self.assertIsInstance(result, PMOwnerCut)
        self.assertEqual(pm.calls, 1)
        self.assertEqual(ingress.calls, [])
        self.assertFalse(any(call[0] == "append_disposition_once" for call in api.calls))

    def test_existing_inbox_event_id_selects_data_without_granting_custody(self):
        api = FakeSessionAPI(authorized_identity())
        channel = X9SessionChannelPort(api=api, enabled=True)
        ingress = CaptureIngress()
        consume_current_x9_pulse(ingress, channel, event_id="event:1", now=NOW,
                                 prior_material_sha256="c" * 64)
        self.assertEqual(ingress.calls, [("event:1", NOW, "c" * 64)])
        api.identity_value = authorized_identity(current=False)
        result = consume_current_x9_pulse(ingress, channel, event_id="event:1", now=NOW)
        self.assertEqual(result.state, "REFINE_INGRESS_PORT_UNBOUND")
        self.assertEqual(len(ingress.calls), 1)

    def test_duplicate_pulse_returns_exact_prior_disposition_without_reappend(self):
        ptr = pointer()
        api = FakeSessionAPI(authorized_identity(), pointer_value=ptr)
        channel = X9SessionChannelPort(api=api, enabled=True)
        pm = FakePMOwnerRead(ptr)
        ingress = X9ChannelIngress(enabled=True, channel=channel, pm_reader=pm,
                                   producer_principal=PM, x9_principal=X9_PRINCIPAL,
                                   x9_session_ref=X9_SESSION_REF)

        first = consume_current_x9_pulse(ingress, channel, event_id=ptr.event_id, now=NOW)
        second = consume_current_x9_pulse(ingress, channel, event_id=ptr.event_id, now=NOW)

        self.assertEqual(first.state, "DISPOSITION_READBACK")
        self.assertEqual(second.state, "ALREADY_DISPOSED")
        self.assertFalse(first.disposition.work_consumed)
        self.assertEqual(pm.calls, 1)
        self.assertEqual([c[0] for c in api.calls].count("append_disposition_once"), 1)


if __name__ == "__main__":
    unittest.main()
