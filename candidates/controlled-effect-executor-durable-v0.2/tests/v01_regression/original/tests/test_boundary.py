"""T12 (no public bypass), T13 (immutable basis), provenance reconstruction, state-machine closure, source guards."""
import ast
import dataclasses
import inspect
import pathlib
import typing
import unittest

import _world
from _world import ART_NEW, TARGET, World, default_spec
import controlled_effect_executor as pkg
from controlled_effect_executor import (COMMITTED_READBACK, FENCED, IN_FLIGHT, NO_COMMIT, STATES, TERMINAL, TRANSITIONS,
                                        UNKNOWN_EFFECT, ExecutionGrant, LedgerBasisError, ProviderBypassRefused,
                                        verify_provenance)

SRC = pathlib.Path(_world.SRC) / "controlled_effect_executor"
CANDIDATE = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN_MUTATION_NAMES = {"apply", "mutate", "commit", "call_provider", "perform", "write", "send", "push", "deploy",
                            "run_effect", "execute_provider"}


class NoPublicBypass(unittest.TestCase):
    """T12. This is an API/candidate boundary inside one Python process, NOT credential isolation."""

    def test_T12_the_public_api_exposes_exactly_one_path_to_a_provider_mutation(self):
        offenders = []
        for name in pkg.__all__:
            obj = getattr(pkg, name)
            if inspect.isclass(obj) and not typing.is_protocol(obj):
                for member, value in inspect.getmembers(obj, callable):
                    if not member.startswith("_") and member in FORBIDDEN_MUTATION_NAMES:
                        offenders.append(f"{name}.{member}")
            elif callable(obj) and not inspect.isclass(obj) and name in FORBIDDEN_MUTATION_NAMES:
                offenders.append(name)
        self.assertEqual(offenders, [])
        self.assertTrue(callable(pkg.ControlledEffectExecutor.execute))
        self.assertFalse(any(n for n in pkg.__all__ if n in FORBIDDEN_MUTATION_NAMES))
        for fixture in ("SyntheticProvider", "SyntheticOwner", "FakeClock"):
            self.assertNotIn(fixture, pkg.__all__)  # fixtures are not part of the public API

    def test_T12_no_other_public_operation_ever_reaches_the_provider(self):
        w = World()
        spec, receipt = w.fence()
        calls = []
        real = w.provider.adapter.apply
        w.executor._provider = type("P", (), {"apply": staticmethod(lambda g: calls.append(g) or real(g))})()
        ex, st = w.executor, w.store
        for _ in range(2):
            ex.status(receipt.admission_ref)
            ex.reconcile(receipt.admission_ref)
            ex.declare_in_flight_lost(receipt.admission_ref, owner_reason_ref="SYNTH-OWNER-DECISION-1")
            w.admit(spec)
            st.get_admission(receipt.admission_ref)
            st.state_of(receipt.admission_ref)
            st.provenance(receipt.admission_ref)
            st.denials()
        self.assertEqual((calls, w.provider.apply_call_count), ([], 0))
        ex.execute(receipt, spec)  # and the one sanctioned path does reach it, exactly once
        self.assertEqual(len(calls), 1)

    def test_T12_a_provider_adapter_refuses_anything_but_an_executor_minted_grant(self):
        w = World()
        adapter = w.provider.adapter
        forged_fields = dict(admission_ref="ADM-X", attempt_ref="ATT-X", batch_digest="0" * 64, correlation_ref="ADM-X",
                             target_identity=TARGET, target_precondition_version="3", artifact_version=ART_NEW,
                             operations=default_spec().operations)
        for bad in (None, object(), "grant", ExecutionGrant(**forged_fields),
                    ExecutionGrant(**forged_fields, _token=object())):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ProviderBypassRefused):
                    adapter.apply(bad)
        with self.assertRaises(ProviderBypassRefused):
            ExecutionGrant._mint(object(), **forged_fields)
        self.assertEqual((w.provider.apply_call_count, w.provider.mutation_count), (0, 0))

    def test_T12_a_captured_grant_cannot_be_replayed_through_the_adapter(self):
        w, captured = World(), []
        real = w.provider.adapter

        class Spy:
            def apply(self, grant):
                captured.append(grant)
                return real.apply(grant)

        w.executor._provider = Spy()
        w.run()
        self.assertEqual((len(captured), w.provider.mutation_count), (1, 1))
        for _ in range(2):
            with self.assertRaisesRegex(ProviderBypassRefused, "grant-already-used"):
                real.apply(captured[0])
        self.assertEqual((w.provider.apply_call_count, w.provider.mutation_count), (1, 1))

    def test_the_synthetic_provider_is_idempotent_per_correlation_as_a_production_provider_must_be(self):
        w, captured = World(), []
        real = w.provider.adapter

        class Spy:
            def apply(self, grant):
                captured.append(grant)
                return real.apply(grant)

        w.executor._provider = Spy()
        w.run()
        from controlled_effect_executor.grant import _SEAL  # private forge, as the honest limit documents
        forged = ExecutionGrant._mint(_SEAL, **{k: getattr(captured[0], k) for k in (
            "admission_ref", "attempt_ref", "batch_digest", "correlation_ref", "target_identity",
            "target_precondition_version", "artifact_version", "operations")})
        from controlled_effect_executor.synthetic import ProviderRejected
        with self.assertRaises(ProviderRejected):
            real.apply(forged)
        self.assertEqual((w.provider.mutation_count, w.provider.duplicate_correlation_refused), (1, 1))

    def test_T12_the_readback_port_has_no_mutation_method(self):
        w = World()
        public = {n for n in dir(w.provider.readback) if not n.startswith("_")}
        self.assertEqual(public, {"read_target_state"})
        w.provider.readback.read_target_state(TARGET)
        self.assertEqual(w.provider.mutation_count, 0)

    def test_T12_the_documented_limit_is_stated_honestly(self):
        for doc in ("README.md", "ARCHITECTURE.md"):
            text = (CANDIDATE / doc).read_text(encoding="utf-8")
            self.assertIn("API/candidate boundary", text, doc)
            self.assertIn("not production credential isolation proof", text, doc)


