#!/usr/bin/env python3
"""C1006 default-off WORKER acceptance and negative fixture; no provider/live calls."""
from __future__ import annotations

import copy
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "tooling/context"), str(ROOT / "tooling/validator")]

from assistant_overlay_way_home_validation import (  # noqa: E402
    HEAD, context, continuity, provider, source,
)
from boot_birth_source import verify_role_method_projection  # noqa: E402
from control_context_state_port import InMemoryControlContextStatePort, StateBindingError  # noqa: E402
from control_context_tools import (  # noqa: E402
    ControlContextMcpTools, ControlContextToolAuthorizationError,
    ControlContextToolError, HmacControlResolutionAttestor, McpToolCallContext,
)
from worker_context_auth import (  # noqa: E402
    BoundWorkerPolicyResolver, SourceStandingGrantWorkerPolicyResolver,
    WorkerContextAuthError,
)

GENERATION = "CEREBRO-BOOT-WORKER-20261003-C1006"
ACTOR = "WORKER_X1_C1006"
ACTOR_GENERATION = "WORKER_X1_C1006_G1"
TOOL = "resume_ready_unbound_worker_overlay"


class Owners:
    def __init__(self, method: str):
        self.lock = threading.RLock()
        self.method = method
        self.intent = "BOOT_WORKER"
        self.intent_current = True
        self.intent_revision = 5
        self.intent_generation = GENERATION
        self.intent_session = context().session_ref()
        self.grantor = "HUMAN"
        self.mandate_current = True
        self.mandate_revision = 3
        self.mandate_generation = GENERATION
        self.mandate_source = HEAD
        self.mandate_scopes = ["WORKER_ROLE_ATTACH", "WORKER_TASK_ISSUE"]
        self.allowed_task_scopes = ["TASK_EXECUTE"]
        self.claim_count = 0
        self.claim_refs: list[str] = []
        self.claim_revision = 7
        self.claim_current = True
        self.claim_readback_verified = True
        self.claim_generation = GENERATION
        self.task_current = True
        self.task_mandate_revision = 3
        self.task_generation = GENERATION
        self.task_scope = ["TASK_EXECUTE"]
        self.task_method = method
        self.human_reads = 0
        self.task_reads = 0

    def human(self, **kwargs):
        self.human_reads += 1
        return {"schema": "cerebro.worker-human-intent/v1", "intent": self.intent,
                "current": self.intent_current, "revoked": not self.intent_current,
                "principal_ref": kwargs["principal_ref"], "session_ref": self.intent_session,
                "generation_ref": self.intent_generation, "source_revision": HEAD,
                "method_fingerprint": self.method, "intent_ref": "HUMAN-BOOT-WORKER-001",
                "owner_revision": self.intent_revision,
                "readback_ref": "HUMAN-READBACK-001", "actor_ref": ACTOR,
                "actor_generation_ref": ACTOR_GENERATION, "overlay_ref": "WORKER-OVERLAY-001"}

    def mandate(self, **kwargs):
        return {"schema": "cerebro.worker-pm-parent-mandate/v1",
                "current": self.mandate_current, "revoked": not self.mandate_current,
                "grantor_role": self.grantor, "grantee_role": "PROJECT_MANAGER",
                "generation_ref": self.mandate_generation, "actor_ref": ACTOR,
                "source_revision": self.mandate_source, "scopes": self.mandate_scopes,
                "allowed_task_scopes": self.allowed_task_scopes,
                "mandate_ref": "HUMAN-PM-MANDATE-001",
                "owner_revision": self.mandate_revision, "readback_ref": "MANDATE-READBACK-001"}

    def hold_human(self, *, generation_ref, principal_ref, session_ref,
                   intent_ref, expected_revision):
        owner = self
        return OwnerFence(owner, "worker-human-intent-fence-stale", lambda: (
            generation_ref == owner.intent_generation and
            principal_ref == context().identity.principal_ref and
            session_ref == owner.intent_session and
            intent_ref == "HUMAN-BOOT-WORKER-001" and
            expected_revision == owner.intent_revision and owner.intent_current
        ))

    def hold_mandate(self, *, generation_ref, actor_ref, mandate_ref,
                     expected_revision):
        owner = self
        return OwnerFence(owner, "worker-parent-mandate-fence-stale", lambda: (
            generation_ref == owner.mandate_generation and actor_ref == ACTOR and
            mandate_ref == "HUMAN-PM-MANDATE-001" and
            expected_revision == owner.mandate_revision and owner.mandate_current
        ))

    def zero(self, **kwargs):
        with self.lock:
            return {"schema": "cerebro.worker-claim-current/v1",
                    "generation_ref": self.claim_generation, "actor_ref": ACTOR,
                    "current": self.claim_current,
                    "readback_verified": self.claim_readback_verified,
                    "owner_revision": self.claim_revision,
                    "frontier_ref": "PM-FRONTIER-001", "claim_count": self.claim_count,
                    "active_claim_refs": copy.deepcopy(self.claim_refs)}

    def hold(self, *, generation_ref, actor_ref, expected_revision):
        owner = self

        class Fence:
            def __enter__(self):
                owner.lock.acquire()
                return self

            def __exit__(self, *_):
                owner.lock.release()
                return False

            def assert_current(self):
                if (generation_ref != GENERATION or actor_ref != ACTOR or
                        owner.claim_revision != expected_revision or
                        owner.claim_count != 0 or owner.claim_refs):
                    raise WorkerContextAuthError("worker-zero-claim-fence-stale")

        return Fence()

    def task(self, **kwargs):
        self.task_reads += 1
        return {"schema": "cerebro.worker-pm-task-decision/v1", "current": self.task_current,
                "revoked": not self.task_current, "issued_by_role": "PROJECT_MANAGER",
                "generation_ref": self.task_generation, "actor_ref": ACTOR,
                "task_ref": kwargs["task_ref"],
                "source_revision": HEAD, "mandate_ref": "HUMAN-PM-MANDATE-001",
                "method_fingerprint": self.task_method,
                "mandate_revision": self.task_mandate_revision,
                "scope": self.task_scope, "claim_ref": "PM-CLAIM-001",
                "packet_ref": "PM-PACKET-001", "queue_ref": "PM-QUEUE-001",
                "decision_ref": "PM-TASK-ISSUE-001", "owner_revision": 8,
                "frontier_ref": "PM-FRONTIER-002", "readback_verified": True,
                "readback_ref": "PM-TASK-READBACK-001"}

    def issue_claim(self):
        with self.lock:
            self.claim_count = 1
            self.claim_refs = ["PM-CLAIM-001"]
            self.claim_revision += 1


