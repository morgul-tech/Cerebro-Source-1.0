"""Focused Source-host lifecycle controls; no provider or network operation."""
from __future__ import annotations

import sys
import unittest
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "candidates" / "signalvev-client-v0.1" / "src"),
                str(REPO / "candidates" / "signalvev-sensing-runtime-v0.1"),
                str(REPO / "candidates" / "signalvev-sensing-runtime-v0.1" / "src"),
                str(REPO / "mcp"), str(REPO)]

from providers.pm_x9_live_host import LiveRuntimeBindings  # noqa: E402
from signalvev_client.pm_x9 import (ConsumeResult, DepositRecord, HintSendResult,
                                     PmX9Binding, PmX9Settings, PmX9Unbound)  # noqa: E402
from signalvev_client import pm_x9, pm_x9_host  # noqa: E402


def settings(mode="PRODUCTION"):
    return PmX9Settings(mode, "owner:pm", "claim:one", "packet:one", "queue:one",
                        "a" * 64, "principal:pm", "principal:x9", "session:x9",
                        "attempt:one", ports_factory="providers.pm_x9_live_host:make_ports")


def runtime(pm_readback=None):
    return LiveRuntimeBindings(pm_readback, None, None, None, None, None, None,
                               None, None, None, enabled=True)


class FakeBinding:
    def __init__(self, send, deposit=None, consume=None, *, start_error=None):
        self.send = send
        self.sink = SimpleNamespace(records=deque([deposit] if deposit else []))
        self.consume = consume
        self.start_error = start_error
        self.calls = []
        self.ingress_count = 0

    def start_listener(self):
        self.calls.append("listen")
        if self.start_error:
            raise self.start_error

    def open_sender(self):
        self.calls.append("open")

    def send_hint(self, receipt):
        self.calls.append(("send", receipt))
        return self.send

    def wait_for_ingress(self, count, timeout):
        self.calls.append("wait")
        return False

    def consume_one(self, event_id):
        self.calls.append(("consume", event_id))
        return self.consume

    def close(self):
        self.calls.append("close")


