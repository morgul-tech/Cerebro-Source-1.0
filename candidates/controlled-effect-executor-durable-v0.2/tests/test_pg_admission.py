"""Falsifiers 1, 2 and 7 on REAL PostgreSQL: the admission fence, contention, immutability, denial matrix.

Every test builds a fresh disposable schema. No mocks: connections are real libpq sessions to a real server.
"""
from __future__ import annotations

import json
import threading
import unittest

import _pgworld as W
from _pgworld import PgCase, default_spec
from controlled_effect_executor_durable import PgError, verify_provenance_durable
import pg_harness as H


def blocked_backends(world: W.PgWorld, app: str) -> int:
    rows = world.q("SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = %s"
                   " AND wait_event_type = 'Lock'", (app,))
    return int(rows[0]["n"])


class _StripShare:
    """Test-only mutant of the owner reader: reads the delegation WITHOUT a row lock (the TOCTOU the fence prevents)."""

    def __init__(self, cur) -> None:
        self._cur = cur

    def execute(self, sql, params=()):
        return self._cur.execute(sql.replace(" FOR SHARE", ""), params)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class UnlockedOwner(W.PgSyntheticOwner):
    def lock_and_read_delegation(self, cur, delegation_ref):
        return super().lock_and_read_delegation(_StripShare(cur), delegation_ref)

    def lock_and_read_approval(self, cur, approval_ref):
        return super().lock_and_read_approval(_StripShare(cur), approval_ref)


class RevokeAdmitOrdering(PgCase):
    def test_1a_revoke_committed_first_gives_no_admission_and_no_provider_call(self) -> None:
        w = self.w
        w.owner.revoke("SYNTH-DELEG-1")
        d = w.admit()
        self.assertEqual((d.state, d.reason_code, d.receipt), ("DENIED", "DELEGATION_REVOKED", None))
        self.assertEqual([w.count(t) for t in ("cee_admission", "cee_event", "cee_progress")], [0, 0, 0])
        self.assertEqual(w.provider.counts(), {"calls": 0, "applied": 0})
        self.assertEqual(w.store.list_unresolved().items, ())

    def test_1b_admission_first_blocks_the_revoke_and_yields_one_exact_receipt(self) -> None:
        w = self.w
        at_barrier, release = threading.Event(), threading.Event()

        def hook() -> None:
            at_barrier.set()
            self.assertTrue(release.wait(30))

        store = W.make_store(w.schema, hooks={"admission.after_owner_read": hook}, owner=w.owner)
        spec, out = default_spec(), {}
        t1 = threading.Thread(target=lambda: out.update(d=store.admit(spec, spec.digest)))
        t1.start()
        self.assertTrue(at_barrier.wait(15))  # admission transaction is open, delegation row locked FOR SHARE
        t2 = threading.Thread(target=lambda: out.update(rev=w.owner.revoke("SYNTH-DELEG-1")))
        t2.start()
        H.wait_until(lambda: blocked_backends(w, "cee_test_owner") >= 1, what="revoke blocked on the row lock")
        self.assertNotIn("rev", out)  # the revoke genuinely cannot complete while the fence transaction is open
        release.set()
        t1.join(30)
        t2.join(30)
        d = out["d"]
        self.assertEqual((d.state, d.reason_code), ("FENCED", "FENCED_ONCE"))
        self.assertTrue(d.receipt.verify())
        self.assertGreater(out["rev"], d.receipt.delegation_currentness)  # revoke is strictly AFTER the fence
        self.assertEqual(w.count("cee_admission"), 1)
        self.assertEqual(w.owner.status_of("SYNTH-DELEG-1"), "REVOKED")
        # the admitted batch is preserved exactly (admission-before-revoke contract), a NEW key is refused
        replay = w.admit()
        self.assertEqual((replay.reason_code, replay.replayed, replay.receipt), ("EXACT_REPLAY", True, d.receipt))
        fresh = default_spec(idempotency_key="SYNTH-IDEM-2")
        self.assertEqual(w.admit(fresh).reason_code, "DELEGATION_REVOKED")

    def test_1c_negative_control_without_the_row_lock_the_race_is_real(self) -> None:
        """Proves the barrier test discriminates: with a plain (unlocked) read the revoke is NOT blocked and an
        admission is fenced even though the revoke committed before the insert. This is the separate-connection TOCTOU."""
        w = self.w
        at_barrier, release = threading.Event(), threading.Event()

        def hook() -> None:
            at_barrier.set()
            self.assertTrue(release.wait(30))

        unlocked = UnlockedOwner(W.factory("cee_test_owner"), w.schema)
        store = W.make_store(w.schema, hooks={"admission.after_owner_read": hook}, owner=unlocked)
        spec, out = default_spec(), {}
        t1 = threading.Thread(target=lambda: out.update(d=store.admit(spec, spec.digest)))
        t1.start()
        self.assertTrue(at_barrier.wait(15))
        rev = w.owner.revoke("SYNTH-DELEG-1")  # completes immediately: nothing holds a lock on the row
        release.set()
        t1.join(30)
        self.assertEqual(out["d"].state, "FENCED")  # the stale admission got through
        self.assertGreater(rev, out["d"].receipt.delegation_currentness)  # revoke happened AFTER the read, BEFORE insert

    def test_1d_revoke_vs_admit_stress_every_outcome_is_strictly_ordered(self) -> None:
        w = self.w
        outcomes = {"admit_first": 0, "revoke_first": 0}
        for i in range(12):
            tag = f"SYNTH-STRESS-{i}"
            world = W.PgWorld()
            try:
                spec = default_spec(idempotency_key=tag)
                out, go = {}, threading.Event()

                def admit() -> None:
                    go.wait()
                    out["d"] = world.store.admit(spec, spec.digest)

                def revoke() -> None:
                    go.wait()
                    out["rev"] = world.owner.revoke("SYNTH-DELEG-1")

                ts = [threading.Thread(target=admit), threading.Thread(target=revoke)]
                [t.start() for t in ts]
                go.set()
                [t.join(30) for t in ts]
                d = out["d"]
                if d.state == "FENCED":
                    outcomes["admit_first"] += 1
                    self.assertGreater(out["rev"], d.receipt.delegation_currentness)
                    self.assertEqual(world.count("cee_admission"), 1)
                else:
                    outcomes["revoke_first"] += 1
                    self.assertEqual(d.reason_code, "DELEGATION_REVOKED")
                    self.assertEqual(world.count("cee_admission"), 0)
            finally:
                world.close()
        self.assertEqual(sum(outcomes.values()), 12)
        _ = w  # the base world is unused here; every iteration owns its schema


