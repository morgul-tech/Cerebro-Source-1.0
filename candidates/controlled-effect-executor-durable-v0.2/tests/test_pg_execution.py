"""Falsifiers 2-6 on REAL PostgreSQL with REAL OS processes: attempt contention, restart discovery, kill/recovery,
lost response, delayed (late) commit. Provider-side counters are the oracle for 'at most one mutation'.
"""
from __future__ import annotations

import json
import os
import signal
import unittest

import _pgworld as W
from _pgworld import PgCase, default_spec
from controlled_effect_executor_durable import PgError, StorageError, make_connection_factory, verify_provenance_durable
from controlled_effect_executor_durable.synthetic_pg import ProviderRejected
import pg_harness as H


def run(job: dict) -> dict:
    rc, out, err = W.finish(W.spawn(job))
    assert rc == 0 and out is not None, (rc, err)
    return out


def events(world: W.PgWorld, ref: str) -> list:
    return [(r["event_kind"], r["state_after"]) for r in world.q(
        f"SELECT event_kind, state_after FROM {world.schema}.cee_event WHERE admission_ref = %s ORDER BY seq", (ref,))]


class _ReservationFault:
    """Real connections whose COMMIT fails ONLY for the transaction that writes ATTEMPT_STARTED."""

    def __init__(self, inner_factory, mode: str) -> None:
        self._f, self.mode, self.fired = inner_factory, mode, False

    def __call__(self):
        conn, fault = self._f(), self

        class Cur:
            def __init__(self, inner) -> None:
                self._i, self.hit = inner, False

            def execute(self, sql, params=()):
                if "ATTEMPT_STARTED" in repr(params):
                    self.hit = True
                if sql == "COMMIT" and self.hit and not fault.fired:
                    fault.fired = True
                    if fault.mode == "raise_after_commit":
                        self._i.execute(sql, params)
                    raise PgError("injected-commit-failure", "08006")
                return self._i.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._i, name)

        class Conn:
            def __init__(self) -> None:
                self._cur = Cur(conn.cursor())

            def cursor(self_inner):
                return self_inner._cur

            def close(self_inner):
                conn.close()

        return Conn()


class AttemptContention(PgCase):
    def test_2d_two_processes_execute_the_same_admission_one_attempt_one_mutation(self) -> None:
        for rnd in range(6):
            spec = default_spec(idempotency_key=f"SYNTH-ATT-{rnd}")
            spec, receipt = self.w.fence(spec)
            gate = W.scratch(f"att_{rnd}")
            procs = [W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": receipt.admission_ref,
                              "wait_file": gate}) for _ in range(2)]
            open(gate, "w").close()
            results = [W.finish(p)[1]["result"] for p in procs]
            winners = [r for r in results if not r["replayed"]]
            losers = [r for r in results if r["replayed"]]
            self.assertEqual((len(winners), len(losers)), (1, 1), results)
            self.assertEqual(winners[0]["state"], "COMMITTED_READBACK")
            # the loser only reports what it observed (the winner may still be inside the provider call) and calls nothing
            self.assertIn(losers[0]["reason_code"], ("ATTEMPT_ALREADY_STARTED", "NOT_FENCED_NO_NEW_ATTEMPT"))
            self.assertIn(losers[0]["state"], ("IN_FLIGHT", "COMMITTED_READBACK"))
            c = self.w.provider.counts(receipt.admission_ref)
            self.assertEqual(c, {"calls": 1, "applied": 1}, c)
            self.assertEqual(events(self.w, receipt.admission_ref).count(("ATTEMPT_STARTED", "IN_FLIGHT")), 1)
            # reset the synthetic target so every round starts from the same precondition (SYNTH data only)
            self.w.sql(f"UPDATE {self.w.schema}_prov.cee_synth_provider_target SET version = 3, artifact_version = %s"
                       f" WHERE target_identity = %s", (W.ART_OLD, W.TARGET))

    def test_a_second_process_cannot_call_while_the_first_is_inside_the_provider(self) -> None:
        spec, receipt = self.w.fence()
        sig, rel = W.scratch("in_provider"), W.scratch("release_provider")
        first = W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": receipt.admission_ref,
                         "hook": {"point": "provider.before_effect", "action": "signal_then_release",
                                  "signal_file": sig, "release_file": rel}})
        H.wait_until(lambda: os.path.exists(sig), what="first process inside the provider call")
        # the reservation is DURABLE and visible from another connection while the provider is still being called
        prog = self.w.store.progress_of(receipt.admission_ref)
        self.assertEqual((prog.state, prog.last_seq), ("IN_FLIGHT", 2))
        second = run({"role": "execute", "schema": self.w.schema, "admission_ref": receipt.admission_ref})["result"]
        self.assertEqual((second["reason_code"], second["replayed"]), ("NOT_FENCED_NO_NEW_ATTEMPT", True))
        self.assertEqual(self.w.provider.counts(receipt.admission_ref), {"calls": 1, "applied": 0})
        # a recovery racing the live process answers conservatively and never calls the provider
        rec = run({"role": "recover", "schema": self.w.schema, "admission_ref": receipt.admission_ref})["result"]
        self.assertEqual((rec["state"], rec["reason_code"]), ("UNKNOWN_EFFECT", "RECOVERY_ATTEMPT_RESULT_NOT_RECORDED"))
        open(rel, "w").close()
        rc, out, err = W.finish(first)
        self.assertEqual(rc, 0, err)
        # the live process's ack is refused by the ledger (state already UNKNOWN) and reconciled instead: one mutation
        self.assertEqual(out["result"]["state"], "COMMITTED_READBACK")
        self.assertEqual(self.w.provider.counts(receipt.admission_ref), {"calls": 1, "applied": 1})
        self.assertEqual(events(self.w, receipt.admission_ref),
                         [("ADMISSION_FENCED", "FENCED"), ("ATTEMPT_STARTED", "IN_FLIGHT"),
                          ("RECONCILIATION", "UNKNOWN_EFFECT"), ("RECONCILIATION", "COMMITTED_READBACK")])
        self.assertEqual(verify_provenance_durable(self.w.store.provenance(receipt.admission_ref)), ())


