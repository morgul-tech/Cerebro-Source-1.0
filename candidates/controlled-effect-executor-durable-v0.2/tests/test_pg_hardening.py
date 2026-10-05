"""Findings of the independent review, as executable evidence on REAL PostgreSQL: scope binding everywhere, detection
tools that survive malformed rows, no unbounded ledger growth under periodic recovery, a deterministic cross-process
compare-and-set loser, and an atomic synthetic-provider fence."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import unittest

import _pgworld as W
from _pgworld import PgCase, default_spec
from controlled_effect_executor.errors import LedgerBasisError
import pg_harness as H

SCOPE_B = "SYNTH-EXECUTOR-SCOPE-B"


def run(job: dict) -> dict:
    rc, out, err = W.finish(W.spawn(job))
    assert rc == 0 and out is not None, (rc, err)
    return out


def blocked(world: W.PgWorld, app: str) -> int:
    return int(world.q("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = %s"
                       " AND wait_event_type = 'Lock'", (app,))[0]["n"])


class ScopeIsEnforcedEverywhere(PgCase):
    def test_another_scope_cannot_replay_read_execute_or_recover_an_admission(self) -> None:
        spec, receipt = self.w.fence()
        ref = receipt.admission_ref
        store_b, ex_b = self.w.new_executor(scope=SCOPE_B)
        d = store_b.admit(spec, spec.digest)  # same key, same spec, other scope
        self.assertEqual((d.state, d.reason_code, d.receipt), ("DENIED", "IDEMPOTENCY_KEY_BELONGS_TO_ANOTHER_SCOPE", None))
        self.assertEqual((store_b.progress_of(ref), store_b.get_admission(ref), store_b.state_of(ref)), (None, None, None))
        with self.assertRaises(LedgerBasisError):
            store_b.provenance(ref)
        self.assertEqual(store_b.list_unresolved().items, ())
        res = ex_b.execute(receipt, spec)  # holding A's receipt and spec grants nothing in scope B
        self.assertEqual((res.state, res.reason_code), ("DENIED", "ADMISSION_NOT_FOUND"))
        for call in (ex_b.recover, ex_b.status):
            self.assertEqual(call(ref).reason_code, "ADMISSION_NOT_FOUND")
        self.assertEqual(ex_b.declare_in_flight_lost(ref, owner_reason_ref="SYNTH-R").reason_code, "ADMISSION_NOT_FOUND")
        self.assertEqual(self.w.provider.counts(), {"calls": 0, "applied": 0})
        self.assertEqual(self.w.store.state_of(ref), "FENCED")  # untouched
        # the owning scope still works
        self.assertEqual(self.w.executor.execute(receipt, spec).state, "COMMITTED_READBACK")


class DetectionToolsSurviveMalformedRows(PgCase):
    def _junk_admission(self, spec_text: str = '{"junk":true}') -> str:
        s, ref = self.w.schema, "ADM-JUNK0001"
        digest = hashlib.sha256(spec_text.encode()).hexdigest()
        self.w.sql(f"INSERT INTO {s}.cee_admission (admission_ref, scope_ref, idempotency_key, batch_digest,"
                   f" work_order_ref, effect_ref, spec_canonical, receipt_canonical, receipt_fingerprint)"
                   f" VALUES (%s, %s, 'SYNTH-JUNK', %s, 'wo', 'ef', %s, '{{}}', %s)",
                   (ref, W.SCOPE, digest, spec_text, "a" * 64))
        canon = json.dumps({"admission_ref": ref, "attempt_ref": None, "batch_digest": digest, "detail": {},
                            "event_fingerprint": "b" * 64, "event_kind": "ADMISSION_FENCED", "event_ref": "EVT-JUNK",
                            "prev_fingerprint": "a" * 64, "reason_code": "X", "schema": "x", "seq": 1,
                            "state_after": "FENCED"}, sort_keys=True)
        self.w.sql(f"INSERT INTO {s}.cee_event (admission_ref, seq, event_ref, event_kind, state_after, attempt_ref,"
                   f" event_canonical, event_fingerprint, prev_fingerprint) VALUES (%s, 1, 'EVT-JUNK', 'ADMISSION_FENCED',"
                   f" 'FENCED', NULL, %s, %s, %s)", (ref, canon, "b" * 64, "a" * 64))
        self.w.sql(f"INSERT INTO {s}.cee_progress (admission_ref, scope_ref, admit_seq, last_seq, state, ack_recorded)"
                   f" SELECT admission_ref, scope_ref, admit_seq, 1, 'FENCED', false FROM {s}.cee_admission"
                   f" WHERE admission_ref = %s", (ref,))
        return ref

    def test_one_unparseable_admission_does_not_hide_the_healthy_ones(self) -> None:
        spec, receipt = self.w.fence()
        junk = self._junk_admission()
        page = self.w.store.list_unresolved()
        by_ref = {i.admission_ref: i for i in page.items}
        self.assertEqual(set(by_ref), {receipt.admission_ref, junk})
        self.assertEqual((by_ref[junk].integrity_ok, by_ref[junk].spec, by_ref[junk].receipt), (False, None, None))
        self.assertTrue(by_ref[receipt.admission_ref].integrity_ok)
        # reading/recovering the junk row is a refusal, never a crash and never a provider call
        res = self.w.executor.recover(junk)
        self.assertEqual((res.state, res.reason_code), ("DENIED", "STORED_ADMISSION_UNPARSEABLE"))
        self.assertEqual(self.w.provider.counts(), {"calls": 0, "applied": 0})
        self.assertEqual(self.w.executor.execute(receipt, spec).state, "COMMITTED_READBACK")  # healthy rows still work

    def test_verify_projection_reports_instead_of_crashing_on_a_corrupt_event(self) -> None:
        spec, receipt = self.w.fence()
        ref, s = receipt.admission_ref, self.w.schema
        self.assertEqual(self.w.store.verify_projection(ref), ())
        self.w.sql(f"ALTER TABLE {s}.cee_event DISABLE TRIGGER ALL")  # privileged tamper (what verification is FOR)
        self.w.sql(f"UPDATE {s}.cee_event SET event_canonical = '{{\"x\":1}}' WHERE admission_ref = %s", (ref,))
        self.assertEqual(self.w.store.verify_projection(ref), ("PROVENANCE_UNPARSEABLE:1",))


class RecoverySweepsDoNotGrowTheLedger(PgCase):
    def test_identical_unknown_verdicts_are_not_re_appended_but_changes_are(self) -> None:
        provider = W.make_provider(self.w.schema, apply_mode="delay_commit")
        store, ex = self.w.new_executor(provider=provider)
        spec, receipt = self.w.fence()
        ref = receipt.admission_ref
        ex.execute(receipt, spec)
        n0 = self.w.count("cee_event")  # FENCED, ATTEMPT_STARTED, PROVIDER_RESULT
        for _ in range(12):
            self.assertEqual(ex.recover(ref).reason_code, "LATE_COMMIT_STILL_POSSIBLE_SETTLEMENT_PENDING")
        self.assertEqual(self.w.count("cee_event"), n0 + 1)  # one reconciliation, eleven no-ops
        self.assertEqual(store.verify_projection(ref), ())
        provider.deliver(ref)  # the evidence changes: that IS recorded
        self.assertEqual(ex.recover(ref).state, "COMMITTED_READBACK")
        self.assertEqual(self.w.count("cee_event"), n0 + 2)


class DeterministicCrossProcessCompareAndSet(PgCase):
    def test_the_loser_blocks_on_the_row_lock_then_returns_without_calling_the_provider(self) -> None:
        spec, receipt = self.w.fence()
        ref = receipt.admission_ref
        sig, rel = W.scratch("cas_a_sig"), W.scratch("cas_a_rel")
        a = W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": ref, "app": "cee_test_adapter_a",
                     "hook": {"point": "attempt.before_commit", "action": "signal_then_release",
                              "signal_file": sig, "release_file": rel}})
        H.wait_until(lambda: os.path.exists(sig), what="A holds the progress row lock inside begin_attempt")
        self.assertEqual(self.w.store.state_of(ref), "FENCED")  # A's reservation is not committed yet
        b = W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": ref, "app": "cee_test_adapter_b"})
        H.wait_until(lambda: blocked(self.w, "cee_test_adapter_b") >= 1, what="B blocked on A's row lock (the CAS)")
        self.assertEqual(self.w.provider.counts(ref), {"calls": 0, "applied": 0})
        open(rel, "w").close()
        rc_a, out_a, err_a = W.finish(a)
        rc_b, out_b, err_b = W.finish(b)
        self.assertEqual((rc_a, rc_b), (0, 0), (err_a, err_b))
        self.assertEqual(out_a["result"]["state"], "COMMITTED_READBACK")
        self.assertEqual((out_b["result"]["reason_code"], out_b["result"]["replayed"]), ("ATTEMPT_ALREADY_STARTED", True))
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 1})


class NoTransactionOpenProbeHasTeeth(PgCase):
    def test_the_idle_in_transaction_probe_used_by_the_lost_response_test_does_detect_an_open_transaction(self) -> None:
        def idle() -> int:
            return int(self.w.q("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()"
                                " AND application_name LIKE 'cee_test_%%' AND state LIKE 'idle in transaction%%'")[0]["n"])
        self.assertEqual(idle(), 0)
        conn = W.factory("cee_test_adapter")()
        try:
            conn.cursor().execute("BEGIN")
            self.assertEqual(idle(), 1)  # control: an adapter-style connection holding a transaction IS seen
        finally:
            conn.close()
        self.assertEqual(idle(), 0)


class SyntheticProviderFenceIsAtomic(PgCase):
    def test_settlement_and_a_late_effect_never_both_succeed(self) -> None:
        for i in range(8):
            w = W.PgWorld()
            try:
                provider = W.make_provider(w.schema, apply_mode="delay_commit")
                store, ex = w.new_executor(provider=provider)
                spec, receipt = w.fence(default_spec(idempotency_key=f"SYNTH-FENCE-{i}"))
                ex.execute(receipt, spec)
                ref, out, go = receipt.admission_ref, {}, threading.Event()

                def deliver() -> None:
                    go.wait()
                    try:
                        provider.deliver(ref)
                        out["deliver"] = "applied"
                    except Exception as exc:  # noqa: BLE001
                        out["deliver"] = type(exc).__name__

                def settle() -> None:
                    go.wait()
                    try:
                        provider.settle_absent(ref)
                        out["settle"] = "settled"
                    except Exception as exc:  # noqa: BLE001
                        out["settle"] = type(exc).__name__
                ts = [threading.Thread(target=deliver), threading.Thread(target=settle)]
                [t.start() for t in ts]
                go.set()
                [t.join(30) for t in ts]
                applied = w.provider.counts(ref)["applied"]
                # exactly one of the two can have happened; the executor's verdict must agree with what really happened
                self.assertEqual((out["deliver"] == "applied") + (out["settle"] == "settled"), 1, out)
                verdict = ex.recover(ref)
                self.assertEqual(verdict.state, "COMMITTED_READBACK" if applied else "NO_COMMIT", (out, verdict))
            finally:
                w.close()


if __name__ == "__main__":
    unittest.main()