class ImmutableBasis(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_T13_receipts_specs_events_and_views_are_frozen(self):
        spec, receipt, _ = self.w.run()
        event = self.w.store.provenance(receipt.admission_ref).events[0]
        for obj, field in ((spec, "artifact_version"), (receipt, "state"), (event, "state_after"),
                           (self.w.executor.status(receipt.admission_ref), "state")):
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(obj, field, "X")

    def test_T13_progress_never_changes_the_basis_identity(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, run = self.w.run()
        before = (receipt.batch_digest, receipt.receipt_fingerprint, self.w.store.get_admission(receipt.admission_ref).spec.digest)
        self.w.executor.reconcile(receipt.admission_ref)
        self.w.owner.revoke("SYNTH-DELEG-1")  # owner moves on; the admitted basis does not
        after = self.w.store.get_admission(receipt.admission_ref)
        self.assertEqual(before, (after.receipt.batch_digest, after.receipt.receipt_fingerprint, after.spec.digest))
        self.assertEqual((after.receipt, after.spec), (receipt, spec))
        self.assertEqual(self.w.store.provenance(receipt.admission_ref).spec_basis, spec.basis())

    def test_T13_the_ledger_refuses_writes_that_contradict_the_admission_basis(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, run = self.w.run()
        st, ref, att, key = self.w.store, receipt.admission_ref, run.attempt_ref, self.w.executor._key
        good = dict(state_after=COMMITTED_READBACK, classification="COMMITTED",
                    reason_code="READBACK_MATCHES_INTENDED_STATE", observation_digest="a" * 64, writer_key=key)
        with self.assertRaisesRegex(LedgerBasisError, "BASIS_MISMATCH"):  # a different batch digest
            st.record_reconciliation(ref, att, "f" * 64, **good)
        with self.assertRaisesRegex(LedgerBasisError, "ATTEMPT_MISMATCH"):  # a different attempt
            st.record_reconciliation(ref, "ATT-OTHER", spec.digest, **good)
        with self.assertRaisesRegex(LedgerBasisError, "ADMISSION_UNKNOWN"):  # an unknown admission
            st.begin_attempt("ADM-UNKNOWN", spec.digest, writer_key=key)
        self.assertEqual(st.state_of(ref), UNKNOWN_EFFECT)
        self.assertEqual(verify_provenance(st.provenance(ref)), ())

    def test_the_ledger_cannot_be_written_without_the_executors_capability(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, run = self.w.run()
        st, ref, att = self.w.store, receipt.admission_ref, run.attempt_ref
        fabricated = dict(state_after=COMMITTED_READBACK, classification="COMMITTED",
                          reason_code="READBACK_MATCHES_INTENDED_STATE", observation_digest="a" * 64)
        for call in (lambda **k: st.begin_attempt(ref, spec.digest, **k),
                     lambda **k: st.record_provider_result(ref, att, spec.digest, acked=True, **k),
                     lambda **k: st.record_reconciliation(ref, att, spec.digest, **fabricated, **k),
                     lambda **k: st.declare_in_flight_lost(ref, spec.digest, "SYNTH-X", **k)):
            for kwargs in ({}, {"writer_key": object()}, {"writer_key": None}):
                with self.assertRaisesRegex(LedgerBasisError, "WRITER_CAPABILITY_REQUIRED"):
                    call(**kwargs)
        with self.assertRaisesRegex(LedgerBasisError, "WRITER_KEY_ALREADY_ISSUED"):
            st.issue_writer_key()
        self.assertEqual(st.state_of(ref), UNKNOWN_EFFECT)  # nothing was resolved without a readback
        self.assertEqual(self.w.provider.readback_call_count, 0)

    def test_a_resolved_state_must_be_derived_from_an_authoritative_observation(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, run = self.w.run()
        st, ref, att, key = self.w.store, receipt.admission_ref, run.attempt_ref, self.w.executor._key
        bad = [
            dict(state_after=COMMITTED_READBACK, classification="INDETERMINATE", reason_code="X", observation_digest=None),
            dict(state_after=COMMITTED_READBACK, classification="COMMITTED", reason_code="FABRICATED",
                 observation_digest="a" * 64),
            dict(state_after=COMMITTED_READBACK, classification="COMMITTED",
                 reason_code="READBACK_MATCHES_INTENDED_STATE", observation_digest=None),
            dict(state_after=NO_COMMIT, classification="NO_COMMIT", reason_code="READBACK_PROVES_NO_COMMIT",
                 observation_digest=None),
            dict(state_after=UNKNOWN_EFFECT, classification="COMMITTED",
                 reason_code="READBACK_MATCHES_INTENDED_STATE", observation_digest="a" * 64),
        ]
        for fields in bad:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(LedgerBasisError, "NOT_DERIVED"):
                    st.record_reconciliation(ref, att, spec.digest, writer_key=key, **fields)
        self.assertEqual(st.state_of(ref), UNKNOWN_EFFECT)
        # NO_COMMIT is also refused when the provider ever acked (an ack contradicted by readback stays ambiguous)
        w2 = World()
        w2.provider.apply_script[:] = ["ack_without_commit"]
        spec3, receipt3, run3 = w2.run()
        self.assertEqual(w2.store.state_of(receipt3.admission_ref), UNKNOWN_EFFECT)
        with self.assertRaisesRegex(LedgerBasisError, "NOT_DERIVED"):
            w2.store.record_reconciliation(receipt3.admission_ref, run3.attempt_ref, spec3.digest,
                                           state_after=NO_COMMIT, classification="NO_COMMIT",
                                           reason_code="READBACK_PROVES_NO_COMMIT", observation_digest="a" * 64,
                                           writer_key=w2.executor._key)

    def test_provenance_verification_flags_a_fabricated_resolution_even_if_written_around_the_api(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, run = self.w.run()
        ref = receipt.admission_ref
        self.w.store._append(ref, "RECONCILIATION", COMMITTED_READBACK, "FABRICATED", run.attempt_ref, spec.digest,
                             {"classification": "COMMITTED", "observation_digest": None})  # private bypass of the API
        self.assertIn("EVENT_SEMANTICS:4", verify_provenance(self.w.store.provenance(ref)))

    def test_T13_returned_provenance_is_a_copy_not_a_handle_on_the_store(self):
        spec, receipt, _ = self.w.run()
        prov = self.w.store.provenance(receipt.admission_ref)
        prov.spec_basis["artifact_version"] = "TAMPERED"
        prov.spec_basis["operations"].clear()
        again = self.w.store.provenance(receipt.admission_ref)
        self.assertEqual(again.spec_basis, spec.basis())
        self.assertEqual(verify_provenance(again), ())
        self.assertNotEqual(verify_provenance(prov), ())  # and the tampered copy is detected

    def test_T13_tampering_with_any_event_or_the_chain_is_detected(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, _ = self.w.run()
        self.w.executor.reconcile(receipt.admission_ref)
        prov = self.w.store.provenance(receipt.admission_ref)
        self.assertEqual(verify_provenance(prov), ())
        forged_event = dataclasses.replace(prov.events[2], state_after=NO_COMMIT)
        self.assertIn("EVENT_FINGERPRINT:3", verify_provenance(dataclasses.replace(
            prov, events=prov.events[:2] + (forged_event,) + prov.events[3:])))
        self.assertTrue(any(p.startswith("EVENT_CHAIN") for p in verify_provenance(dataclasses.replace(
            prov, events=prov.events[:1] + prov.events[2:]))))
        other = dataclasses.replace(prov.admission, actor_generation=99)
        self.assertIn("ADMISSION_RECEIPT_FINGERPRINT", verify_provenance(dataclasses.replace(prov, admission=other)))

    def test_provenance_reconstructs_delegation_batch_admission_attempt_and_readback(self):
        self.w.provider.apply_script[:] = ["commit_lose_response"]
        spec, receipt, run = self.w.run()
        res = self.w.executor.reconcile(receipt.admission_ref)
        prov = self.w.store.provenance(receipt.admission_ref)
        a = prov.admission
        self.assertEqual((a.delegation_ref, a.delegation_revision, a.batch_digest, a.admission_ref),
                         (spec.delegation_ref, spec.delegation_revision, spec.digest, receipt.admission_ref))
        self.assertEqual([e.event_kind for e in prov.events],
                         ["ADMISSION_FENCED", "ATTEMPT_STARTED", "PROVIDER_RESULT", "RECONCILIATION"])
        self.assertEqual([e.state_after for e in prov.events], [FENCED, IN_FLIGHT, UNKNOWN_EFFECT, COMMITTED_READBACK])
        self.assertEqual({e.attempt_ref for e in prov.events[1:]}, {run.attempt_ref})
        self.assertEqual({e.admission_ref for e in prov.events}, {receipt.admission_ref})
        self.assertEqual(prov.events[0].prev_fingerprint, a.receipt_fingerprint)
        self.assertIsNotNone(dict(prov.events[3].detail)["observation_digest"])
        self.assertEqual((res.state, prov.spec_basis["delegation_ref"]), (COMMITTED_READBACK, "SYNTH-DELEG-1"))
        blob = repr([e.to_dict() for e in prov.events])
        self.assertNotIn("synthetic: committed", blob)  # exception MESSAGES are never persisted, only class names


class StateMachine(unittest.TestCase):
    def test_terminal_states_have_no_successor_and_nothing_re_enters_fenced_or_in_flight_from_ambiguity(self):
        for state in TERMINAL:
            self.assertEqual(TRANSITIONS[state], frozenset())
        self.assertEqual(TERMINAL, {COMMITTED_READBACK, NO_COMMIT})
        self.assertFalse(any(FENCED in succ for succ in TRANSITIONS.values()))
        self.assertNotIn(IN_FLIGHT, TRANSITIONS[UNKNOWN_EFFECT])  # automatic retry is not representable
        self.assertEqual(set(TRANSITIONS) | {"DENIED"}, set(STATES))

    def test_the_ledger_enforces_the_table_for_every_state_pair(self):
        reached = {}
        for mode, drive in (("commit_ok", None), ("commit_lose_response", None), ("fail_before_commit", "nc")):
            w = World()
            w.provider.apply_script[:] = [mode]
            spec, receipt, run = w.run()
            if drive == "nc":
                w.executor.reconcile(receipt.admission_ref)
            reached[w.store.state_of(receipt.admission_ref)] = (w, spec, receipt, run)
        w = World()
        reached[FENCED] = (w, *w.fence(), None)
        for current, (w, spec, receipt, run) in reached.items():
            for target in STATES:
                if target == "DENIED":
                    continue
                if target in TRANSITIONS[current]:
                    continue
                with self.subTest(current=current, target=target):
                    with self.assertRaises(LedgerBasisError):
                        w.store._append(receipt.admission_ref, "RECONCILIATION", target, "FORCED", None, spec.digest)


class SourceGuards(unittest.TestCase):
    FORBIDDEN_IMPORTS = {"socket", "ssl", "http", "urllib", "requests", "asyncio", "subprocess", "os", "shutil",
                         "tempfile", "ctypes", "multiprocessing", "sched", "signal", "time", "pathlib", "sqlite3",
                         "psycopg", "psycopg2", "pickle", "importlib", "sys"}
    FORBIDDEN_BUILTIN_CALLS = {"eval", "exec", "open", "__import__", "compile", "input"}
    FORBIDDEN_ATTR_CALLS = {"getenv", "system", "sleep", "Thread", "Timer", "Popen", "popen", "putenv"}

    def files(self):
        return sorted(SRC.rglob("*.py"))

    def test_source_has_no_network_process_file_environment_or_scheduler_surface(self):
        self.assertGreater(len(self.files()), 10)
        for path in self.files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {(node.module or "").split(".")[0]} if node.level == 0 else set()
                else:
                    names = set()
                self.assertFalse(names & self.FORBIDDEN_IMPORTS, f"{path.name}: {names & self.FORBIDDEN_IMPORTS}")
                if isinstance(node, ast.Call):
                    f = node.func
                    if isinstance(f, ast.Name):
                        self.assertNotIn(f.id, self.FORBIDDEN_BUILTIN_CALLS | self.FORBIDDEN_ATTR_CALLS, f"{path.name}: {f.id}")
                    elif isinstance(f, ast.Attribute):
                        self.assertNotIn(f.attr, self.FORBIDDEN_ATTR_CALLS, f"{path.name}: {f.attr}")
                if isinstance(node, ast.While):
                    self.fail(f"{path.name}: loop construct (no daemon/poller allowed)")
                if isinstance(node, ast.Attribute) and node.attr == "environ":
                    self.fail(f"{path.name}: environment access")

    def test_source_names_no_live_provider_and_embeds_no_secret_shaped_literal(self):
        for path in self.files():
            text = path.read_text(encoding="utf-8").lower()
            for word in ("railway", "api_key", "apikey", "password", "bearer ", "private key", "secret_key"):
                self.assertNotIn(word, text, f"{path.name}: {word}")

    def test_threading_is_used_only_for_locks(self):
        for path in self.files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "threading":
                    self.assertEqual(node.attr, "RLock", f"{path.name}: threading.{node.attr}")


if __name__ == "__main__":
    unittest.main()