class RestartBeforeExecution(PgCase):
    def test_3_new_process_discovers_the_full_basis_without_the_original_pointer(self) -> None:
        spec = default_spec()
        admitted = run({"role": "admit", "schema": self.w.schema, "spec_json": spec.canonical_text()})["decision"]
        self.assertEqual(admitted["state"], "FENCED")
        # (the admitting process has exited; nothing but the database remembers this task)
        listed = run({"role": "list", "schema": self.w.schema})
        self.assertEqual(len(listed["page"]), 1)
        item = listed["page"][0]
        self.assertEqual((item["state"], item["attempt_ref"], item["batch_digest"], item["work_order_ref"],
                          item["integrity_ok"]), ("FENCED", None, spec.digest, "SYNTH-WO-1", True))
        self.assertEqual(item["spec_json"], spec.canonical_text())  # the exact full basis
        self.assertEqual(item["admission_ref"], admitted["receipt"]["admission_ref"])
        done = run({"role": "execute", "schema": self.w.schema, "admission_ref": item["admission_ref"]})["result"]
        self.assertEqual((done["state"], done["attempts_started"]), ("COMMITTED_READBACK", 1))
        self.assertEqual(self.w.count("cee_admission"), 1)  # no second admission
        self.assertEqual(self.w.provider.counts(), {"calls": 1, "applied": 1})
        again = self.w.admit(spec)
        self.assertEqual((again.reason_code, again.state, again.replayed), ("EXACT_REPLAY", "COMMITTED_READBACK", True))
        self.assertEqual(run({"role": "list", "schema": self.w.schema})["page"], [])  # resolved: no longer unresolved