class AdmissionContention(PgCase):
    def test_2a_two_processes_identical_request_one_identity(self) -> None:
        for rnd in range(6):
            spec = default_spec(idempotency_key=f"SYNTH-CONTEND-{rnd}")
            gate = W.scratch(f"go_{rnd}")
            procs = [W.spawn({"role": "admit", "schema": self.w.schema, "spec_json": spec.canonical_text(),
                              "wait_file": gate}) for _ in range(2)]
            open(gate, "w").close()
            results = [W.finish(p) for p in procs]
            decisions = [r[1]["decision"] for r in results]
            self.assertTrue(all(r[0] == 0 for r in results), results)
            self.assertEqual({d["receipt"]["admission_ref"] for d in decisions}.__len__(), 1)
            self.assertEqual(sorted(d["reason_code"] for d in decisions), ["EXACT_REPLAY", "FENCED_ONCE"])
            self.assertEqual(self.w.count("cee_admission", "WHERE idempotency_key = %s", (spec.idempotency_key,)), 1)

    def test_2b_same_key_different_basis_is_refused_under_contention_and_alone(self) -> None:
        a = default_spec(idempotency_key="SYNTH-KEY-X")
        b = default_spec(idempotency_key="SYNTH-KEY-X", work_order_ref="SYNTH-WO-OTHER")
        self.assertNotEqual(a.digest, b.digest)
        gate = W.scratch("go_basis")
        procs = [W.spawn({"role": "admit", "schema": self.w.schema, "spec_json": s.canonical_text(), "wait_file": gate})
                 for s in (a, b)]
        open(gate, "w").close()
        decisions = [W.finish(p)[1]["decision"] for p in procs]
        self.assertEqual(sorted(d["reason_code"] for d in decisions), ["FENCED_ONCE", "IDEMPOTENCY_KEY_CONFLICT"])
        winner = next(d for d in decisions if d["state"] == "FENCED")
        stored = self.w.store.get_admission(winner["receipt"]["admission_ref"])
        self.assertEqual(stored.spec.digest, winner["receipt"]["batch_digest"])
        self.assertEqual(self.w.count("cee_admission"), 1)
        # and alone, in one process: the second basis under the same key stays refused
        other = a if winner["receipt"]["batch_digest"] == b.digest else b
        self.assertEqual(self.w.admit(other).reason_code, "IDEMPOTENCY_KEY_CONFLICT")

    def test_2c_in_process_threads_also_converge_on_one_admission(self) -> None:
        spec, out, gate = default_spec(idempotency_key="SYNTH-THREADS"), [], threading.Event()
        stores = [W.make_store(self.w.schema, owner=self.w.owner) for _ in range(8)]

        def run(store) -> None:
            gate.wait()
            out.append(store.admit(spec, spec.digest))

        ts = [threading.Thread(target=run, args=(s,)) for s in stores]
        [t.start() for t in ts]
        gate.set()
        [t.join(60) for t in ts]
        self.assertEqual(sorted(d.reason_code for d in out), ["EXACT_REPLAY"] * 7 + ["FENCED_ONCE"])
        self.assertEqual(len({d.receipt.admission_ref for d in out}), 1)
        self.assertEqual(self.w.count("cee_admission"), 1)