class Reader:
    def __init__(self, call, hold=None):
        self.read_current = call
        if hold is not None:
            self.hold_current = hold


class OwnerFence:
    def __init__(self, owner, error, current):
        self.owner, self.error, self.current = owner, error, current

    def __enter__(self):
        self.owner.lock.acquire()
        return self

    def __exit__(self, *_):
        self.owner.lock.release()
        return False

    def assert_current(self):
        if not self.current():
            raise WorkerContextAuthError(self.error)


class ClaimReader:
    def __init__(self, owners):
        self.read_zero_claim = owners.zero
        self.hold_zero_claim = owners.hold
        self.read_current_task = owners.task


class StandingGrantReader:
    """Synthetic provider fixture; its receipts do not establish formal role."""

    def __init__(self, owners, method):
        self.owners = owners
        self.method = method
        self.current = True
        self.revoked = False
        self.readback_verified = True
        self.generation = GENERATION
        self.source = HEAD
        self.grantor = "HUMAN"
        self.grantee = "PROJECT_MANAGER"
        self.scopes = ["WORKER_ROLE_ATTACH", "WORKER_TASK_ISSUE"]
        self.revision = 11
        self.reads = 0
        self.missing_readback = False

    def read_current(self, *, generation_ref, source_revision, method_fingerprint):
        self.reads += 1
        if self.missing_readback:
            return None
        return {"schema": "cerebro.worker-standing-grant-readback/v1",
                "current": self.current, "revoked": self.revoked,
                "readback_verified": self.readback_verified,
                "generation_ref": self.generation, "source_revision": self.source,
                "method_fingerprint": self.method, "grantor_role": self.grantor,
                "grantee_role": self.grantee, "scopes": copy.deepcopy(self.scopes),
                "grant_ref": "HUMAN-STANDING-WORKER-GRANT-001",
                "owner_revision": self.revision, "readback_ref": "GRANT-READBACK-001"}

    def hold_current(self, *, generation_ref, source_revision, method_fingerprint,
                     grant_ref, expected_revision):
        owner = self

        class Fence:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def assert_current(self):
                if (not owner.current or owner.revoked or not owner.readback_verified or
                        owner.generation != generation_ref or owner.source != source_revision or
                        owner.method != method_fingerprint or owner.revision != expected_revision or
                        grant_ref != "HUMAN-STANDING-WORKER-GRANT-001"):
                    raise WorkerContextAuthError("worker-standing-grant-fence-stale")

        return Fence()


