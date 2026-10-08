"""Thin caller contract; remote Context service tests prove the other side."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

import _support  # noqa: F401
from signalvev_sensing.model import canonical, sha256_hex
from signalvev_sensing.resolver import ResolveRequest, ResolverUnavailable
from signalvev_client import pm_x9
from signalvev_client import context_notice as notice
from signalvev_client import context_notice_host as host
from signalvev_client.pm_x9_synthetic import SyntheticWorld


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.world = SyntheticWorld()
        self.addCleanup(self.world.close)
        self.settings = replace(self.world.settings, mode=pm_x9.MODE_PRODUCTION)
        self.receipt_ref = "pm-receipt:new1"
        self.record = {
            "owner_ref": self.settings.owner_ref, "receipt_ref": self.receipt_ref,
            "event_id": "pm-event:new1", "referent_id": "pm-ready:new1",
            "owner_seq": 1, "revision_after": "pm-revision:new1", "revision_before": None,
            "claim_ref": self.settings.claim_ref, "packet_ref": self.settings.packet_ref,
            "queue_ref": self.settings.queue_ref, "packet_sha256": self.settings.packet_sha256,
            "ready_state": "MATERIAL_READY", "commit_ref": self.settings.attempt_ref,
            "snapshot_ref": "pm-snapshot:new1", "readback_ref": "pm-readback:new1",
            "observed_at": "2026-10-07T18:00:00+00:00", "way_home": ["way:pm"],
        }
        self.record["snapshot_sha256"] = sha256_hex(canonical(self.record))
        self.prepared = {"schema": notice.PREPARE_SCHEMA, "state": "DEPOSITED_READBACK",
                         "subject": "cerebro.v1.artifact.pointer",
                         "event_id": self.record["event_id"], "record": self.record,
                         "projection": {"material_sha256": "a" * 64, "source_cut": "pm-cut:new1"},
                         "pointer_sha256": "b" * 64}

    def test_preparation_maps_real_service_shape_to_owner_event(self):
        result = notice.parse_preparation({"structuredContent": self.prepared},
                                          receipt_ref=self.receipt_ref, settings=self.settings)
        self.assertEqual(result.raw_event["event_id"], self.record["event_id"])
        self.assertEqual(result.raw_event["delta"]["expected_sha256"], self.record["snapshot_sha256"])
        self.assertEqual(result.raw_event["commit"]["state"], "COMMITTED_READBACK")

    def test_no_send_on_hold_or_changed_receipt(self):
        call = Mock(return_value={**self.prepared, "state": "HOLD_UNREADABLE"})
        with patch.object(notice, "SendClient") as sender:
            host = notice.PMNoticePublisher(settings=self.settings, call_pm=call,
                                            client_config=self.world.ports().client_config)
            with self.assertRaises(notice.ContextNoticeError):
                host.send_receipt(self.receipt_ref)
            sender.return_value.connect.assert_not_called()
        changed = {**self.prepared, "record": {**self.record, "owner_ref": "other:owner"}}
        with self.assertRaises(notice.ContextNoticeError):
            notice.parse_preparation(changed, receipt_ref=self.receipt_ref, settings=self.settings)

    def test_publisher_uses_only_receipt_operation_then_existing_sender(self):
        call = Mock(return_value=self.prepared)
        with patch.object(notice, "SendClient") as sender:
            host = notice.PMNoticePublisher(settings=self.settings, call_pm=call,
                                            client_config=self.world.ports().client_config)
            host.send_receipt(self.receipt_ref)
            call.assert_called_once_with(notice.PREPARE, {"receipt_ref": self.receipt_ref})
            sender.return_value.send.assert_called_once()
            sender.return_value.close.assert_called_once()

    def test_x9_resolver_reads_fresh_cut_without_consume(self):
        req = ResolveRequest(event_id=self.record["event_id"], owner_ref=self.settings.owner_ref,
                             referent_type=pm_x9.PM_READY_HINT, referent_id=self.record["referent_id"],
                             depth="POINTER_GROUND", pointer_ref=self.record["snapshot_ref"],
                             expected_revision=self.record["revision_after"],
                             expected_sha256=self.record["snapshot_sha256"], owner_seq=1)
        observed = {"schema": notice.OBSERVE_SCHEMA, "state": "SAME",
                    "event_id": req.event_id,
                    "pointer": {"event_id": req.event_id, "owner_ref": req.owner_ref,
                                "referent_id": req.referent_id, "revision": req.expected_revision,
                                "expected_sha256": req.expected_sha256, "owner_seq": req.owner_seq},
                    "pm_current_cut": {"owner_ref": req.owner_ref, "revision": req.expected_revision,
                                       "relation_to_hint": "SAME", "packet_sha256": self.settings.packet_sha256,
                                       "authenticated": True, "committed_readback": True,
                                       "material_ready": True, "source_cut": "pm-cut:new1"}}
        observed["pointer_sha256"] = sha256_hex(canonical(observed["pointer"]))
        call = Mock(return_value={"structuredContent": observed})
        resolver = notice.RuntimeContextResolver(owner_ref=self.settings.owner_ref, call_runtime=call)
        result = resolver.resolve(req)
        self.assertEqual(result.revision_relation, "SAME")
        call.assert_called_once_with(notice.OBSERVE, {"event_id": req.event_id})
        observed["pm_current_cut"]["relation_to_hint"] = "SUPERSEDED"
        with self.assertRaises(ResolverUnavailable):
            resolver.resolve(req)

    def test_programmatic_routes_are_separate_and_inert(self):
        cfg = self.world.ports().client_config
        base = self.world.evidence_dir.parent
        sender = replace(cfg, interests=(), credentials_file=base / "pm.creds",
                         evidence_dir=base / "sender")
        receiver = replace(cfg, resolver_kind="factory", credentials_file=base / "x9.creds",
                           evidence_dir=base / "receiver")
        profile = {"enabled": True, "broker": cfg.server,
                   "publisher_credential_ref": str(sender.credentials_file),
                   "sender_state_dir": str(sender.evidence_dir),
                   "receiver_credential_ref": str(receiver.credentials_file),
                   "receiver_state_dir": str(receiver.evidence_dir)}
        settings = replace(self.settings, ports_factory=host.REMOTE_FACTORY)
        pm_call, runtime_call = Mock(), Mock()
        with (patch.object(host, "_profile", return_value=profile),
              patch.object(host, "_load_settings", return_value=settings),
              patch.object(host, "load_config", side_effect=[sender, receiver])):
            publisher = host.compose_pm(None, None, None, call_pm=pm_call)
            listener = host.compose_runtime(None, None, None, call_runtime=runtime_call)
        self.assertIsInstance(publisher, notice.PMNoticePublisher)
        self.assertIsInstance(listener._resolver, notice.RuntimeContextResolver)
        pm_call.assert_not_called()
        runtime_call.assert_not_called()
        self.assertIsNone(listener.receiver)

    def test_disabled_profile_and_old_factory_fail_before_calls(self):
        with patch.object(host, "_profile", return_value={"enabled": False}):
            with self.assertRaisesRegex(host.HostRefused, "PROFILE_DISABLED"):
                host.compose_pm(None, None, None, call_pm=Mock())
        with (patch.object(host, "_profile", return_value={"enabled": True}),
              patch.object(host, "_load_settings", return_value=self.settings)):
            with self.assertRaisesRegex(host.HostRefused, "REMOTE_CONTEXT_FACTORY_REQUIRED"):
                host.compose_runtime(None, None, None, call_runtime=Mock())
        with self.assertRaisesRegex(host.HostRefused, "REMOTE_CONTEXT_NOTICE_ROUTE_ONLY"):
            host.make_ports()

    def test_admitted_runtime_uses_off_lease_and_rechecks_before_each_resolution(self):
        cfg = self.world.ports().client_config
        base = self.world.evidence_dir.parent
        receiver = replace(cfg, resolver_kind="factory", credentials_file=base / "x9.creds",
                           evidence_dir=base / "receiver")
        profile = {"enabled": False, "broker": cfg.server,
                   "receiver_credential_ref": str(receiver.credentials_file),
                   "receiver_state_dir": str(receiver.evidence_dir)}
        with (patch.object(host, "CURRENT_PM_OWNER_REF", self.settings.owner_ref),
              patch.object(host, "_profile", return_value=profile),
              patch.object(host, "load_config", return_value=receiver),
              patch.object(host, "read_admitted_local_session", return_value={}) as admitted,
              patch.object(host, "resolve_current_notice",
                           return_value={"structuredContent": {"state": "HOLD"}}) as resolved):
            listener = host.compose_admitted_runtime(host.PROFILE_PATH, None)
            self.assertIsNone(listener.receiver)
            admitted.assert_called_once_with(profile_path=host.PROFILE_PATH, state_dir=host.STATE_DIR)
            listener._resolver.call_runtime(notice.OBSERVE, {"event_id": "pm-event:new1"})
            resolved.assert_called_once_with("pm-event:new1")
            with self.assertRaisesRegex(ResolverUnavailable, "TOOL_NOT_ALLOWED"):
                listener._resolver.call_runtime("other_tool", {"event_id": "pm-event:new2"})
            resolved.assert_called_once()
            resolved.side_effect = host.PortUnavailable("BK07_BINDING_MISMATCH")
            with self.assertRaisesRegex(ResolverUnavailable, "BK07_ADMISSION_NOT_CURRENT"):
                listener._resolver.call_runtime(notice.OBSERVE, {"event_id": "pm-event:new2"})
            self.assertEqual(resolved.call_count, 2)

        with (patch.object(host, "_profile", return_value={**profile, "enabled": True}),
              patch.object(host, "read_admitted_local_session") as admitted):
            with self.assertRaisesRegex(host.HostRefused, "PROFILE_MUST_REMAIN_OFF"):
                host.compose_admitted_runtime(host.PROFILE_PATH, None)
            admitted.assert_not_called()
        with self.assertRaisesRegex(host.HostRefused, "PROFILE_PATH_MISMATCH"):
            host.compose_admitted_runtime(base / "foreign-profile.json", None)
        with (patch.object(host, "_profile", return_value=profile),
              patch.object(host, "load_config", return_value=receiver),
              patch.object(host, "read_admitted_local_session") as admitted):
            with self.assertRaisesRegex(host.HostRefused, "RECEIVER_PROFILE_MISMATCH"):
                host.compose_admitted_runtime(host.PROFILE_PATH, None)
            admitted.assert_not_called()


if __name__ == "__main__":
    unittest.main()