class KillAndRecover(PgCase):
    def _admit(self):
        spec, receipt = self.w.fence()
        return spec, receipt.admission_ref

    def test_4a_external_kill_9_inside_the_provider_call(self) -> None:
        spec, ref = self._admit()
        sig = W.scratch("kill_a")
        proc = W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": ref,
                        "hook": {"point": "provider.before_effect", "action": "signal_then_block", "signal_file": sig}})
        H.wait_until(lambda: os.path.exists(sig), what="worker inside the provider call")
        proc.send_signal(signal.SIGKILL)
        rc, out, _ = W.finish(proc)
        self.assertEqual((rc, out), (-signal.SIGKILL, None))
        before = self.w.store.progress_of(ref)
        self.assertEqual((before.state, before.ack_recorded), ("IN_FLIGHT", False))
        attempt = before.attempt_ref
        self.assertIsNotNone(attempt)
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 0})
        # restart: a fresh process finds it, with the original attempt, and does NOT replay or invent NO_COMMIT
        page = run({"role": "list", "schema": self.w.schema})["page"]
        self.assertEqual([(i["state"], i["attempt_ref"]) for i in page], [("IN_FLIGHT", attempt)])
        rec = run({"role": "recover", "schema": self.w.schema, "admission_ref": ref})["result"]
        self.assertEqual((rec["state"], rec["reason_code"], rec["attempt_ref"]),
                         ("UNKNOWN_EFFECT", "RECOVERY_ATTEMPT_RESULT_NOT_RECORDED", attempt))
        self.assertTrue(rec["owner_action_required"])
        self.assertFalse(rec["automatic_retry_allowed"])
        # the uncertainty is explained in the immutable ledger: empty history was NOT accepted as final
        ev = json.loads(self.w.q(f"SELECT event_canonical FROM {self.w.schema}.cee_event WHERE admission_ref = %s"
                                 f" AND seq = 3", (ref,))[0]["event_canonical"])
        self.assertEqual((ev["event_kind"], ev["state_after"], ev["detail"]["settlement_status"],
                          ev["detail"]["classification"]), ("RECONCILIATION", "UNKNOWN_EFFECT", "UNKNOWN", "INDETERMINATE"))
        # nothing replays automatically; calling execute again or recovering again starts nothing
        again = run({"role": "execute", "schema": self.w.schema, "admission_ref": ref})["result"]
        self.assertEqual(again["reason_code"], "NOT_FENCED_NO_NEW_ATTEMPT")
        run({"role": "recover", "schema": self.w.schema, "admission_ref": ref})
        self.assertEqual(self.w.store.progress_of(ref).state, "UNKNOWN_EFFECT")
        self.assertEqual(self.w.store.progress_of(ref).attempt_ref, attempt)
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 0})
        self.assertEqual(self.w.count("cee_admission"), 1)
        self.assertEqual(verify_provenance_durable(self.w.store.provenance(ref)), ())

    def test_4b_crash_after_the_provider_committed_resolves_by_readback_without_a_second_mutation(self) -> None:
        spec, ref = self._admit()
        proc = W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": ref,
                        "hook": {"point": "provider.after_commit", "action": "kill_self"}})
        self.assertEqual(W.finish(proc)[0], -signal.SIGKILL)
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 1})
        self.assertEqual(self.w.store.state_of(ref), "IN_FLIGHT")  # the result was never recorded
        rec = run({"role": "recover", "schema": self.w.schema, "admission_ref": ref})["result"]
        self.assertEqual((rec["state"], rec["reason_code"]), ("COMMITTED_READBACK", "READBACK_MATCHES_INTENDED_STATE"))
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 1})
        self.assertEqual(events(self.w, ref), [("ADMISSION_FENCED", "FENCED"), ("ATTEMPT_STARTED", "IN_FLIGHT"),
                                               ("RECONCILIATION", "COMMITTED_READBACK")])

    def test_4c_crash_before_the_reservation_commit_leaves_the_admission_startable(self) -> None:
        spec, ref = self._admit()
        proc = W.spawn({"role": "execute", "schema": self.w.schema, "admission_ref": ref,
                        "hook": {"point": "attempt.before_commit", "action": "kill_self"}})
        self.assertEqual(W.finish(proc)[0], -signal.SIGKILL)
        self.assertEqual((self.w.store.state_of(ref), self.w.provider.counts(ref)["calls"]), ("FENCED", 0))
        done = run({"role": "execute", "schema": self.w.schema, "admission_ref": ref})["result"]
        self.assertEqual(done["state"], "COMMITTED_READBACK")
        self.assertEqual(self.w.provider.counts(ref), {"calls": 1, "applied": 1})

    def test_4d_crash_inside_the_admission_transaction_leaves_nothing_durable(self) -> None:
        proc = W.spawn({"role": "admit", "schema": self.w.schema, "spec_json": default_spec().canonical_text(),
                        "hook": {"point": "admission.before_commit", "action": "kill_self"}})
        self.assertEqual(W.finish(proc)[0], -signal.SIGKILL)
        self.assertEqual([self.w.count(t) for t in ("cee_admission", "cee_event", "cee_progress")], [0, 0, 0])
        self.assertEqual(self.w.admit().reason_code, "FENCED_ONCE")

    def test_reservation_commit_failure_never_reaches_the_provider(self) -> None:
        for mode, durable in (("raise_before_commit", False), ("raise_after_commit", True)):
            with self.subTest(mode=mode):
                w = W.PgWorld()
                try:
                    spec, receipt = w.fence()
                    faulty = _ReservationFault(W.factory("cee_test_adapter"), mode)
                    store, ex = w.new_executor(W.make_store(w.schema, owner=w.owner, connect=faulty))
                    res = ex.execute(receipt, spec)
                    self.assertTrue(faulty.fired)
                    self.assertEqual(res.reason_code, "ATTEMPT_RESERVATION_NOT_CONFIRMED")
                    self.assertEqual(w.provider.counts(), {"calls": 0, "applied": 0})  # the provider was never called
                    self.assertEqual(w.store.state_of(receipt.admission_ref), "IN_FLIGHT" if durable else "FENCED")
                finally:
                    w.close()

    def test_storage_failure_before_any_provider_call_raises_and_calls_nothing(self) -> None:
        spec, receipt = self.w.fence()
        bad = make_connection_factory({**self.w.params, "port": "1", "connect_timeout": "2"})
        store, ex = self.w.new_executor(W.make_store(self.w.schema, owner=self.w.owner, connect=bad))
        with self.assertRaises(StorageError):
            ex.execute(receipt, spec)
        self.assertEqual(self.w.provider.counts(), {"calls": 0, "applied": 0})
        self.assertEqual(self.w.store.state_of(receipt.admission_ref), "FENCED")
        down = W.make_store(self.w.schema, owner=self.w.owner, connect=bad)
        self.assertEqual(down.admit(default_spec(idempotency_key="SYNTH-DOWN"), default_spec(
            idempotency_key="SYNTH-DOWN").digest).reason_code, "ADMISSION_STORAGE_ERROR")

    def test_storage_failure_after_the_provider_call_is_reported_not_raised_and_recovers(self) -> None:
        spec, receipt = self.w.fence()
        holder = {}
        bad = make_connection_factory({**self.w.params, "port": "1", "connect_timeout": "2"})

        def break_storage() -> None:
            holder["store"]._connect = bad  # the database vanishes right after the provider committed

        provider = W.make_provider(self.w.schema, hooks={"provider.after_commit": break_storage})
        store, ex = self.w.new_executor(provider=provider)
        holder["store"] = store
        good = store._connect
        res = ex.execute(receipt, spec)
        self.assertEqual((res.state, res.reason_code), ("IN_FLIGHT", "PROVIDER_RESULT_NOT_RECORDED_STORAGE_ERROR"))
        store._connect = good
        self.assertEqual(self.w.provider.counts(), {"calls": 1, "applied": 1})
        rec = ex.recover(receipt.admission_ref)
        self.assertEqual((rec.state, rec.reason_code), ("COMMITTED_READBACK", "READBACK_MATCHES_INTENDED_STATE"))
        self.assertEqual(self.w.provider.counts(), {"calls": 1, "applied": 1})


