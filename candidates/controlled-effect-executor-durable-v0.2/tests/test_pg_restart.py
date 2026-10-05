"""Restart evidence on REAL PostgreSQL: the whole database server is crashed (``pg_ctl -m immediate``: no shutdown
checkpoint, WAL crash recovery on start) and every adapter/worker process is a fresh one afterwards.
These tests need the runner's own disposable cluster (they skip as UNRUN against an operator-supplied endpoint).
"""
from __future__ import annotations

import os
import signal
import unittest

import _pgworld as W
from _pgworld import PgCase, default_spec
from controlled_effect_executor_durable import verify_provenance_durable
import pg_harness as H


def run(job: dict) -> dict:
    rc, out, err = W.finish(W.spawn(job))
    assert rc == 0 and out is not None, (rc, err)
    return out


class ClusterCrashRestart(PgCase):
    def setUp(self) -> None:
        self.cluster = W.cluster()  # UNRUN (skip) when the cluster is not owned by this run
        super().setUp()

    def crash(self) -> None:
        self.cluster.restart("immediate")

    def test_3b_admission_survives_a_database_crash_and_executes_once_afterwards(self) -> None:
        spec = default_spec()
        adm = run({"role": "admit", "schema": self.w.schema, "spec_json": spec.canonical_text()})["decision"]
        self.assertEqual(adm["state"], "FENCED")
        self.crash()
        page = run({"role": "list", "schema": self.w.schema})["page"]
        self.assertEqual([(i["admission_ref"], i["state"], i["integrity_ok"], i["spec_json"]) for i in page],
                         [(adm["receipt"]["admission_ref"], "FENCED", True, spec.canonical_text())])
        done = run({"role": "execute", "schema": self.w.schema, "admission_ref": adm["receipt"]["admission_ref"]})["result"]
        self.assertEqual(done["state"], "COMMITTED_READBACK")
        self.assertEqual(self.w.provider.counts(), {"calls": 1, "applied": 1})
        self.assertEqual(self.w.count("cee_admission"), 1)
        self.assertEqual(verify_provenance_durable(self.w.store.provenance(adm["receipt"]["admission_ref"])), ())
        self.assertEqual(self.w.store.verify_projection(adm["receipt"]["admission_ref"]), ())

    def test_4e_database_crash_while_the_provider_call_is_in_progress_stays_unknown(self) -> None:
        spec, receipt = self.w.fence()
        ref = receipt.admission_ref
        sig = W.scratch("crash_db_in_call")
        proc = W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": ref,
                        "hook": {"point": "provider.before_effect", "action": "signal_then_block", "signal_file": sig}})
        H.wait_until(lambda: os.path.exists(sig), what="worker inside the provider call")
        self.crash()  # the reservation was committed BEFORE the call: it is in the WAL and must come back
        proc.send_signal(signal.SIGKILL)
        self.assertEqual(W.finish(proc)[0], -signal.SIGKILL)
        prog = self.w.store.progress_of(ref)
        self.assertEqual((prog.state, prog.last_seq, prog.ack_recorded), ("IN_FLIGHT", 2, False))
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 0})
        page = run({"role": "list", "schema": self.w.schema})["page"]
        self.assertEqual([(i["state"], i["attempt_ref"]) for i in page], [("IN_FLIGHT", prog.attempt_ref)])
        rec = run({"role": "recover", "schema": self.w.schema, "admission_ref": ref})["result"]
        self.assertEqual((rec["state"], rec["reason_code"]), ("UNKNOWN_EFFECT", "RECOVERY_ATTEMPT_RESULT_NOT_RECORDED"))
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 0})  # recovery never re-called it
        self.assertEqual(verify_provenance_durable(self.w.store.provenance(ref)), ())

    def test_6g_late_commit_delivered_after_a_database_crash_is_found_by_readback_not_replayed(self) -> None:
        provider = W.make_provider(self.w.schema, apply_mode="delay_commit")
        store, ex = self.w.new_executor(provider=provider)
        spec, receipt = self.w.fence()
        ref = receipt.admission_ref
        self.assertEqual(ex.execute(receipt, spec).state, "UNKNOWN_EFFECT")
        self.assertEqual(ex.recover(ref).reason_code, "LATE_COMMIT_STILL_POSSIBLE_SETTLEMENT_PENDING")
        self.crash()
        self.assertEqual(run({"role": "recover", "schema": self.w.schema, "admission_ref": ref,
                              })["result"]["state"], "UNKNOWN_EFFECT")
        provider.deliver(ref)  # the provider's slow queue finally commits, after the crash and an empty read
        rec = run({"role": "recover", "schema": self.w.schema, "admission_ref": ref})["result"]
        self.assertEqual((rec["state"], rec["reason_code"]), ("COMMITTED_READBACK", "READBACK_MATCHES_INTENDED_STATE"))
        self.assertEqual(provider.counts(ref), {"calls": 1, "applied": 1})

    def test_7b_revocation_and_the_immutable_basis_are_visible_after_reconnect_and_crash(self) -> None:
        spec, receipt = self.w.fence()
        before = self.w.q(f"SELECT spec_canonical, receipt_canonical, batch_digest FROM {self.w.schema}.cee_admission")
        rev = self.w.owner.revoke("SYNTH-DELEG-1")
        self.crash()
        # a brand-new process, new connections: the revocation is the committed truth, a NEW key is refused
        fresh = default_spec(idempotency_key="SYNTH-AFTER-CRASH")
        denied = run({"role": "admit", "schema": self.w.schema, "spec_json": fresh.canonical_text()})["decision"]
        self.assertEqual((denied["state"], denied["reason_code"]), ("DENIED", "DELEGATION_REVOKED"))
        self.assertEqual(self.w.count("cee_admission"), 1)
        self.assertGreater(rev, receipt.delegation_currentness)
        # the exactly-admitted batch is byte-identical and still listed; the same-key retry is an exact replay
        after = self.w.q(f"SELECT spec_canonical, receipt_canonical, batch_digest FROM {self.w.schema}.cee_admission")
        self.assertEqual(after, before)
        replay = run({"role": "admit", "schema": self.w.schema, "spec_json": spec.canonical_text()})["decision"]
        self.assertEqual((replay["reason_code"], replay["replayed"]), ("EXACT_REPLAY", True))
        self.assertEqual(len(run({"role": "list", "schema": self.w.schema})["page"]), 1)


if __name__ == "__main__":
    unittest.main()