class _Faulty:
    """Fault injection around a REAL connection: the COMMIT statement fails before or after reaching the server."""

    def __init__(self, inner_factory, mode: str) -> None:
        self._f, self.mode = inner_factory, mode
        self.armed = True

    def __call__(self):
        conn = self._f()
        fault = self

        class Cur:
            def __init__(self, inner) -> None:
                self._i = inner

            def execute(self, sql, params=()):
                if sql == "COMMIT" and fault.armed:
                    fault.armed = False
                    if fault.mode == "raise_after_commit":
                        self._i.execute(sql, params)
                    raise PgError("injected-commit-failure", "08006")
                return self._i.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._i, name)

        class Conn:
            def cursor(self_inner):
                return Cur(conn.cursor())

            def close(self_inner):
                conn.close()

        return Conn()


class CommitFailureIsNotSuccess(PgCase):
    def test_commit_never_reached_the_server(self) -> None:
        w = self.w
        store = W.make_store(w.schema, owner=w.owner, connect=_Faulty(W.factory("cee_test_adapter"), "raise_before_commit"))
        d = w.admit(store=store)
        self.assertEqual((d.state, d.reason_code, d.receipt), ("DENIED", "ADMISSION_COMMIT_NOT_CONFIRMED", None))
        self.assertEqual(w.count("cee_admission"), 0)  # the transaction was aborted with the connection
        again = w.admit()  # an honest retry is a first admission
        self.assertEqual(again.reason_code, "FENCED_ONCE")

    def test_commit_reached_the_server_but_the_caller_was_not_told(self) -> None:
        w = self.w
        store = W.make_store(w.schema, owner=w.owner, connect=_Faulty(W.factory("cee_test_adapter"), "raise_after_commit"))
        d = w.admit(store=store)
        self.assertEqual((d.state, d.reason_code, d.receipt), ("DENIED", "ADMISSION_COMMIT_NOT_CONFIRMED", None))
        self.assertEqual(w.count("cee_admission"), 1)  # durable, yet NOT reported as success: nothing may execute from it
        found = w.store.list_unresolved()
        self.assertEqual([i.state for i in found.items], ["FENCED"])  # recoverable without the lost pointer
        replay = w.admit()
        self.assertEqual((replay.reason_code, replay.replayed), ("EXACT_REPLAY", True))
        self.assertEqual(replay.receipt, found.items[0].receipt)
        self.assertEqual(w.count("cee_admission"), 1)