class WorkerOverlayProof(unittest.TestCase):
    def setUp(self):
        self.state = InMemoryControlContextStatePort()
        self.attestor = HmacControlResolutionAttestor(
            key_id="C1006-TEST", secret=b"local-worker-test-secret-at-least-32-bytes",
        )
        bootstrap = ControlContextMcpTools(
            self.state, self.attestor, boot_birth_source_verifier=source,
            boot_birth_attestation_issuer=self.attestor,
        )
        self.birth = bootstrap.dispatch(
            "create_ready_unbound_boot_generation",
            {"boot_attempt_id": GENERATION, "source_revision": HEAD}, context(),
        )["structuredContent"]["pre_role_generation"]
        self.owners = Owners(self.birth["civilization_method_attestation"]["method_fingerprint"])
        self.policy = BoundWorkerPolicyResolver(
            Reader(self.owners.human), Reader(self.owners.mandate),
            ClaimReader(self.owners), enabled=True,
        )
        self.tools = self.host()
        self.args = {"generation_ref": GENERATION,
                     "expected_revision": self.birth["revision"], "source_revision": HEAD}

    def host(self, *, policy=None, issuer=None, continuity_verifier=continuity):
        return ControlContextMcpTools(
            self.state, self.attestor, boot_birth_source_verifier=source,
            boot_birth_attestation_issuer=self.attestor,
            worker_overlay_control_resolver=self.policy if policy is None else policy,
            worker_overlay_attestation_issuer=self.attestor if issuer is None else issuer,
            worker_overlay_source_continuity_verifier=continuity_verifier,
            role_method_source_verifier=lambda head, role: verify_role_method_projection(
                head, role, fetch=provider(head),
            ),
        )

    def resume(self, *, tools=None, args=None):
        return (tools or self.tools).dispatch(TOOL, self.args if args is None else args,
                                              context())["structuredContent"]

    def fenced_owner_readers(self):
        return (
            Reader(self.owners.human, self.owners.hold_human),
            Reader(self.owners.mandate, self.owners.hold_mandate),
        )

    def test_g0_g2_exact_attach_readback_and_zero_claim(self):
        self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")
        self.assertIsNone(self.birth["authority_envelope"]["role"])
        result = self.resume()
        pre, actor = result["pre_role_generation"], result["actor_generation_shadow"]
        self.assertEqual((pre["generation_ref"], pre["lifecycle"]), (GENERATION, "ROLE_ATTACHED"))
        self.assertEqual(pre["ready_unbound_receipt"], self.birth["ready_unbound_receipt"])
        self.assertEqual((actor["actor_ref"], actor["generation_ref"], actor["role"],
                          actor["lifecycle"], actor["authority"]),
                         (ACTOR, ACTOR_GENERATION, "WORKER", "READY", "SHADOW_ONLY"))
        self.assertEqual(pre["role_overlay"]["method_projection"]["authority"], "NONE")
        self.assertEqual(result["zero_claim_owner_revision"], 7)
        self.assertEqual(self.owners.zero()["claim_count"], 0)
        self.assertNotIn("control_resolution_attestation", str(result))
        self.assertEqual(self.tools.dispatch("read_boot_generation_state",
            {"generation_ref": GENERATION}, context("project_state:read"))
            ["structuredContent"]["pre_role_generation"], pre)

    def test_client_attestation_and_missing_owner_ports_denied(self):
        with self.assertRaises(ControlContextToolError):
            self.resume(args={**self.args, "control_resolution_attestation": {}})
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.resume(tools=self.host(policy=object()))
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.resume(tools=self.host(issuer=object()))
        disabled = BoundWorkerPolicyResolver(None, None, None)
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.resume(tools=self.host(policy=disabled))

    def test_standing_human_grant_composition_is_default_off_and_parity_only(self):
        grant_reader = StandingGrantReader(self.owners, self.owners.method)
        disabled = SourceStandingGrantWorkerPolicyResolver(
            grant_reader, Reader(self.owners.human), Reader(self.owners.mandate),
            ClaimReader(self.owners),
        )
        with self.assertRaisesRegex(WorkerContextAuthError, "worker-policy-default-off"):
            disabled.resolve(pre_role_generation=self.birth,
                             verified_source={"source_revision": HEAD,
                                              "method_fingerprint": self.owners.method,
                                              "method_unchanged": True,
                                              "ancestry_verified": True},
                             identity=context().identity, session_ref=context().session_ref())
        self.assertEqual((grant_reader.reads, self.owners.human_reads), (0, 0))

        intent_reader, mandate_reader = self.fenced_owner_readers()
        enabled = SourceStandingGrantWorkerPolicyResolver(
            grant_reader, intent_reader, mandate_reader,
            ClaimReader(self.owners), enabled=True,
        )
        decision = enabled.resolve(
            pre_role_generation=self.birth,
            verified_source={"source_revision": HEAD,
                             "method_fingerprint": self.owners.method,
                             "method_unchanged": True, "ancestry_verified": True},
            identity=context().identity, session_ref=context().session_ref(),
        )
        self.assertEqual(decision["result"], "ALLOW")
        self.assertEqual(decision["overlay_payload"]["role"], "WORKER")
        with enabled.hold_zero_claim(decision) as fence:
            fence.assert_current()
        self.assertEqual(grant_reader.reads, 1)
        self.assertEqual(self.owners.human_reads, 1)
        self.assertEqual(self.owners.zero()["claim_count"], 0)
        self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")

        decision = enabled.resolve(
            pre_role_generation=self.birth,
            verified_source={"source_revision": HEAD,
                             "method_fingerprint": self.owners.method,
                             "method_unchanged": True, "ancestry_verified": True},
            identity=context().identity, session_ref=context().session_ref(),
        )
        grant_reader.revoked = True
        with self.assertRaisesRegex(WorkerContextAuthError,
                                    "worker-standing-grant-fence-stale"):
            with enabled.hold_zero_claim(decision):
                self.fail("revoked grant crossed the pre-attach fence")
        self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")

    def test_revoked_intent_after_allow_fails_held_attach_fence(self):
        intent_reader, mandate_reader = self.fenced_owner_readers()
        policy = SourceStandingGrantWorkerPolicyResolver(
            StandingGrantReader(self.owners, self.owners.method), intent_reader,
            mandate_reader, ClaimReader(self.owners), enabled=True,
        )
        decision = policy.resolve(
            pre_role_generation=self.birth,
            verified_source={"source_revision": HEAD,
                             "method_fingerprint": self.owners.method,
                             "method_unchanged": True, "ancestry_verified": True},
            identity=context().identity, session_ref=context().session_ref(),
        )
        self.owners.intent_current = False
        self.owners.intent_revision += 1
        with self.assertRaisesRegex(WorkerContextAuthError,
                                    "worker-human-intent-fence-stale"):
            with policy.hold_zero_claim(decision):
                self.fail("revoked Human intent crossed the pre-attach fence")
        self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")

    def test_revoked_mandate_after_allow_fails_held_attach_fence(self):
        intent_reader, mandate_reader = self.fenced_owner_readers()
        policy = SourceStandingGrantWorkerPolicyResolver(
            StandingGrantReader(self.owners, self.owners.method), intent_reader,
            mandate_reader, ClaimReader(self.owners), enabled=True,
        )
        decision = policy.resolve(
            pre_role_generation=self.birth,
            verified_source={"source_revision": HEAD,
                             "method_fingerprint": self.owners.method,
                             "method_unchanged": True, "ancestry_verified": True},
            identity=context().identity, session_ref=context().session_ref(),
        )
        self.owners.mandate_current = False
        self.owners.mandate_revision += 1
        with self.assertRaisesRegex(WorkerContextAuthError,
                                    "worker-parent-mandate-fence-stale"):
            with policy.hold_zero_claim(decision):
                self.fail("revoked PM mandate crossed the pre-attach fence")
        self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")

    def test_standing_grant_rejections_happen_before_intent_or_claim_reads(self):
        for field, value in (("current", False), ("revoked", True),
                             ("readback_verified", False), ("generation", "OTHER"),
                             ("source", "0" * 40), ("method", "0" * 64),
                             ("grantor", "PROJECT_MANAGER"),
                             ("grantee", "WORKER"), ("scopes", ["WORKER_ROLE_ATTACH"])):
            grant_reader = StandingGrantReader(self.owners, self.owners.method)
            setattr(grant_reader, field, value)
            policy = SourceStandingGrantWorkerPolicyResolver(
                grant_reader, Reader(self.owners.human), Reader(self.owners.mandate),
                ClaimReader(self.owners), enabled=True,
            )
            with self.subTest(field=field), self.assertRaises(WorkerContextAuthError):
                policy.resolve(
                    pre_role_generation=self.birth,
                    verified_source={"source_revision": HEAD,
                                     "method_fingerprint": self.owners.method,
                                     "method_unchanged": True, "ancestry_verified": True},
                    identity=context().identity, session_ref=context().session_ref(),
                )
            self.assertEqual(self.owners.human_reads, 0)
            self.assertEqual(self.owners.task_reads, 0)
            self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")

    def test_standing_composition_owner_negative_guards_fail_before_attach(self):
        cases = (
            ("missing_provider_readback", "grant_missing", True),
            ("missing_human_intent", "intent", None),
            ("revoked_human_intent", "intent_current", False),
            ("wrong_intent_generation", "intent_generation", "OTHER"),
            ("wrong_intent_session", "intent_session", "OTHER"),
            ("stale_parent_source", "mandate_source", "0" * 40),
            ("wrong_parent_generation", "mandate_generation", "OTHER"),
            ("nonzero_claim", "claim_count", 1),
            ("stale_claim_readback", "claim_readback_verified", False),
            ("wrong_claim_generation", "claim_generation", "OTHER"),
        )
        for label, field, value in cases:
            with self.subTest(case=label):
                grant_reader = StandingGrantReader(self.owners, self.owners.method)
                before = None
                if field == "grant_missing":
                    grant_reader.missing_readback = True
                else:
                    before = getattr(self.owners, field)
                    setattr(self.owners, field, value)
                policy = SourceStandingGrantWorkerPolicyResolver(
                    grant_reader, Reader(self.owners.human), Reader(self.owners.mandate),
                    ClaimReader(self.owners), enabled=True,
                )
                try:
                    with self.assertRaises(WorkerContextAuthError):
                        policy.resolve(
                            pre_role_generation=self.birth,
                            verified_source={"source_revision": HEAD,
                                             "method_fingerprint": self.owners.method,
                                             "method_unchanged": True,
                                             "ancestry_verified": True},
                            identity=context().identity,
                            session_ref=context().session_ref(),
                        )
                    self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")
                    self.assertEqual(self.owners.task_reads, 0)
                finally:
                    if before is not None:
                        setattr(self.owners, field, before)

    def test_standing_composition_rejects_stale_source_method_before_owner_reads(self):
        grant_reader = StandingGrantReader(self.owners, self.owners.method)
        policy = SourceStandingGrantWorkerPolicyResolver(
            grant_reader, Reader(self.owners.human), Reader(self.owners.mandate),
            ClaimReader(self.owners), enabled=True,
        )
        for verified_source in (
            {"source_revision": "0" * 40, "method_fingerprint": self.owners.method,
             "method_unchanged": True, "ancestry_verified": True},
            {"source_revision": HEAD, "method_fingerprint": "0" * 64,
             "method_unchanged": False, "ancestry_verified": True},
        ):
            with self.assertRaises(WorkerContextAuthError):
                policy.resolve(pre_role_generation=self.birth,
                               verified_source=verified_source,
                               identity=context().identity,
                               session_ref=context().session_ref())
            self.assertEqual(self.owners.human_reads, 0)
        self.assertEqual(self.birth["lifecycle"], "READY_UNBOUND")

    def test_wrong_human_or_parent_mandate_denied(self):
        for field, value in (("intent", "BOOT_ASSISTANT"), ("intent_current", False),
                             ("intent_generation", "OTHER"), ("intent_session", "OTHER"),
                             ("grantor", "PROJECT_MANAGER"), ("mandate_current", False),
                             ("mandate_generation", "OTHER"), ("mandate_scopes", ["WORKER_ROLE_ATTACH"])):
            before = getattr(self.owners, field)
            setattr(self.owners, field, value)
            with self.subTest(field=field), self.assertRaises(ControlContextToolAuthorizationError):
                self.resume()
            setattr(self.owners, field, before)
        self.assertEqual(self.tools.dispatch("read_boot_generation_state",
            {"generation_ref": GENERATION}, context("project_state:read"))
            ["structuredContent"]["pre_role_generation"]["lifecycle"], "READY_UNBOUND")

    def test_false_zero_and_stale_fence_denied_before_attach(self):
        self.owners.issue_claim()
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.resume()
        self.owners.claim_count, self.owners.claim_refs = 0, []
        parent = self.policy
        class Drift:
            def resolve(self, **kwargs):
                decision = parent.resolve(**kwargs)
                self_outer.owners.claim_revision += 1
                return decision
            def hold_zero_claim(self, decision):
                return parent.hold_zero_claim(decision)
        self_outer = self
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.resume(tools=self.host(policy=Drift()))
        self.assertEqual(self.tools.dispatch("read_boot_generation_state",
            {"generation_ref": GENERATION}, context("project_state:read"))
            ["structuredContent"]["pre_role_generation"]["lifecycle"], "READY_UNBOUND")

    def test_concurrent_claim_issue_waits_for_attach_readback(self):
        at_attach, permit, issuing, issued = (threading.Event() for _ in range(4))
        original = self.tools.attach_role_overlay

        def paused(*args, **kwargs):
            at_attach.set()
            self.assertTrue(permit.wait(5), "attach permit timeout")
            return original(*args, **kwargs)

        def issue():
            issuing.set()
            self.owners.issue_claim()
            issued.set()

        self.tools.attach_role_overlay = paused
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                attaching = pool.submit(self.resume)
                self.assertTrue(at_attach.wait(5))
                issuing_job = pool.submit(issue)
                self.assertTrue(issuing.wait(5))
                self.assertFalse(issued.wait(0.05), "claim bypassed zero fence")
                permit.set()
                result = attaching.result(timeout=5)
                issuing_job.result(timeout=5)
        finally:
            permit.set()
            self.tools.attach_role_overlay = original
        self.assertEqual(result["zero_claim_owner_revision"], 7)
        self.assertEqual((self.owners.claim_revision, self.owners.claim_count), (8, 1))

    def test_generation_revision_method_and_replay_restart_handoff(self):
        with self.assertRaises(StateBindingError):
            self.resume(args={**self.args, "generation_ref": GENERATION + "-OTHER"})
        with self.assertRaises(ControlContextToolError):
            self.resume(args={**self.args, "expected_revision": 1})
        with self.assertRaises(ControlContextToolError):
            self.resume(args={**self.args, "source_revision": "0" * 40})
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.resume(tools=self.host(continuity_verifier=lambda *_: {
                "source_revision": HEAD, "method_fingerprint": "0" * 64,
                "method_unchanged": False, "ancestry_verified": True,
            }))
        self.resume()
        restarted = self.host()
        with self.assertRaises(ControlContextToolError):
            self.resume(tools=restarted)
        handoff_context = McpToolCallContext(
            identity=context("project_state:read").identity,
            request_meta={"openai/session": "HANDOFF-SESSION"},
        )
        task = restarted.dispatch("read_worker_operative_task",
            {"generation_ref": GENERATION, "actor_generation_ref": ACTOR_GENERATION,
             "task_ref": "TASK-001"}, handoff_context)["structuredContent"]["task_decision"]
        self.assertEqual(task["decision_ref"], "PM-TASK-ISSUE-001")
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.host(continuity_verifier=lambda *_: {
                "source_revision": HEAD, "method_fingerprint": "0" * 64,
                "method_unchanged": False, "ancestry_verified": True,
            }).dispatch("read_worker_operative_task",
                {"generation_ref": GENERATION, "actor_generation_ref": ACTOR_GENERATION,
                 "task_ref": "TASK-001"}, handoff_context)
        with self.assertRaises(ControlContextToolError):
            restarted.dispatch(TOOL, self.args, context("project_state:read"))

    def test_g3_operative_pm_task_within_existing_mandate(self):
        task_args = {"generation_ref": GENERATION,
                     "actor_generation_ref": ACTOR_GENERATION, "task_ref": "TASK-001"}
        def read_task():
            return self.tools.dispatch("read_worker_operative_task", task_args,
                                       context("project_state:read"))["structuredContent"]["task_decision"]
        with self.assertRaises(ControlContextToolAuthorizationError):
            read_task()
        self.resume()
        task = read_task()
        self.assertEqual(task["mandate_ref"], "HUMAN-PM-MANDATE-001")
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.tools.dispatch("read_worker_operative_task",
                {**task_args, "actor_generation_ref": "OLD-GENERATION"},
                context("project_state:read"))
        with self.assertRaises(ControlContextToolAuthorizationError):
            self.tools.dispatch("read_worker_operative_task", task_args,
                                context("project_state:transition"))
        self.owners.task_mandate_revision = 2
        with self.assertRaises(ControlContextToolAuthorizationError):
            read_task()
        self.owners.task_mandate_revision = 3
        self.owners.task_current = False
        with self.assertRaises(ControlContextToolAuthorizationError):
            read_task()
        self.owners.task_current = True
        self.owners.mandate_current = False
        with self.assertRaises(ControlContextToolAuthorizationError):
            read_task()
        self.owners.mandate_current = True
        for field, wrong in (("task_generation", "OLD-GENERATION"),
                             ("task_scope", ["OUTSIDE_MANDATE"]),
                             ("task_method", "0" * 64)):
            before = getattr(self.owners, field)
            setattr(self.owners, field, wrong)
            with self.subTest(field=field), self.assertRaises(ControlContextToolAuthorizationError):
                read_task()
            setattr(self.owners, field, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
