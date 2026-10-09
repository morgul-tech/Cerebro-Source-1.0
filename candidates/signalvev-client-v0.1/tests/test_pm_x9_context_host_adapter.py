"""Contract tests for the host-only Context adapter; fakes are never live proof."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import sys
import types
import unittest
from unittest.mock import patch

import _support  # noqa: F401
from signalvev_client import pm_x9
from signalvev_client.pm_x9_synthetic import SyntheticWorld

from signalvev_client import pm_x9_live_host as adapter  # noqa: E402


class Identity:
    def __init__(self, principal, session):
        self.tenant_ref = "tenant:test"
        self.workspace_ref = "workspace:test"
        self.principal_ref = principal
        self.consumer_ref = "consumer:" + principal
        self.session_ref = session

    def validate(self):
        pass


class McpToolCallContext:
    def __init__(self, identity):
        self.identity = identity
        self.private_headers = {"authorization": "private-test"}

    def transport_session(self):
        return {"session_ref": self.identity.session_ref}


class API:
    @contextmanager
    def _transaction(self, *, write):
        assert write is False
        yield None


class PostgresPmOwner:
    def __init__(self, assignment):
        self._assignment = assignment


class X9ProducerHost:
    def __init__(self, config, assignment, record):
        self._config = config
        self._pm_owner_assignment = assignment
        self.record = record
        self.api = API()

    def _channel(self, context):
        return object(), self.api

    def _receipt(self, api, receipt_ref):
        assert api is self.api
        assert receipt_ref == self.record["receipt_ref"]
        return self.record, {"source_cut": "cut:test", "material_sha256": "a" * 64}


class X9ReceiverHost:
    def __init__(self, config):
        self._config = config

    def _channel(self, context):
        return object(), API()


class Config:
    def __init__(self, producer, receiver):
        self.producer = types.SimpleNamespace(identity=producer)
        self.receiver = types.SimpleNamespace(identity=receiver)

    def validate(self):
        assert self.producer.identity.session_ref != self.receiver.identity.session_ref


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.world = SyntheticWorld()
        self.addCleanup(self.world.close)
        self.settings = replace(self.world.settings, mode=pm_x9.MODE_PRODUCTION)
        pm_identity = Identity(self.settings.producer_principal, "session:pm")
        x9_identity = Identity(self.settings.x9_principal, self.settings.x9_session_ref)
        self.config = Config(pm_identity, x9_identity)
        self.assignment = types.SimpleNamespace(owner_ref=self.settings.owner_ref)
        self.record = {"receipt_ref": "pm-receipt:fresh", "ready_state": "MATERIAL_READY",
                       "commit_ref": self.settings.attempt_ref,
                       "owner_ref": self.settings.owner_ref, "claim_ref": self.settings.claim_ref,
                       "packet_ref": self.settings.packet_ref, "queue_ref": self.settings.queue_ref,
                       "packet_sha256": self.settings.packet_sha256}
        self.owner = PostgresPmOwner(self.assignment)
        self.producer = X9ProducerHost(self.config, self.assignment, self.record)
        self.receiver = X9ReceiverHost(self.config)
        self.runtime = adapter.LiveRuntimeBindings(
            pm_owner=self.owner, producer_host=self.producer, receiver_host=self.receiver,
            pm_context=McpToolCallContext(pm_identity), x9_context=McpToolCallContext(x9_identity),
            receipt_ref=self.record["receipt_ref"], client_config=self.world.ports().client_config,
            clock=lambda: datetime.now(timezone.utc), enabled=True)
        modules = {"pm_owner_atomic": types.SimpleNamespace(PostgresPmOwner=PostgresPmOwner),
                   "x9_producer_host": types.SimpleNamespace(X9ProducerHost=X9ProducerHost),
                   "x9_receiver_host": types.SimpleNamespace(X9ReceiverHost=X9ReceiverHost),
                   "control_context_tools": types.SimpleNamespace(McpToolCallContext=McpToolCallContext)}
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def test_current_distinct_contexts_bind_existing_channels_without_send(self):
        ports = adapter.make_ports(self.settings, host_runtime=self.runtime)
        self.assertIsInstance(ports, pm_x9.PmX9HostPorts)
        self.assertIsNot(ports.producer_channel, ports.x9_channel)
        self.assertIsInstance(ports.pm_port, adapter._PmPort)
        self.assertIsInstance(ports.pm_reread_port, adapter._X9RereadPort)

    def test_unbound_without_host_runtime(self):
        with self.assertRaisesRegex(pm_x9.PmX9Unbound, "PM_X9_CURRENT_HOST_COMPOSITION_UNAVAILABLE"):
            adapter.make_ports(self.settings)

    def test_wrong_pm_session_and_private_headers_fail_closed(self):
        bad = replace(self.runtime, pm_context=McpToolCallContext(Identity(
            self.settings.producer_principal, "session:stale")))
        with self.assertRaises(pm_x9.PmX9Unbound):
            adapter.make_ports(self.settings, host_runtime=bad)
        self.runtime.pm_context.private_headers = {}
        with self.assertRaises(pm_x9.PmX9Unbound):
            adapter.make_ports(self.settings, host_runtime=self.runtime)

    def test_changed_receipt_and_settings_fail_closed(self):
        self.record["packet_sha256"] = "0" * 64
        with self.assertRaises(pm_x9.PmX9Unbound):
            adapter.make_ports(self.settings, host_runtime=self.runtime)
        self.record["packet_sha256"] = self.settings.packet_sha256
        with self.assertRaises(pm_x9.PmX9Unbound):
            adapter.make_ports(replace(self.settings, claim_ref="claim:wrong"), host_runtime=self.runtime)

    def test_x9_session_cannot_substitute_for_pm(self):
        with self.assertRaises(pm_x9.PmX9Unbound):
            adapter.make_ports(self.settings, host_runtime=replace(
                self.runtime, pm_context=self.runtime.x9_context))


if __name__ == "__main__":
    unittest.main()