class DenialMatrix(PgCase):
    """Falsifier 7: every stale / wrong / expired input stays denied on real PostgreSQL, with nothing written."""

    CASES = (
        ("DELEGATION_NOT_FOUND", dict(delegation_ref="SYNTH-DELEG-X"), None),
        ("DELEGATION_REVOKED", {}, lambda w: w.owner.revoke("SYNTH-DELEG-1")),
        ("DELEGATION_EXPIRED", {}, lambda w: w.owner.expire("SYNTH-DELEG-1")),
        ("DELEGATION_EXPIRED", dict(delegation_expiry=W.T0), None),
        ("STALE_DELEGATION_REVISION", {}, lambda w: w.owner.amend("SYNTH-DELEG-1", actor_generation=4)),
        ("DELEGATION_REVISION_MISMATCH", dict(delegation_revision=2), None),
        ("ACTOR_MISMATCH", dict(actor_ref="SYNTH-ACTOR-2"), None),
        ("ACTOR_GENERATION_MISMATCH", dict(actor_generation=5), None),
        ("DELEGATION_EXPIRY_MISMATCH", dict(delegation_expiry=W.T0 + 10), None),
        ("OPERATION_OUT_OF_SCOPE", dict(operations=(W.Operation.of("SYNTH-OP-OTHER"),)), None),
        ("TARGET_OUT_OF_SCOPE", dict(target_identity="SYNTH-TARGET-B"), None),
        ("VERSION_OUT_OF_SCOPE", dict(artifact_version="SYNTH-ARTIFACT-V9"), None),
        ("APPROVAL_NOT_FOUND", dict(human_approval_ref="SYNTH-APPROVAL-X"), None),
        ("APPROVAL_NOT_ACTIVE", {}, lambda w: w.owner.withdraw_approval("SYNTH-APPROVAL-1")),
        ("NON_SYNTHETIC_REF_REFUSED", dict(actor_ref="ACTOR-NOT-SYNTH"), None),
    )

    def test_each_denial_creates_no_row_and_consumes_no_key(self) -> None:
        for reason, over, mutate in self.CASES:
            with self.subTest(reason=reason, over=sorted(over)):
                w = W.PgWorld()
                try:
                    if mutate is not None:
                        mutate(w)
                    spec = default_spec(**over)
                    d = w.admit(spec)
                    self.assertEqual((d.state, d.reason_code, d.receipt), ("DENIED", reason, None))
                    self.assertEqual([w.count(t) for t in ("cee_admission", "cee_event", "cee_progress")], [0, 0, 0])
                    self.assertEqual(w.provider.counts(), {"calls": 0, "applied": 0})
                finally:
                    w.close()

    def test_binding_mismatch_and_forged_digest(self) -> None:
        w = self.w
        w.sql(f"UPDATE {w.schema}.cee_synth_owner_approval SET operations_digest = %s", ("0" * 64,))
        self.assertEqual(w.admit().reason_code, "APPROVAL_BINDING_MISMATCH")
        self.assertEqual(w.admit(digest="f" * 64).reason_code, "DIGEST_MISMATCH")
        self.assertEqual(w.count("cee_admission"), 0)

    def test_a_denied_request_does_not_burn_the_idempotency_key(self) -> None:
        w = self.w
        bad = default_spec(target_identity="SYNTH-TARGET-B")
        self.assertEqual(w.admit(bad).reason_code, "TARGET_OUT_OF_SCOPE")
        self.assertEqual(w.admit(default_spec()).reason_code, "FENCED_ONCE")  # same key, valid basis