class HostEntryTests(unittest.TestCase):
    def test_default_off_and_missing_current_mapping_stop_before_provider_port(self):
        with patch.object(pm_x9_host, "load_ports") as load:
            with self.assertRaisesRegex(PmX9Unbound, "PM_X9_DEFAULT_OFF"):
                pm_x9_host.compose(settings("OFF"), runtime(), host_authorized=True)
            with self.assertRaisesRegex(PmX9Unbound, "PM_X9_PRODUCTION_MODE_REQUIRED"):
                pm_x9_host.compose(settings("SYNTHETIC_TEST_ONLY"), runtime(), host_authorized=True)
            with self.assertRaisesRegex(PmX9Unbound, "PM_X9_TRUSTED_HOST_FACTORY_REQUIRED"):
                pm_x9_host.compose(replace(settings(), ports_factory="arbitrary.module:factory"),
                                   runtime(), host_authorized=True)
            load.assert_not_called()
        null_pm = {"project": {"project_ref": "PROJECT", "revision": 1},
                   "authenticated_caller": {"schema": "cerebro-authenticated-project-read-caller/v2",
                                            "oauth_verified": True, "consumer_source": "SERVICE_BOUND",
                                            "transport_session": {"status": "AVAILABLE", "authority": "TRANSPORT_METADATA_ONLY"},
                                            "persisted_session": None, "session_status": "NO_CURRENT_PERSISTED_SESSION"}}
        with self.assertRaisesRegex(PmX9Unbound, "PM_X9_CURRENT_HOST_COMPOSITION_UNAVAILABLE"):
            pm_x9_host.compose(settings(), runtime(null_pm), host_authorized=True)

    def test_unknown_send_returns_exact_event_without_wait_consume_or_retry(self):
        b = FakeBinding(HintSendResult("UNKNOWN_SEND", "UNKNOWN_SEND_NO_REPLAY", "event:one", "attempt:one"))
        with patch.object(pm_x9_host, "compose", return_value=b):
            result = pm_x9_host.run_one_existing_ready_receipt(
                settings(), runtime(), "receipt:one", host_authorized=True,
                receiver_consume_authorized=True)
        self.assertEqual((result.state, result.send.event_id), ("SEND_UNCONFIRMED", "event:one"))
        self.assertEqual(b.calls, ["listen", "open", ("send", "receipt:one"), "close"])

    def test_only_matching_deposit_allows_fresh_consume_and_typed_readback(self):
        send = HintSendResult("TRANSPORT_ACCEPTED", "ACCEPTED", "event:one", "attempt:one")
        other = DepositRecord("event:other", "DEPOSITED_READBACK", "OK", "ACK_READ", queued_for_pulse=True)
        b = FakeBinding(send, other)
        with patch.object(pm_x9_host, "compose", return_value=b):
            missing = pm_x9_host.run_one_existing_ready_receipt(
                settings(), runtime(), "receipt:one", host_authorized=True,
                receiver_consume_authorized=True)
        self.assertEqual(missing.state, "INGRESS_UNCONFIRMED")
        self.assertFalse(any(isinstance(c, tuple) and c[0] == "consume" for c in b.calls))
        self.assertEqual(b.calls[-1], "close")

        deposit = DepositRecord("event:one", "DEPOSITED_READBACK", "READBACK", "ACK_READ",
                                pointer_sha256="b" * 64, queued_for_pulse=True)
        consume = ConsumeResult("DISPOSITION_READBACK", "READBACK", "event:one", "MATERIAL_ROUTE",
                                record=SimpleNamespace(event_id="event:one", disposition="MATERIAL_ROUTE"),
                                pointer_sha256="b" * 64)
        b = FakeBinding(send, deposit, consume)
        with patch.object(pm_x9_host, "compose", return_value=b):
            result = pm_x9_host.run_one_existing_ready_receipt(
                settings(), runtime(), "receipt:one", host_authorized=True,
                receiver_consume_authorized=True)
        self.assertEqual((result.state, result.send, result.deposit, result.consume),
                         ("DISPOSITION_READBACK", send, deposit, consume))
        self.assertEqual(b.calls[-2:], [("consume", "event:one"), "close"])

        wrong = ConsumeResult("DISPOSITION_READBACK", "READBACK", "event:one", "MATERIAL_ROUTE",
                              record=SimpleNamespace(event_id="event:other", disposition="MATERIAL_ROUTE"),
                              pointer_sha256="b" * 64)
        b = FakeBinding(send, deposit, wrong)
        with patch.object(pm_x9_host, "compose", return_value=b):
            mismatched = pm_x9_host.run_one_existing_ready_receipt(
                settings(), runtime(), "receipt:one", host_authorized=True,
                receiver_consume_authorized=True)
        self.assertEqual(mismatched.state, "CONSUME_UNCONFIRMED")

    def test_partial_start_failure_is_type_only_and_closes(self):
        b = FakeBinding(None, start_error=RuntimeError("credential value must not escape"))
        with patch.object(pm_x9_host, "compose", return_value=b):
            result = pm_x9_host.run_one_existing_ready_receipt(
                settings(), runtime(), "receipt:one", host_authorized=True,
                receiver_consume_authorized=True)
        self.assertEqual((result.state, result.error_type), ("OPERATION_UNCONFIRMED", "RuntimeError"))
        self.assertNotIn("credential value", repr(result))
        self.assertEqual(b.calls, ["listen", "close"])

    def test_partial_client_startup_releases_its_own_resource(self):
        class Sender:
            def __init__(self, *args, **kwargs):
                self.closed = False

            def connect(self):
                raise RuntimeError("connect failed")

            def close(self):
                self.closed = True

        sender = Sender()
        fake = SimpleNamespace(_sender=None, ports=SimpleNamespace(client_config=None, connect_fn=None),
                               _epoch=lambda: 0)
        with patch.object(pm_x9, "SendClient", return_value=sender):
            with self.assertRaises(RuntimeError):
                PmX9Binding.open_sender(fake)
        self.assertTrue(sender.closed)
        self.assertIsNone(fake._sender)

        class Listener:
            def __init__(self, *args, **kwargs):
                self.stopped = False

            def start(self):
                raise RuntimeError("listen failed")

            def stop(self):
                self.stopped = True

        listener = Listener()
        fake = SimpleNamespace(_listener=None, ports=SimpleNamespace(client_config=None, connect_fn=None),
                               resolver=None, sink=None, _epoch=lambda: 0, _on_result=lambda result: None)
        with patch.object(pm_x9, "ListenClient", return_value=listener):
            with self.assertRaises(RuntimeError):
                PmX9Binding.start_listener(fake)
        self.assertTrue(listener.stopped)
        self.assertIsNone(fake._listener)


if __name__ == "__main__":
    unittest.main()