class LostResponseAndDistinctTransactions(PgCase):
    def test_5_committed_then_response_lost_resolves_the_original_attempt_by_readback(self) -> None:
        w = self.w
        seen = {}

        def idle_in_tx() -> int:
            rows = w.q("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database()"
                       " AND application_name LIKE 'cee_test_%%' AND state LIKE 'idle in transaction%%'")
            return int(rows[0]["n"])

        spec, receipt = w.fence()
        ref = receipt.admission_ref

        def before_effect() -> None:  # still inside the provider call
            seen["idle_before"] = idle_in_tx()
            seen["reserved_visible"] = w.store.progress_of(ref).state
            seen["applied_before"] = w.provider.counts(ref)["applied"]

        def after_commit() -> None:
            seen["idle_after"] = idle_in_tx()
            seen["applied_after"] = w.provider.counts(ref)["applied"]
            seen["events_after"] = len(events(w, ref))

        provider = W.make_provider(w.schema, apply_mode="commit_lose_response",
                                   hooks={"provider.before_effect": before_effect, "provider.after_commit": after_commit})
        store, ex = w.new_executor(provider=provider)
        res = ex.execute(receipt, spec)
        # no database transaction of the adapter was open during the (potentially slow) provider call
        self.assertEqual((seen["idle_before"], seen["idle_after"]), (0, 0))
        self.assertEqual((seen["reserved_visible"], seen["applied_before"]), ("IN_FLIGHT", 0))
        self.assertEqual((seen["applied_after"], seen["events_after"]), (1, 2))  # effect durable BEFORE any ledger result
        self.assertEqual((res.state, res.reason_code), ("UNKNOWN_EFFECT", "PROVIDER_RESPONSE_NOT_RECEIVED"))
        attempt = res.attempt_ref
        rec = ex.recover(ref)
        self.assertEqual((rec.state, rec.reason_code, rec.attempt_ref),
                         ("COMMITTED_READBACK", "READBACK_MATCHES_INTENDED_STATE", attempt))
        self.assertEqual(w.provider.counts(ref), {"calls": 1, "applied": 1})  # no second mutation, no second call
        # the provider's effect transaction is a different transaction from every ledger transaction
        applied_xid = w.q(f"SELECT xmin::text AS x FROM {w.schema}_prov.cee_synth_provider_applied")[0]["x"]
        ledger_xids = {r["x"] for r in w.q(f"SELECT xmin::text AS x FROM {w.schema}.cee_event")}
        self.assertNotIn(applied_xid, ledger_xids)
        self.assertEqual(verify_provenance_durable(store.provenance(ref)), ())