class StoredBasisIsImmutable(PgCase):
    """Falsifier 7 (immutability): the database itself refuses rewrites; a privileged bypass is DETECTED."""

    def _executed(self):
        spec, receipt = self.w.fence()
        result = self.w.executor.execute(receipt, spec)
        self.assertEqual(result.state, "COMMITTED_READBACK")
        return spec, receipt

    def _refused(self, sql: str, params=()) -> str:
        with self.assertRaises(PgError) as cm:
            self.w.sql(sql, params)
        return cm.exception.sqlstate

    def test_updates_deletes_truncates_are_refused_by_the_database(self) -> None:
        self._executed()
        s = self.w.schema
        for sql in (f"UPDATE {s}.cee_admission SET work_order_ref = 'SYNTH-TAMPERED'",
                    f"DELETE FROM {s}.cee_admission", f"TRUNCATE {s}.cee_admission CASCADE",
                    f"UPDATE {s}.cee_event SET event_kind = 'x'",
                    f"DELETE FROM {s}.cee_event", f"TRUNCATE {s}.cee_event",
                    f"DELETE FROM {s}.cee_progress", f"TRUNCATE {s}.cee_progress CASCADE"):
            with self.subTest(sql=sql):
                self.assertTrue(self._refused(sql).startswith("23"), sql)
        self.assertEqual((self.w.count("cee_admission"), self.w.count("cee_event")), (1, 4))

    def test_projection_cannot_move_without_a_ledger_event(self) -> None:
        self._executed()
        s = self.w.schema
        for sql in (f"UPDATE {s}.cee_progress SET state = 'NO_COMMIT', last_seq = last_seq + 1",
                    f"UPDATE {s}.cee_progress SET last_seq = last_seq + 2",
                    f"UPDATE {s}.cee_progress SET attempt_ref = 'ATT-OTHER'",
                    f"UPDATE {s}.cee_progress SET ack_recorded = false",
                    f"UPDATE {s}.cee_progress SET scope_ref = 'SYNTH-OTHER-SCOPE'"):
            with self.subTest(sql=sql):
                self.assertTrue(self._refused(sql).startswith("23"), sql)

    def _forged_event(self, ref: str, seq: int, kind: str, state: str, prev_fp: str, *, detail=None, attempt=None,
                      canonical_override=None) -> tuple:
        """SQL + params for a raw INSERT whose canonical JSON is WELL-FORMED and matches its columns, so that only the
        rule under test (chain / transition / settlement) can be what refuses it."""
        fp = "c" * 63 + str(seq)
        canon = canonical_override if canonical_override is not None else json.dumps(
            {"admission_ref": ref, "attempt_ref": attempt, "batch_digest": "d" * 64, "detail": detail or {},
             "event_fingerprint": fp, "event_kind": kind, "event_ref": f"EVT-F{seq}", "prev_fingerprint": prev_fp,
             "reason_code": "FORGED", "schema": "x", "seq": seq, "state_after": state}, sort_keys=True)
        sql = (f"INSERT INTO {self.w.schema}.cee_event (admission_ref, seq, event_ref, event_kind, state_after,"
               f" attempt_ref, event_canonical, event_fingerprint, prev_fingerprint) VALUES (%s, %s, %s, %s, %s, %s, %s,"
               f" %s, %s)")
        return sql, (ref, seq, f"EVT-F{seq}", kind, state, attempt, canon, fp, prev_fp)

    def test_ledger_insert_must_chain_and_follow_legal_transitions(self) -> None:
        spec, receipt = self.w.fence()
        s = self.w.schema
        ref = receipt.admission_ref
        head = self.w.q(f"SELECT event_fingerprint FROM {s}.cee_event WHERE admission_ref = %s AND seq = 1", (ref,))[0]
        good_prev = head["event_fingerprint"]
        # control: the SAME well-formed insert with a correct chain and a legal edge IS accepted (so the refusals below
        # are caused by the rule under test, not by malformed input)
        ok_sql, ok_params = self._forged_event(ref, 2, "ATTEMPT_STARTED", "IN_FLIGHT", good_prev, attempt="ATT-F")
        self.w.sql(ok_sql, ok_params)
        self.assertEqual(self.w.count("cee_event"), 2)

    def test_each_ledger_rule_refuses_on_its_own(self) -> None:
        spec, receipt = self.w.fence()
        s, ref = self.w.schema, receipt.admission_ref
        good_prev = self.w.q(f"SELECT event_fingerprint FROM {s}.cee_event WHERE admission_ref = %s AND seq = 1",
                             (ref,))[0]["event_fingerprint"]
        cases = {
            "illegal edge FENCED->COMMITTED_READBACK": self._forged_event(ref, 2, "RECONCILIATION", "COMMITTED_READBACK", good_prev),
            "broken hash chain": self._forged_event(ref, 2, "ATTEMPT_STARTED", "IN_FLIGHT", "0" * 64, attempt="ATT-F"),
            "sequence gap": self._forged_event(ref, 3, "ATTEMPT_STARTED", "IN_FLIGHT", good_prev, attempt="ATT-F"),
            "unknown event kind": self._forged_event(ref, 2, "MADE_UP", "IN_FLIGHT", good_prev),
        }
        for name, (sql, params) in cases.items():
            with self.subTest(name):
                self.assertTrue(self._refused(sql, params).startswith("23"), name)
        self.assertEqual(self.w.count("cee_event"), 1)

    def test_columns_must_equal_the_canonical_event_and_no_commit_needs_settlement_evidence(self) -> None:
        # drive a REAL admission to UNKNOWN_EFFECT, then try to forge its resolution straight in SQL
        provider = W.make_provider(self.w.schema, apply_mode="fail_before_commit")
        store, ex = self.w.new_executor(provider=provider)
        spec, receipt = self.w.fence()
        ref, s = receipt.admission_ref, self.w.schema
        self.assertEqual(ex.execute(receipt, spec).state, "UNKNOWN_EFFECT")
        head = self.w.q(f"SELECT seq, event_fingerprint FROM {s}.cee_event WHERE admission_ref = %s ORDER BY seq DESC"
                        f" LIMIT 1", (ref,))[0]
        seq, prev, att = int(head["seq"]) + 1, head["event_fingerprint"], store.progress_of(ref).attempt_ref

        def refused(**kw) -> str:
            sql, params = self._forged_event(ref, seq, "RECONCILIATION", "NO_COMMIT", prev, attempt=att, **kw)
            return self._refused(sql, params)
        # (a) NO_COMMIT without settlement evidence / with the wrong evidence
        self.assertTrue(refused(detail={"classification": "NO_COMMIT"}).startswith("23"))
        self.assertTrue(refused(detail={"settlement_status": "PENDING", "settlement_digest": "x"}).startswith("23"))
        self.assertTrue(refused(detail={"settlement_status": "SETTLED_ABSENT"}).startswith("23"))  # no digest
        # (b) canonical text that is not the row: junk, wrong state, wrong seq, wrong attempt, not an object
        for bad in ("not json", "[]", "{}"):
            self.assertTrue(refused(canonical_override=bad).startswith("23"), bad)
        for field, value in (("state_after", "UNKNOWN_EFFECT"), ("seq", seq + 1), ("attempt_ref", "ATT-OTHER"),
                             ("event_kind", "PROVIDER_RESULT"), ("event_ref", "EVT-OTHER"), ("admission_ref", "ADM-OTHER"),
                             ("prev_fingerprint", "e" * 64), ("event_fingerprint", "e" * 64)):
            canon = json.loads(self._forged_event(ref, seq, "RECONCILIATION", "NO_COMMIT", prev, attempt=att,
                                                  detail={"settlement_status": "SETTLED_ABSENT",
                                                          "settlement_digest": "x"})[1][6])
            canon[field] = value
            with self.subTest(field=field):
                self.assertTrue(refused(canonical_override=json.dumps(canon)).startswith("23"), field)
        self.assertEqual(self.w.store.state_of(ref), "UNKNOWN_EFFECT")
        self.assertEqual(self.w.count("cee_event", "WHERE seq = %s", (seq,)), 0)

    def test_stored_spec_must_hash_to_the_digest(self) -> None:
        s = self.w.schema
        ins = (f"INSERT INTO {s}.cee_admission (admission_ref, scope_ref, idempotency_key, batch_digest, work_order_ref,"
               f" effect_ref, spec_canonical, receipt_canonical, receipt_fingerprint)"
               f" VALUES ('ADM-X', 'SYNTH-S', 'SYNTH-K', %s, 'wo', 'ef', '{{\"forged\":1}}', '{{}}', 'fp')")
        self.assertEqual(self._refused(ins, ("a" * 64,)), "23514")  # check_violation

    def test_a_privileged_bypass_is_detected_not_silently_accepted(self) -> None:
        spec, receipt = self._executed()
        w, s, ref = self.w, self.w.schema, receipt.admission_ref
        self.assertEqual(verify_provenance_durable(w.store.provenance(ref)), ())
        self.assertEqual(w.store.verify_projection(ref), ())
        w.sql(f"ALTER TABLE {s}.cee_progress DISABLE TRIGGER ALL")
        w.sql(f"UPDATE {s}.cee_progress SET state = 'NO_COMMIT'")  # projection rewritten behind the ledger's back
        self.assertIn("PROJECTION_STATE", w.store.verify_projection(ref))
        w.sql(f"ALTER TABLE {s}.cee_event DISABLE TRIGGER ALL")
        w.sql(f"UPDATE {s}.cee_event SET event_canonical = replace(event_canonical, 'COMMITTED_READBACK', 'NO_COMMIT')"
              f" WHERE seq = 4")  # the ledger itself rewritten
        problems = verify_provenance_durable(w.store.provenance(ref))
        self.assertTrue(any(p.startswith("EVENT_FINGERPRINT:4") for p in problems), problems)

    def test_provenance_of_a_normal_run_is_intact_and_recomputable(self) -> None:
        spec, receipt = self._executed()
        prov = self.w.store.provenance(receipt.admission_ref)
        self.assertEqual(verify_provenance_durable(prov), ())
        self.assertEqual(prov.spec_basis, spec.basis())
        self.assertEqual(prov.admission, receipt)
        self.assertEqual([e.event_kind for e in prov.events],
                         ["ADMISSION_FENCED", "ATTEMPT_STARTED", "PROVIDER_RESULT", "RECONCILIATION"])


if __name__ == "__main__":
    unittest.main()
