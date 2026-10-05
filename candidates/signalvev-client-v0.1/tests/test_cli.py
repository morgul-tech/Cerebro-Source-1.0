"""Foreground CLI: check-config | health | send | listen (fake broker; offline)."""
import io
import json
import threading
import time
import unittest

from _support import FakeBroker, TmpCase, config_doc, fixture_entry, raw_event, synthetic_fixture_doc
from signalvev_client.cli import main


def run(argv, broker=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, connect_fn=broker.connect if broker else None, stdout=out, stderr=err)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


class CliTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.broker = FakeBroker()
        (self.tmp / "owner.json").write_text(json.dumps(synthetic_fixture_doc(fixture_entry())))
        (self.tmp / "event.json").write_text(json.dumps(raw_event()))
        self.cfgfile = self.tmp / "client.toml"
        self.cfgfile.write_text(
            '[node]\nid = "node-cli"\n[nats]\nserver = "nats://127.0.0.1:4222"\nflush_timeout_seconds = 0.3\n'
            'connect_timeout_seconds = 1.0\n[evidence]\ndir = "ev"\n[send]\nttl_seconds = 30\n'
            '[[listen.interest]]\nowner_ref = "owner:test-synthetic"\nreferent_type = "doc"\n'
            '[resolver]\nkind = "synthetic_fixture"\nfixture_file = "owner.json"\n')
        self.c = ["--config", str(self.cfgfile)]

    def test_check_config_is_offline_and_prints_a_non_secret_summary(self):
        code, (res,) = run(["check-config", *self.c], self.broker)
        self.assertEqual((code, res["ok"], res["network"]), (0, True, "NOT_USED"))
        self.assertEqual(res["config"]["node_id"], "node-cli")
        self.assertEqual(self.broker.connects, [])

    def test_bad_or_missing_config_exits_2_with_a_typed_error(self):
        code, (res,) = run(["check-config", "--config", str(self.tmp / "nope.toml")])
        self.assertEqual((code, res["ok"], res["error"]), (2, False, "CONFIG_UNREADABLE"))
        self.cfgfile.write_text('[node]\nid = "n"\n[nats]\nserver = "nats://u:p@127.0.0.1"\n[evidence]\ndir = "ev"\n')
        code, (res,) = run(["health", *self.c])
        self.assertEqual((code, res["error"]), (2, "INLINE_SECRET_NOT_ALLOWED"))

    def test_health_offline_by_default_and_honest_when_connecting(self):
        code, (res,) = run(["health", *self.c], self.broker)
        self.assertEqual((code, res["ok"], res["transport"]), (0, True, {"checked": False}))
        self.assertEqual(self.broker.connects, [])
        code, (res,) = run(["health", "--connect", *self.c], self.broker)
        self.assertEqual((code, res["transport"]["result"]), (0, "CONNECTED_AND_SERVER_ROUNDTRIP_OK"))
        self.assertIn("peer receiver liveness", res["not_proof_of"])
        self.assertEqual(res["claims"]["peer_receiver_liveness"], "NOT_PROVEN")
        self.broker.refuse_connect = True
        code, (res,) = run(["health", "--connect", *self.c], self.broker)
        self.assertEqual((code, res["ok"], res["transport"]["result"]), (5, False, "CONNECT_FAILED"))

    def test_listen_exits_5_when_the_server_never_answers_the_subscription_roundtrip(self):
        self.broker.flush_error = True
        code, lines = run(["listen", *self.c, "--duration", "1"], self.broker)
        self.assertEqual(code, 5)
        self.assertEqual((lines[-1]["ok"], lines[-1]["error"]), (False, "SUBSCRIPTION_ROUNDTRIP_FAILED"))
        self.assertFalse(any(t.name == "signalvev-dispatch" and t.is_alive() for t in threading.enumerate()))

    def test_output_is_ascii_safe_for_legacy_consoles(self):
        out = io.StringIO()
        from signalvev_client.cli import _emit
        _emit(out, {"detail": "blåbær"})
        out.getvalue().encode("ascii")

    def test_send_outcomes_map_to_exit_codes_and_say_transport_only(self):
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "event.json")], self.broker)
        self.assertEqual((code, res["state"], res["ok"]), (0, "ACCEPTED", True))
        self.assertIn("transport evidence only", res["note"])
        self.assertEqual(res["claims"]["delivered"], "NOT_PROVEN")
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "event.json")], self.broker)   # same event again
        self.assertEqual((code, res["state"], res["replay_refused"]), (0, "ACCEPTED", True))
        self.assertEqual(len(self.broker.publish_calls), 1)

    def test_not_sent_and_unknown_send_have_distinct_exit_codes_and_unknown_is_not_replayed(self):
        self.broker.script = ["refuse_closed"]
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "event.json")], self.broker)
        self.assertEqual((code, res["state"]), (3, "NOT_SENT"))
        self.broker.script = ["oserror"]
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "event.json")], self.broker)   # NOT_SENT may be retried
        self.assertEqual((code, res["state"]), (4, "UNKNOWN_SEND"))
        calls = len(self.broker.publish_calls)
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "event.json")], self.broker)
        self.assertEqual((code, res["state"], res["replay_refused"]), (4, "UNKNOWN_SEND", True))
        self.assertEqual(len(self.broker.publish_calls), calls)

    def test_invalid_event_never_touches_the_network(self):
        bad = raw_event()
        bad["commit"] = {"state": "WRITTEN"}
        (self.tmp / "bad.json").write_text(json.dumps(bad))
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "bad.json")], self.broker)
        self.assertEqual((code, res["error"], res["network"]), (2, "OWNER_EVENT_NOT_COMMITTED", "NOT_USED"))
        (self.tmp / "junk.json").write_text("{nope")
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "junk.json")], self.broker)
        self.assertEqual((code, res["error"]), (2, "EVENT_UNREADABLE"))
        self.assertEqual(self.broker.connects, [])

    def test_send_connect_failure_exits_5_and_reports_nothing_published(self):
        self.broker.refuse_connect = True
        code, (res,) = run(["send", *self.c, "--event", str(self.tmp / "event.json")], self.broker)
        self.assertEqual((code, res["published"], res["error"]), (5, False, "CONNECT_FAILED"))

    def test_listen_prints_ingress_lines_and_ends_cleanly(self):
        box = {}
        t = threading.Thread(target=lambda: box.update(r=run(["listen", *self.c, "--duration", "8", "--max-frames", "1"], self.broker)))
        t.start()
        deadline = time.monotonic() + 5
        while not self.broker.subs.get("cerebro.v1.state.delta") and time.monotonic() < deadline:
            time.sleep(0.02)
        code, (res,) = run(["send", "--config", str(self.cfgfile), "--event", str(self.tmp / "event.json")], self.broker)
        self.assertEqual(code, 0)
        t.join(10)
        self.assertFalse(t.is_alive())
        code, lines = box["r"]
        self.assertEqual(code, 0)
        kinds = [l.get("event") for l in lines]
        self.assertEqual(kinds, ["LISTEN_START", "INGRESS", "LISTEN_END"])
        self.assertEqual(lines[0]["resolver"], "SYNTHETIC_FIXTURE_NOT_OWNER_TRUTH")
        self.assertEqual((lines[1]["disposition"], lines[1]["claim"]), ("ACK_READ", "ACK_READ != WORK_CONSUMED != EFFECT"))
        self.assertEqual(lines[2]["reason"], "MAX_FRAMES_REACHED")

    def test_listen_without_fixture_or_connection_fails_closed(self):
        self.cfgfile.write_text(self.cfgfile.read_text().replace('fixture_file = "owner.json"\n', ""))
        code, (res,) = run(["listen", *self.c, "--duration", "1"], self.broker)
        self.assertEqual((code, res["error"]), (2, "SYNTHETIC_FIXTURE_REQUIRED"))
        self.cfgfile.write_text(self.cfgfile.read_text().replace('kind = "synthetic_fixture"\n', 'kind = "synthetic_fixture"\nfixture_file = "owner.json"\n'))
        self.broker.refuse_connect = True
        code, (res,) = run(["listen", *self.c, "--duration", "1"], self.broker)
        self.assertEqual((code, res["error"]), (5, "CONNECT_FAILED"))

    def test_listen_connection_loss_exits_6(self):
        box = {}
        t = threading.Thread(target=lambda: box.update(r=run(["listen", *self.c, "--duration", "10"], self.broker)))
        t.start()
        deadline = time.monotonic() + 5
        while not self.broker.connects and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        for nc in self.broker.connects:
            nc.is_closed, nc.is_connected = True, False
        t.join(10)
        code, lines = box["r"]
        self.assertEqual((code, lines[-1]["reason"]), (6, "CONNECTION_LOST"))


if __name__ == "__main__":
    unittest.main()