class DelayedCommitAndFinality(PgCase):
    def _delayed(self, **kw):
        provider = W.make_provider(self.w.schema, apply_mode=kw.pop("apply_mode", "delay_commit"), **kw)
        store, ex = self.w.new_executor(provider=provider)
        spec, receipt = self.w.fence()
        res = ex.execute(receipt, spec)
        self.assertEqual((res.state, res.reason_code), ("UNKNOWN_EFFECT", "PROVIDER_RESPONSE_NOT_RECEIVED"))
        return provider, store, ex, receipt.admission_ref

    def _recon(self, ref: str) -> tuple:
        """(reason_code, detail) of the LAST ledger RECONCILIATION event: the durable explanation of the last verdict."""
        row = self.w.q(f"SELECT event_canonical FROM {self.w.schema}.cee_event WHERE admission_ref = %s"
                       f" AND event_kind = 'RECONCILIATION' ORDER BY seq DESC LIMIT 1", (ref,))[0]
        ev = json.loads(row["event_canonical"])
        return ev["reason_code"], ev["detail"]

    def test_6a_late_commit_after_an_empty_read_stays_unknown_then_reconciles_to_the_real_result(self) -> None:
        provider, store, ex, ref = self._delayed()
        obs = provider.readback.read_target_state(W.TARGET)  # the earlier EMPTY read
        self.assertEqual((obs.applied_correlation_refs, obs.history_complete, obs.authoritative), ((), True, True))
        for _ in range(3):  # complete, authoritative, empty: still NOT final while the old operation is pending
            rec = ex.recover(ref)
            self.assertEqual(rec.state, "UNKNOWN_EFFECT")
        reason, detail = self._recon(ref)
        self.assertEqual((reason, detail["settlement_status"], detail["classification"]),
                         ("LATE_COMMIT_STILL_POSSIBLE_SETTLEMENT_PENDING", "PENDING", "INDETERMINATE"))
        self.assertEqual(provider.counts(ref), {"calls": 1, "applied": 0})
        provider.deliver(ref)  # the delayed provider commit finally lands
        rec = ex.recover(ref)
        self.assertEqual((rec.state, rec.reason_code), ("COMMITTED_READBACK", "READBACK_MATCHES_INTENDED_STATE"))
        self.assertEqual(provider.counts(ref), {"calls": 1, "applied": 1})
        self.assertEqual(verify_provenance_durable(store.provenance(ref)), ())

    def test_6b_only_explicit_finality_yields_no_commit_and_it_excludes_the_late_commit(self) -> None:
        provider, store, ex, ref = self._delayed()
        provider.settle_absent(ref)  # the provider's own explicit fence
        rec = ex.recover(ref)
        self.assertEqual((rec.state, rec.reason_code), ("NO_COMMIT", "READBACK_PROVES_NO_COMMIT"))
        self.assertTrue(rec.owner_action_required)
        self.assertFalse(rec.automatic_retry_allowed)
        reason, detail = self._recon(ref)
        self.assertEqual((reason, detail["settlement_status"]), ("READBACK_PROVES_NO_COMMIT", "SETTLED_ABSENT"))
        self.assertTrue(detail["settlement_digest"])
        with self.assertRaises(ProviderRejected):  # the late commit is now impossible, as the evidence claimed
            provider.deliver(ref)
        self.assertEqual(provider.counts(ref), {"calls": 1, "applied": 0})
        self.assertEqual(verify_provenance_durable(store.provenance(ref)), ())
        self.assertEqual(ex.recover(ref).reason_code, "RECOVERY_NOT_APPLICABLE_TERMINAL")
        self.assertEqual(self.w.provider.counts(ref)["applied"], 0)

    def test_6c_empty_complete_history_without_settlement_is_never_a_final_no_commit(self) -> None:
        provider, store, ex, ref = self._delayed(apply_mode="fail_before_commit")  # V0.1 would have said NO_COMMIT
        rec = ex.recover(ref)
        self.assertEqual(rec.state, "UNKNOWN_EFFECT")
        self.assertEqual(rec.reason_code, "NO_COMMIT_REFUSED_NO_SETTLEMENT_EVIDENCE")
        reason, detail = self._recon(ref)
        self.assertEqual((reason, detail["settlement_status"]), ("NO_COMMIT_REFUSED_NO_SETTLEMENT_EVIDENCE", "UNKNOWN"))

    def test_6d_a_readback_without_the_settlement_port_cannot_finalize(self) -> None:
        provider = W.make_provider(self.w.schema, apply_mode="delay_commit")
        store, ex = self.w.new_executor(provider=provider, readback=provider.readback_v01_only)
        spec, receipt = self.w.fence()
        ex.execute(receipt, spec)
        provider.settle_absent(receipt.admission_ref)
        self.assertEqual(ex.recover(receipt.admission_ref).state, "UNKNOWN_EFFECT")

    def test_6e_definite_provider_refusal_with_durable_settlement_yields_no_commit(self) -> None:
        provider, store, ex, ref = self._delayed(apply_mode="reject_definite")
        rec = ex.recover(ref)
        self.assertEqual((rec.state, rec.reason_code), ("NO_COMMIT", "READBACK_PROVES_NO_COMMIT"))
        self.assertEqual(provider.counts(ref), {"calls": 1, "applied": 0})

    def test_6f_unreliable_readbacks_keep_unknown(self) -> None:
        for mode in ("non_authoritative", "history_incomplete", "unavailable"):
            with self.subTest(mode=mode):
                w = W.PgWorld()
                try:
                    provider = W.make_provider(w.schema, apply_mode="reject_definite", readback_mode=mode)
                    store, ex = w.new_executor(provider=provider)
                    spec, receipt = w.fence()
                    ex.execute(receipt, spec)
                    self.assertEqual(ex.recover(receipt.admission_ref).state, "UNKNOWN_EFFECT")
                    self.assertEqual(provider.counts(receipt.admission_ref)["applied"], 0)
                finally:
                    w.close()

    def test_ack_without_commit_is_contradicted_and_even_settlement_cannot_turn_it_into_no_commit(self) -> None:
        provider = W.make_provider(self.w.schema, apply_mode="ack_without_commit")
        store, ex = self.w.new_executor(provider=provider)
        spec, receipt = self.w.fence()
        res = ex.execute(receipt, spec)
        self.assertEqual((res.state, res.reason_code), ("UNKNOWN_EFFECT", "ACK_CONTRADICTED_BY_READBACK"))
        provider.settle_absent(receipt.admission_ref)
        again = ex.recover(receipt.admission_ref)  # an ack was recorded: a contradicting provider is never 'no commit'
        self.assertEqual((again.state, again.reason_code), ("UNKNOWN_EFFECT", "ACK_CONTRADICTED_BY_READBACK"))
        self.assertEqual(provider.counts(receipt.admission_ref), {"calls": 1, "applied": 0})


