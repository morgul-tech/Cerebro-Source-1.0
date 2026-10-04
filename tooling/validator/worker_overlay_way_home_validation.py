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
from worker_context_auth import BoundWorkerPolicyResolver, WorkerContextAuthError  # noqa: E402

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
        self.intent_generation = GENERATION
        self.intent_session = context().session_ref()
        self.grantor = "HUMAN"
        self.mandate_current = True
        self.mandate_revision = 3
        self.mandate_generation = GENERATION
        self.mandate_scopes = ["WORKER_ROLE_ATTACH", "WORKER_TASK_ISSUE"]
        self.allowed_task_scopes = ["TASK_EXECUTE"]
        self.claim_count = 0
        self.claim_refs: list[str] = []
        self.claim_revision = 7
        self.task_current = True
        self.task_mandate_revision = 3
        self.task_generation = GENERATION
        self.task_scope = ["TASK_EXECUTE"]
        self.task_method = method

    def human(self, **kwargs):
        return {"schema": "cerebro.worker-human-intent/v1", "intent": self.intent,
                "current": self.intent_current, "revoked": not self.intent_current,
                "principal_ref": kwargs["principal_ref"], "session_ref": self.intent_session,
                "generation_ref": self.intent_generation, "source_revision": HEAD,
                "method_fingerprint": self.method, "intent_ref": "HUMAN-BOOT-WORKER-001",
                "readback_ref": "HUMAN-READBACK-001", "actor_ref": ACTOR,
                "actor_generation_ref": ACTOR_GENERATION, "overlay_ref": "WORKER-OVERLAY-001"}

    def mandate(self, **kwargs):
        return {"schema": "cerebro.worker-pm-parent-mandate/v1",
                "current": self.mandate_current, "revoked": not self.mandate_current,
                "grantor_role": self.grantor, "grantee_role": "PROJECT_MANAGER",
                "generation_ref": self.mandate_generation, "actor_ref": ACTOR,
                "source_revision": HEAD, "scopes": self.mandate_scopes,
                "allowed_task_scopes": self.allowed_task_scopes,
                "mandate_ref": "HUMAN-PM-MANDATE-001",
                "owner_revision": self.mandate_revision, "readback_ref": "MANDATE-READBACK-001"}

    def zero(self, **kwargs):
        with self.lock:
            return {"schema": "cerebro.worker-claim-current/v1",
                    "generation_ref": GENERATION, "actor_ref": ACTOR,
                    "current": True, "readback_verified": True,
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
    def __init__(self, call):
        self.read_current = call


class ClaimReader:
    def __init__(self, owners):
        self.read_zero_claim = owners.zero
        self.hold_zero_claim = owners.hold
        self.read_current_task = owners.task


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
