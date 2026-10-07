"""Changed seam: scoped normal sender/listener composition without X9 auto-consume."""
import json
import sys
import types
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import _support  # noqa: F401
from signalvev_client import pm_x9
from signalvev_client import pm_x9_normal_host as host
from signalvev_client.pm_x9_synthetic import SyntheticWorld


class NormalHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.sender_cred = base / "producer.creds"
        self.receiver_cred = base / "receiver.creds"
        self.sender_cred.write_text("test-reference")
        self.receiver_cred.write_text("test-reference")
        self.sender_state = base / "sender"
        self.receiver_state = base / "receiver"
        self.profile = base / "profile.json"
        self.doc = {"schema": host.PROFILE_SCHEMA, "enabled": True, "mode": "PRODUCTION",
                    "broker": "nats://10.77.0.1:4222", "subject": host.SUBJECT,
                    "publisher_credential_ref": str(self.sender_cred),
                    "receiver_credential_ref": str(self.receiver_cred),
                    "sender_state_dir": str(self.sender_state),
                    "receiver_state_dir": str(self.receiver_state),
                    "rights": {"no_new_acl": True, "wildcards_forbidden": True,
                               "publisher_pub_only": [host.SUBJECT],
                               "receiver_sub_only": [host.SUBJECT]},
                    "x9_operation": {"host_auto_consume": False, "context_append_from_host": False}}
        self._write_profile()

    def _write_profile(self):
        self.profile.write_text(json.dumps(self.doc), encoding="utf-8")

    def test_composition_uses_distinct_scoped_configs(self):
        world = SyntheticWorld()
        self.addCleanup(world.close)
        settings = replace(world.settings, mode=pm_x9.MODE_PRODUCTION,
                           ports_factory=host.QUALIFIED_FACTORY)
        sender = replace(world.ports().client_config, server=self.doc["broker"],
                         credentials_file=self.sender_cred, evidence_dir=self.sender_state,
                         interests=())
        receiver = replace(sender, credentials_file=self.receiver_cred, evidence_dir=self.receiver_state,
                           interests=world.ports().client_config.interests, resolver_kind="factory")
        marker = object()
        provider = types.ModuleType("providers.pm_x9_live_host")
        class LiveRuntimeBindings:
            enabled = True
        provider.LiveRuntimeBindings = LiveRuntimeBindings
        provider.make_ports = Mock(return_value=world.ports())
        runtime = LiveRuntimeBindings()
        with (patch.object(host, "_load_settings", return_value=settings),
              patch.object(host, "load_config", side_effect=[sender, receiver]),
              patch.dict(sys.modules, {"providers.pm_x9_live_host": provider}),
              patch.object(pm_x9, "build_binding", return_value=marker) as build):
            got = host.compose(self.profile, Path("binding"), Path("sender"), Path("receiver"),
                               host_runtime=runtime)
        self.assertIs(got, marker)
        provider.make_ports.assert_called_once_with(settings, host_runtime=runtime)
        self.assertEqual(build.call_args.args[1].client_config, sender)
        self.assertEqual(build.call_args.args[1].receiver_client_config, receiver)

    def test_alternate_factory_and_missing_current_host_refused(self):
        world = SyntheticWorld()
        self.addCleanup(world.close)
        alternate = replace(world.settings, mode=pm_x9.MODE_PRODUCTION,
                            ports_factory="some.other:make_ports")
        with patch.object(host, "_load_settings", return_value=alternate):
            with self.assertRaisesRegex(host.HostRefused, "QUALIFIED_HOST_FACTORY_REQUIRED"):
                host.compose(self.profile, Path("binding"), Path("sender"), Path("receiver"))
        pinned = replace(alternate, ports_factory=host.QUALIFIED_FACTORY)
        with patch.object(host, "_load_settings", return_value=pinned), \
             patch.object(host.importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(host.HostRefused, "CURRENT_HOST_BACKING_UNAVAILABLE"):
                host.compose(self.profile, Path("binding"), Path("sender"), Path("receiver"))

    def test_profile_disabled_and_wildcard_refused_before_binding(self):
        self.doc["enabled"] = False
        self._write_profile()
        with self.assertRaisesRegex(host.HostRefused, "PROFILE_DISABLED"):
            host.compose(self.profile, Path("binding"), Path("sender"), Path("receiver"))
        self.doc["enabled"] = True
        self.doc["rights"]["receiver_sub_only"] = [">" ]
        self._write_profile()
        with self.assertRaisesRegex(host.HostRefused, "RIGHTS_PROFILE_MISMATCH"):
            host.compose(self.profile, Path("binding"), Path("sender"), Path("receiver"))

    def test_send_receipt_only_never_consumes_x9(self):
        binding = Mock()
        binding.send_hint.return_value = pm_x9.HintSendResult("PM_EVIDENCE_REJECTED", "NOT_CURRENT")
        with patch.object(host, "compose", return_value=binding):
            result = host.main(["--profile", str(self.profile), "--binding-config", "binding",
                                "--sender-config", "sender", "--receiver-config", "receiver",
                                "send", "pm-receipt:current"])
        self.assertEqual(result, 0)
        binding.send_hint.assert_called_once_with("pm-receipt:current")
        binding.consume_one.assert_not_called()
        binding.pulse.assert_not_called()
        binding.start_listener.assert_not_called()
        binding.close.assert_called_once()

    def test_retained_listener_uses_receiver_config(self):
        world = SyntheticWorld()
        self.addCleanup(world.close)
        ports = world.ports()
        receiver = replace(ports.client_config, evidence_dir=self.receiver_state)
        binding = pm_x9.PmX9Binding(world.settings, replace(ports, receiver_client_config=receiver))
        with patch.object(pm_x9, "ListenClient") as listener:
            binding.start_listener()
        self.assertIs(listener.call_args.args[0], receiver)
        binding.consume_one = Mock(side_effect=AssertionError("X9 consume belongs to X9"))
        binding.close()

    def test_production_rejects_shared_nats_identity(self):
        world = SyntheticWorld()
        self.addCleanup(world.close)
        settings = replace(world.settings, mode=pm_x9.MODE_PRODUCTION)
        config = replace(world.ports().client_config, credentials_file=self.sender_cred,
                         evidence_dir=self.sender_state)
        ports = replace(world.ports(), client_config=config, receiver_client_config=config)
        diagnostics = pm_x9.diagnose(settings, ports)
        self.assertIn({"port": "receiver_client_config", "status": "INVALID",
                       "detail": "production needs separate receiver credentials/state and PM_READY_HINT interest"}, diagnostics)


if __name__ == "__main__":
    unittest.main()