class BoundedReadOnlyListing(PgCase):
    def _many(self, n: int) -> list:
        refs = []
        for i in range(n):
            spec = default_spec(idempotency_key=f"SYNTH-LIST-{i}", work_order_ref="SYNTH-WO-ODD" if i % 2 else "SYNTH-WO-EVEN")
            refs.append(self.w.fence(spec)[1].admission_ref)
        return refs

    def test_paging_bounds_cursor_and_filters(self) -> None:
        refs = self._many(7)
        seen, cursor, pages = [], None, 0
        while True:
            page = self.w.store.list_unresolved(after_cursor=cursor, limit=3)
            pages += 1
            self.assertLessEqual(len(page.items), 3)
            seen += [i.admission_ref for i in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual((seen, pages), (refs, 3))  # all, in admission order, none twice
        self.assertEqual(len(self.w.store.list_unresolved(work_order_ref="SYNTH-WO-ODD", limit=100).items), 3)
        for bad in (0, 501, True, "3", None):
            with self.assertRaises(ValueError):
                self.w.store.list_unresolved(limit=bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.w.store.list_unresolved(after_cursor="abc")

    def test_scope_is_respected_and_resolved_items_disappear(self) -> None:
        refs = self._many(2)
        other = W.make_store(self.w.schema, scope="SYNTH-EXECUTOR-SCOPE-B", owner=self.w.owner)
        self.assertEqual(other.list_unresolved().items, ())
        spec_b = default_spec(idempotency_key="SYNTH-IN-B")
        self.assertEqual(self.w.admit(spec_b, store=other).state, "FENCED")
        self.assertEqual([i.idempotency_key for i in other.list_unresolved().items], ["SYNTH-IN-B"])
        self.assertEqual(len(self.w.store.list_unresolved().items), 2)  # B's admission is not in A's scope
        rec = self.w.store.get_admission(refs[0])
        self.assertEqual(self.w.executor.execute(rec.receipt, rec.spec).state, "COMMITTED_READBACK")
        self.assertEqual([i.admission_ref for i in self.w.store.list_unresolved().items], [refs[1]])

    def test_listing_is_read_only_and_works_with_a_role_that_cannot_write(self) -> None:
        refs = self._many(3)
        w = self.w
        before = [w.count(t) for t in ("cee_admission", "cee_event", "cee_progress")]
        password = "rdr" + H.secrets.token_hex(12)
        role = "cee_reader_" + w.schema[-8:]
        w.sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        try:
            w.sql(f"GRANT USAGE ON SCHEMA {w.schema} TO {role}")
            w.sql(f"GRANT SELECT ON ALL TABLES IN SCHEMA {w.schema} TO {role}")
            reader = make_connection_factory({**w.params, "user": role, "password": password,
                                              "application_name": "cee_test_reader"})
            store = W.make_store(w.schema, owner=w.owner, connect=reader)
            page = store.list_unresolved()
            self.assertEqual([i.admission_ref for i in page.items], refs)
            self.assertTrue(all(i.integrity_ok for i in page.items))
            conn = reader()
            try:
                with self.assertRaises(PgError) as cm:
                    conn.cursor().execute(f"INSERT INTO {w.schema}.cee_progress (admission_ref) VALUES ('x')")
                self.assertEqual(cm.exception.sqlstate, "42501")  # insufficient_privilege: the role really is read-only
            finally:
                conn.close()
        finally:
            w.sql(f"REVOKE ALL ON ALL TABLES IN SCHEMA {w.schema} FROM {role}")
            w.sql(f"REVOKE USAGE ON SCHEMA {w.schema} FROM {role}")
            w.sql(f"DROP ROLE {role}")
        self.assertEqual([w.count(t) for t in ("cee_admission", "cee_event", "cee_progress")], before)
        self.assertEqual(w.provider.counts(), {"calls": 0, "applied": 0})

    def test_a_listed_receipt_grants_nothing_new(self) -> None:
        self._many(1)
        item = self.w.store.list_unresolved().items[0]
        forged = item.receipt.__class__(**{**item.receipt.to_dict(), "actor_ref": "SYNTH-ACTOR-EVIL"})
        res = self.w.executor.execute(forged, item.spec)
        self.assertEqual(res.reason_code, "RECEIPT_FORGED_OR_MALFORMED")
        self.assertEqual(self.w.provider.counts(), {"calls": 0, "applied": 0})


if __name__ == "__main__":
    unittest.main()
