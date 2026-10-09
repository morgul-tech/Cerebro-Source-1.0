"""Default-off WORKER policy port; owner readers are injected by the server.

No client argument, shadow absence, service key, or PM task decision can create
Human intent, a parent PM mandate, or an authoritative zero-claim statement.
The owner ports below need production custody before this candidate can run live.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from typing import Any


class WorkerContextAuthError(RuntimeError):
    pass


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise WorkerContextAuthError(reason)


def _ref(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


class BoundWorkerPolicyResolver:
    """Server-owned composition of three independent current owner readers.

    human_intent_reader.read_current and mandate_reader.read_current are
    authenticated owner ports. claim_reader.read_zero_claim and
    claim_reader.hold_zero_claim are generation-scoped PM owner ports. This
    class validates their returned receipts; it cannot authenticate the ports.
    """

    def __init__(self, human_intent_reader: Any, mandate_reader: Any,
                 claim_reader: Any, *, enabled: bool = False):
        self.human_intent_reader = human_intent_reader
        self.mandate_reader = mandate_reader
        self.claim_reader = claim_reader
        self.enabled = enabled

    def _port(self, obj: Any, method: str, **kwargs: Any) -> dict:
        _require(self.enabled, "worker-policy-default-off")
        call = getattr(obj, method, None)
        _require(callable(call), "trusted-worker-owner-port-unbound:" + method)
        try:
            value = call(**kwargs)
        except WorkerContextAuthError:
            raise
        except Exception as exc:
            raise WorkerContextAuthError("worker-owner-read-unavailable:" + method) from exc
        _require(isinstance(value, dict), "worker-owner-receipt-required:" + method)
        return copy.deepcopy(value)

    @staticmethod
    def _mandate(value: dict, *, generation_ref: str, actor_ref: str,
                 source_revision: str) -> None:
        _require(value.get("schema") == "cerebro.worker-pm-parent-mandate/v1",
                 "worker-parent-mandate-schema-mismatch")
        _require(value.get("current") is True and value.get("revoked") is False,
                 "worker-parent-mandate-not-current")
        _require(value.get("grantor_role") == "HUMAN" and
                 value.get("grantee_role") == "PROJECT_MANAGER",
                 "pm-cannot-self-grant-parent-mandate")
        _require(value.get("generation_ref") == generation_ref and
                 value.get("actor_ref") == actor_ref and
                 value.get("source_revision") == source_revision,
                 "worker-parent-mandate-scope-mismatch")
        _require(set(value.get("scopes", [])) == {"WORKER_ROLE_ATTACH", "WORKER_TASK_ISSUE"},
                 "worker-parent-mandate-scope-invalid")
        _require(isinstance(value.get("allowed_task_scopes"), list) and
                 bool(value["allowed_task_scopes"]) and
                 all(_ref(scope) for scope in value["allowed_task_scopes"]),
                 "worker-parent-mandate-task-scope-invalid")
        _require(_ref(value.get("mandate_ref")) and
                 type(value.get("owner_revision")) is int and value["owner_revision"] > 0 and
                 _ref(value.get("readback_ref")), "worker-parent-mandate-owner-readback-required")

    @staticmethod
    def _zero(value: dict, *, generation_ref: str, actor_ref: str) -> int:
        _require(value.get("schema") == "cerebro.worker-claim-current/v1" and
                 value.get("generation_ref") == generation_ref and
                 value.get("actor_ref") == actor_ref,
                 "worker-zero-claim-generation-mismatch")
        _require(value.get("current") is True and value.get("readback_verified") is True and
                 type(value.get("owner_revision")) is int and value["owner_revision"] > 0 and
                 _ref(value.get("frontier_ref")), "worker-zero-claim-owner-readback-required")
        _require(type(value.get("claim_count")) is int and value["claim_count"] == 0 and
                 value.get("active_claim_refs") == [], "worker-zero-claim-false-or-stale")
        return value["owner_revision"]

    def resolve(self, *, pre_role_generation: dict, verified_source: dict,
                identity: Any, session_ref: str) -> dict:
        _require(self.enabled, "worker-policy-default-off")
        generation = pre_role_generation["generation_ref"]
        source = pre_role_generation["source_revision"]
        _require(pre_role_generation["lifecycle"] == "READY_UNBOUND" and
                 verified_source.get("source_revision") == source and
                 verified_source.get("method_fingerprint") ==
                 pre_role_generation["civilization_method_attestation"]["method_fingerprint"] and
                 verified_source.get("method_unchanged") is True and
                 verified_source.get("ancestry_verified") is True,
                 "worker-source-or-method-not-current")
        intent = self._port(self.human_intent_reader, "read_current",
                            generation_ref=generation, principal_ref=identity.principal_ref,
                            session_ref=session_ref)
        _require(intent.get("schema") == "cerebro.worker-human-intent/v1" and
                 intent.get("intent") == "BOOT_WORKER" and
                 intent.get("current") is True and intent.get("revoked") is False and
                 intent.get("principal_ref") == identity.principal_ref and
                 intent.get("session_ref") == session_ref and
                 intent.get("generation_ref") == generation and
                 intent.get("source_revision") == source and
                 intent.get("method_fingerprint") == verified_source["method_fingerprint"] and
                 _ref(intent.get("intent_ref")) and _ref(intent.get("readback_ref")) and
                 type(intent.get("owner_revision")) is int and intent["owner_revision"] > 0 and
                 _ref(intent.get("actor_ref")) and _ref(intent.get("actor_generation_ref")) and
                 _ref(intent.get("overlay_ref")), "trusted-human-worker-intent-required")
        actor_ref = intent["actor_ref"]
        mandate = self._port(self.mandate_reader, "read_current",
                             generation_ref=generation, actor_ref=actor_ref)
        self._mandate(mandate, generation_ref=generation, actor_ref=actor_ref,
                      source_revision=source)
        zero = self._port(self.claim_reader, "read_zero_claim",
                          generation_ref=generation, actor_ref=actor_ref)
        zero_revision = self._zero(zero, generation_ref=generation, actor_ref=actor_ref)
        return {
            "result": "ALLOW", "decision_ref": intent["intent_ref"],
            "pre_role_fingerprint": pre_role_generation["fingerprint"],
            "ready_unbound_receipt_fingerprint":
                pre_role_generation["ready_unbound_receipt"]["receipt_fingerprint"],
            "zero_claim_revision": zero_revision, "mandate_ref": mandate["mandate_ref"],
            "overlay_payload": {
                "generation_ref": generation, "expected_revision": pre_role_generation["revision"],
                "overlay_ref": intent["overlay_ref"], "actor_ref": actor_ref,
                "role": "WORKER", "actor_generation_ref": intent["actor_generation_ref"],
                "source_revision": source,
            },
        }

    def hold_zero_claim(self, decision: dict):
        _require(self.enabled, "worker-policy-default-off")
        payload = decision["overlay_payload"]
        call = getattr(self.claim_reader, "hold_zero_claim", None)
        _require(callable(call), "trusted-worker-zero-claim-fence-unbound")
        fence = call(generation_ref=payload["generation_ref"], actor_ref=payload["actor_ref"],
                     expected_revision=decision["zero_claim_revision"])
        _require(callable(getattr(fence, "__enter__", None)) and
                 callable(getattr(fence, "__exit__", None)) and
                 callable(getattr(fence, "assert_current", None)),
                 "trusted-worker-zero-claim-fence-invalid")
        return fence

    def read_current_task(self, *, generation_ref: str, actor_ref: str,
                          source_revision: str, method_fingerprint: str,
                          task_ref: str) -> dict:
        """Consume an already-issued PM task within the current parent mandate."""
        mandate = self._port(self.mandate_reader, "read_current",
                             generation_ref=generation_ref, actor_ref=actor_ref)
        self._mandate(mandate, generation_ref=generation_ref, actor_ref=actor_ref,
                      source_revision=source_revision)
        task = self._port(self.claim_reader, "read_current_task",
                          generation_ref=generation_ref, actor_ref=actor_ref,
                          task_ref=task_ref)
        _require(task.get("schema") == "cerebro.worker-pm-task-decision/v1" and
                 task.get("current") is True and task.get("revoked") is False and
                 task.get("issued_by_role") == "PROJECT_MANAGER" and
                 task.get("generation_ref") == generation_ref and
                 task.get("actor_ref") == actor_ref and task.get("task_ref") == task_ref and
                 task.get("source_revision") == source_revision and
                 task.get("method_fingerprint") == method_fingerprint and
                 task.get("mandate_ref") == mandate["mandate_ref"] and
                 task.get("mandate_revision") == mandate["owner_revision"] and
                 isinstance(task.get("scope"), list) and bool(task["scope"]) and
                 set(task["scope"]) <= set(mandate["allowed_task_scopes"]) and
                 all(_ref(task.get(field)) for field in
                     ("claim_ref", "packet_ref", "queue_ref", "decision_ref",
                      "frontier_ref", "readback_ref")) and
                 type(task.get("owner_revision")) is int and task["owner_revision"] > 0 and
                 task.get("readback_verified") is True,
                 "operative-pm-task-not-current-or-outside-parent-mandate")
        return copy.deepcopy(task)


class SourceStandingGrantWorkerPolicyResolver:
    """Compose a verified standing Human grant with current worker owner readers.

    The standing-grant, Human-intent and parent-mandate readers are trusted
    owner ports, not JSON values or environment variables. Their current
    readbacks are fenced by revision while an attach decision is consumed.
    This class authenticates none of those readers; host composition must bind
    state-backed/provider-verified ports. It remains disabled unless enabled.
    """

    GRANT_SCHEMA = "cerebro.worker-standing-grant-readback/v1"
    GRANT_SCOPES = {"WORKER_ROLE_ATTACH", "WORKER_TASK_ISSUE"}

    def __init__(self, standing_grant_reader: Any, human_intent_reader: Any,
                 mandate_reader: Any, claim_reader: Any, *, enabled: bool = False):
        _require(type(enabled) is bool, "worker-policy-enabled-flag-invalid")
        self.standing_grant_reader = standing_grant_reader
        self.enabled = enabled
        self._intent_capture = _OwnerReadbackCapture(human_intent_reader)
        self._mandate_capture = _OwnerReadbackCapture(mandate_reader)
        self.policy = BoundWorkerPolicyResolver(
            self._intent_capture, self._mandate_capture, claim_reader, enabled=enabled,
        )
        self._pending_grants: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.RLock()

    def _grant(self, *, generation_ref: str, source_revision: str,
               method_fingerprint: str) -> dict:
        _require(self.enabled, "worker-policy-default-off")
        _require(_ref(generation_ref) and _ref(source_revision) and
                 _ref(method_fingerprint),
                 "worker-standing-grant-scope-input-required")
        reader = self.standing_grant_reader
        call = getattr(reader, "read_current", None)
        _require(callable(call), "trusted-worker-standing-grant-reader-unbound")
        try:
            value = call(generation_ref=generation_ref, source_revision=source_revision,
                         method_fingerprint=method_fingerprint)
        except WorkerContextAuthError:
            raise
        except Exception as exc:
            raise WorkerContextAuthError("worker-standing-grant-read-unavailable") from exc
        _require(isinstance(value, dict), "worker-standing-grant-readback-required")
        value = copy.deepcopy(value)
        _require(set(value) == {
            "schema", "current", "revoked", "readback_verified", "generation_ref",
            "source_revision", "method_fingerprint", "grantor_role", "grantee_role",
            "scopes", "grant_ref", "owner_revision", "readback_ref",
        }, "worker-standing-grant-readback-fields-invalid")
        _require(value.get("schema") == self.GRANT_SCHEMA and
                 value.get("current") is True and value.get("revoked") is False and
                 value.get("readback_verified") is True,
                 "worker-standing-grant-not-current-or-verified")
        _require(value.get("generation_ref") == generation_ref and
                 value.get("source_revision") == source_revision and
                 value.get("method_fingerprint") == method_fingerprint,
                 "worker-standing-grant-generation-source-method-mismatch")
        _require(value.get("grantor_role") == "HUMAN" and
                 value.get("grantee_role") == "PROJECT_MANAGER" and
                 isinstance(value.get("scopes"), list) and
                 len(value["scopes"]) == len(self.GRANT_SCOPES) and
                 set(value["scopes"]) == self.GRANT_SCOPES,
                 "worker-standing-grant-human-pm-scope-required")
        _require(_ref(value.get("grant_ref")) and _ref(value.get("readback_ref")) and
                 type(value.get("owner_revision")) is int and value["owner_revision"] > 0,
                 "worker-standing-grant-owner-readback-required")
        return value

    def resolve(self, *, pre_role_generation: dict, verified_source: dict,
                identity: Any, session_ref: str) -> dict:
        generation = pre_role_generation.get("generation_ref")
        source = pre_role_generation.get("source_revision")
        method = verified_source.get("method_fingerprint")
        grant = self._grant(generation_ref=generation, source_revision=source,
                            method_fingerprint=method)
        self._intent_capture.clear()
        self._mandate_capture.clear()
        decision = self.policy.resolve(
            pre_role_generation=pre_role_generation, verified_source=verified_source,
            identity=identity, session_ref=session_ref,
        )
        decision_ref = decision.get("decision_ref")
        _require(_ref(decision_ref), "worker-standing-grant-decision-ref-required")
        intent = self._intent_capture.take()
        mandate = self._mandate_capture.take()
        _require(isinstance(intent, dict) and isinstance(mandate, dict) and
                 intent.get("intent_ref") == decision_ref and
                 mandate.get("mandate_ref") == decision.get("mandate_ref") and
                 type(intent.get("owner_revision")) is int and intent["owner_revision"] > 0 and
                 type(mandate.get("owner_revision")) is int and mandate["owner_revision"] > 0,
                 "worker-current-owner-readbacks-not-bound-to-decision")
        binding = {
            "generation_ref": generation,
            "source_revision": source,
            "method_fingerprint": method,
            "grant_ref": grant["grant_ref"],
            "grant_revision": grant["owner_revision"],
            "principal_ref": identity.principal_ref,
            "session_ref": session_ref,
            "intent_ref": intent["intent_ref"],
            "intent_revision": intent["owner_revision"],
            "actor_ref": intent["actor_ref"],
            "mandate_ref": mandate["mandate_ref"],
            "mandate_revision": mandate["owner_revision"],
        }
        with self._pending_lock:
            self._pending_grants[id(decision)] = binding
        return decision

    def hold_zero_claim(self, decision: dict):
        _require(self.enabled, "worker-policy-default-off")
        decision_ref = decision.get("decision_ref") if isinstance(decision, dict) else None
        with self._pending_lock:
            binding = self._pending_grants.pop(id(decision), None)
        _require(binding is not None and binding.get("intent_ref") == decision_ref and
                 binding.get("mandate_ref") == decision.get("mandate_ref"),
                 "worker-standing-grant-decision-not-bound")
        grant_fence = self._fence(
            self.standing_grant_reader, "standing-grant",
            generation_ref=binding["generation_ref"],
            source_revision=binding["source_revision"],
            method_fingerprint=binding["method_fingerprint"],
            grant_ref=binding["grant_ref"], expected_revision=binding["grant_revision"],
        )
        intent_fence = self._fence(
            self._intent_capture, "human-intent",
            generation_ref=binding["generation_ref"],
            principal_ref=binding["principal_ref"], session_ref=binding["session_ref"],
            intent_ref=binding["intent_ref"], expected_revision=binding["intent_revision"],
        )
        mandate_fence = self._fence(
            self._mandate_capture, "parent-mandate",
            generation_ref=binding["generation_ref"], actor_ref=binding["actor_ref"],
            mandate_ref=binding["mandate_ref"], expected_revision=binding["mandate_revision"],
        )
        claim_fence = self.policy.hold_zero_claim(decision)
        return _WorkerStandingGrantAndClaimFence(
            grant_fence, intent_fence, mandate_fence, claim_fence,
        )

    @staticmethod
    def _fence(reader: Any, label: str, **kwargs: Any) -> Any:
        call = getattr(reader, "hold_current", None)
        _require(callable(call), "trusted-worker-" + label + "-fence-unbound")
        try:
            return call(**kwargs)
        except WorkerContextAuthError:
            raise
        except Exception as exc:
            raise WorkerContextAuthError("worker-" + label + "-fence-unavailable") from exc

    def read_current_task(self, *, generation_ref: str, actor_ref: str,
                          source_revision: str, method_fingerprint: str,
                          task_ref: str) -> dict:
        self._grant(generation_ref=generation_ref, source_revision=source_revision,
                    method_fingerprint=method_fingerprint)
        return self.policy.read_current_task(
            generation_ref=generation_ref, actor_ref=actor_ref,
            source_revision=source_revision, method_fingerprint=method_fingerprint,
            task_ref=task_ref,
        )


class _OwnerReadbackCapture:
    """Capture the exact current receipt used by the resolver in this thread."""

    def __init__(self, reader: Any):
        self.reader = reader
        self._local = threading.local()

    def clear(self) -> None:
        self._local.value = None

    def read_current(self, **kwargs: Any) -> Any:
        call = getattr(self.reader, "read_current", None)
        _require(callable(call), "trusted-worker-owner-port-unbound:read_current")
        value = call(**kwargs)
        self._local.value = copy.deepcopy(value) if isinstance(value, dict) else None
        return value

    def take(self) -> Any:
        value = getattr(self._local, "value", None)
        self._local.value = None
        return value

    def hold_current(self, **kwargs: Any) -> Any:
        call = getattr(self.reader, "hold_current", None)
        _require(callable(call), "trusted-worker-owner-fence-unbound")
        return call(**kwargs)


class _WorkerStandingGrantAndClaimFence:
    """Hold all current authority and zero-claim fences across attach/readback."""

    def __init__(self, grant_fence: Any, intent_fence: Any,
                 mandate_fence: Any, claim_fence: Any):
        self.grant_fence = grant_fence
        self.intent_fence = intent_fence
        self.mandate_fence = mandate_fence
        self.claim_fence = claim_fence
        self._entered: list[Any] = []

    @staticmethod
    def _validate(fence: Any, label: str) -> None:
        _require(callable(getattr(fence, "__enter__", None)) and
                 callable(getattr(fence, "__exit__", None)) and
                 callable(getattr(fence, "assert_current", None)),
                 "trusted-worker-" + label + "-fence-invalid")

    def __enter__(self):
        try:
            for fence, label in ((self.grant_fence, "standing-grant"),
                                 (self.intent_fence, "human-intent"),
                                 (self.mandate_fence, "parent-mandate"),
                                 (self.claim_fence, "zero-claim")):
                self._validate(fence, label)
                fence.__enter__()
                self._entered.append(fence)
                fence.assert_current()
        except Exception as exc:
            for fence in reversed(self._entered):
                try:
                    fence.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    pass
            self._entered.clear()
            raise
        return self

    def assert_current(self) -> None:
        for fence in self._entered:
            fence.assert_current()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        suppress = False
        exit_error = None
        for fence in reversed(self._entered):
            try:
                suppress = bool(fence.__exit__(exc_type, exc, traceback)) or suppress
            except Exception as error:
                if exit_error is None:
                    exit_error = error
        self._entered.clear()
        if exit_error is not None:
            raise exit_error
        return suppress

PM_RIGHTS_SCHEMA = "cerebro.pm-human-rights/v2"
PM_DELEGATION_SCHEMA = "cerebro.pm-generation-delegation/v2"
PM_BIND_RECEIPT_SCHEMA = "cerebro.pm-generation-delegation-bind/v1"
PM_EFFECT_RECEIPT_SCHEMA = "cerebro.pm-ordinary-effect-receipt/v1"
PM_EFFECT_OUTCOME_SCHEMA = "cerebro.pm-ordinary-effect-outcome/v1"
PM_TASK_AUTHORITY_SCHEMA = "cerebro.pm-authority-task/v2"
UNKNOWN_COMMIT = "UNKNOWN_COMMIT"


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class BoundPmAuthorityV02:
    """Default-off v0.2 PM authority join at the ordinary WORKER task consumer.

    This class authenticates no Human or provider by itself.  Rights,
    delegation, administrative binding and effect-state ports are injected
    owner capabilities.  Local fixtures can prove fail-closed mechanics only.

    Human semantic rights remain immutable owner facts.  Technical PM
    generation binding may be bootstrapped/renewed only through the separate
    administrative owner port; a PROJECT_MANAGER cannot authorize that bind.
    """

    def __init__(
        self,
        worker_policy: BoundWorkerPolicyResolver,
        rights_reader: Any,
        delegation_reader: Any,
        admin_owner: Any,
        effect_owner: Any,
        *,
        enabled: bool = False,
    ):
        self.worker_policy = worker_policy
        self.rights_reader = rights_reader
        self.delegation_reader = delegation_reader
        self.admin_owner = admin_owner
        self.effect_owner = effect_owner
        self.enabled = enabled

    def _port(self, obj: Any, method: str, **kwargs: Any) -> dict:
        _require(self.enabled, "pm-authority-v02-default-off")
        call = getattr(obj, method, None)
        _require(callable(call), "pm-authority-v02-owner-port-unbound:" + method)
        try:
            value = call(**kwargs)
        except WorkerContextAuthError:
            raise
        except Exception as exc:
            raise WorkerContextAuthError(
                "pm-authority-v02-owner-read-unavailable:" + method
            ) from exc
        _require(isinstance(value, dict), "pm-authority-v02-owner-receipt-required:" + method)
        return copy.deepcopy(value)

    @staticmethod
    def _rights_basis(value: dict) -> dict:
        return {
            "mandate_ref": value.get("mandate_ref"),
            "semantic_revision": value.get("semantic_revision"),
            "grantor_role": value.get("grantor_role"),
            "human_decision_refs": copy.deepcopy(value.get("human_decision_refs")),
            "operations": copy.deepcopy(value.get("operations")),
            "operation_scopes": copy.deepcopy(value.get("operation_scopes")),
            "validity": copy.deepcopy(value.get("validity")),
            "renewal_rule_ref": value.get("renewal_rule_ref"),
        }

    @classmethod
    def _rights(cls, value: dict, *, mandate_ref: str) -> dict:
        _require(value.get("schema") == PM_RIGHTS_SCHEMA, "pm-rights-schema-mismatch")
        _require(
            value.get("mandate_ref") == mandate_ref
            and type(value.get("semantic_revision")) is int
            and value["semantic_revision"] > 0,
            "pm-rights-version-mismatch",
        )
        _require(
            value.get("grantor_role") == "HUMAN"
            and value.get("current") is True
            and value.get("revoked") is False
            and value.get("readback_verified") is True,
            "pm-rights-not-current-human-owned",
        )
        decisions = value.get("human_decision_refs")
        operations = value.get("operations")
        scopes = value.get("operation_scopes")
        held = value.get("held_operations", [])
        _require(
            isinstance(decisions, list) and decisions and all(_ref(x) for x in decisions),
            "pm-rights-human-origin-required",
        )
        _require(
            isinstance(operations, list) and operations and len(set(operations)) == len(operations)
            and all(_ref(x) for x in operations),
            "pm-rights-operations-invalid",
        )
        _require(
            isinstance(scopes, dict) and set(scopes) == set(operations)
            and all(
                isinstance(scopes[op], list)
                and scopes[op]
                and all(_ref(scope) for scope in scopes[op])
                for op in operations
            ),
            "pm-rights-operation-scopes-invalid",
        )
        _require(
            isinstance(held, list) and set(held) <= set(operations),
            "pm-rights-held-operations-invalid",
        )
        validity = value.get("validity")
        _require(
            isinstance(validity, dict)
            and type(validity.get("not_before_owner_revision")) is int
            and validity["not_before_owner_revision"] > 0
            and (
                validity.get("expires_after_owner_revision") is None
                or (
                    type(validity.get("expires_after_owner_revision")) is int
                    and validity["expires_after_owner_revision"] >= validity["not_before_owner_revision"]
                )
            ),
            "pm-rights-validity-invalid",
        )
        owner_revision = value.get("owner_revision")
        _require(
            type(owner_revision) is int
            and owner_revision >= validity["not_before_owner_revision"]
            and (
                validity["expires_after_owner_revision"] is None
                or owner_revision <= validity["expires_after_owner_revision"]
            )
            and _ref(value.get("readback_ref"))
            and _ref(value.get("renewal_rule_ref")),
            "pm-rights-owner-readback-or-validity-invalid",
        )
        basis = cls._rights_basis(value)
        _require(
            _hex64(value.get("basis_sha256"))
            and value["basis_sha256"] == _canonical_sha256(basis),
            "pm-rights-immutable-basis-mismatch",
        )
        return copy.deepcopy(value)

    @staticmethod
    def _delegation(
        value: dict,
        *,
        rights: dict,
        pm_actor_ref: str,
        pm_generation_ref: str,
    ) -> dict:
        _require(value.get("schema") == PM_DELEGATION_SCHEMA, "pm-delegation-schema-mismatch")
        _require(
            value.get("current") is True
            and value.get("revoked") is False
            and value.get("readback_verified") is True,
            "pm-delegation-not-current",
        )
        _require(
            value.get("pm_actor_ref") == pm_actor_ref
            and value.get("pm_generation_ref") == pm_generation_ref
            and value.get("mandate_ref") == rights["mandate_ref"]
            and value.get("mandate_semantic_revision") == rights["semantic_revision"],
            "pm-delegation-binding-mismatch",
        )
        _require(
            value.get("authorized_by_role") == "HUMAN_ADMIN_OWNER"
            and _ref(value.get("administrative_grant_ref")),
            "pm-delegation-self-grant-prohibited",
        )
        ops = value.get("operations")
        scopes = value.get("scopes")
        _require(
            isinstance(ops, list) and ops and set(ops) <= set(rights["operations"])
            and isinstance(scopes, list) and scopes and all(_ref(x) for x in scopes),
            "pm-delegation-scope-invalid",
        )
        all_right_scopes = {
            scope for op in ops for scope in rights["operation_scopes"].get(op, [])
        }
        _require(set(scopes) <= all_right_scopes, "pm-delegation-scope-exceeds-human-rights")
        _require(
            value.get("renewal_rule_ref") == rights["renewal_rule_ref"]
            and type(value.get("owner_revision")) is int
            and value["owner_revision"] > 0
            and type(value.get("fence_revision")) is int
            and value["fence_revision"] > 0
            and _ref(value.get("delegation_ref"))
            and _ref(value.get("readback_ref")),
            "pm-delegation-owner-readback-invalid",
        )
        return copy.deepcopy(value)

    def read_current_rights(self, mandate_ref: str) -> dict:
        value = self._port(self.rights_reader, "read_current", mandate_ref=mandate_ref)
        return self._rights(value, mandate_ref=mandate_ref)

    def bind_or_renew_pm_generation(
        self,
        *,
        mandate_ref: str,
        pm_actor_ref: str,
        pm_generation_ref: str,
        previous_pm_generation_ref: str | None = None,
        succession_ref: str | None = None,
    ) -> dict:
        """Bootstrap/renew only through an injected administrative owner path."""
        rights = self.read_current_rights(mandate_ref)
        if previous_pm_generation_ref is None:
            action = "BOOTSTRAP"
        elif previous_pm_generation_ref == pm_generation_ref:
            _require(
                rights["renewal_rule_ref"] == "SAME_ACTOR_SAME_GENERATION_TECHNICAL_RENEWAL",
                "pm-delegation-renewal-rule-absent",
            )
            action = "RENEW"
        else:
            _require(_ref(succession_ref), "pm-new-generation-explicit-succession-required")
            action = "SUCCESSION"

        receipt = self._port(
            self.admin_owner,
            "bind_or_renew",
            action=action,
            mandate_ref=rights["mandate_ref"],
            mandate_semantic_revision=rights["semantic_revision"],
            mandate_basis_sha256=rights["basis_sha256"],
            pm_actor_ref=pm_actor_ref,
            pm_generation_ref=pm_generation_ref,
            previous_pm_generation_ref=previous_pm_generation_ref,
            succession_ref=succession_ref,
        )
        _require(
            receipt.get("authorized_by_role") == "HUMAN_ADMIN_OWNER",
            "pm-administrative-self-grant-prohibited",
        )
        _require(
            receipt.get("schema") == PM_BIND_RECEIPT_SCHEMA
            and receipt.get("action") == action
            and receipt.get("mandate_ref") == rights["mandate_ref"]
            and receipt.get("mandate_semantic_revision") == rights["semantic_revision"]
            and receipt.get("mandate_basis_sha256") == rights["basis_sha256"]
            and receipt.get("pm_actor_ref") == pm_actor_ref
            and receipt.get("pm_generation_ref") == pm_generation_ref
            and _ref(receipt.get("administrative_grant_ref"))
            and _ref(receipt.get("delegation_ref"))
            and type(receipt.get("owner_revision")) is int
            and receipt["owner_revision"] > 0
            and receipt.get("readback_verified") is True
            and _ref(receipt.get("readback_ref")),
            "pm-administrative-bind-receipt-invalid",
        )
        if action == "SUCCESSION":
            _require(receipt.get("succession_ref") == succession_ref, "pm-succession-receipt-mismatch")

        delegation = self._port(
            self.delegation_reader,
            "read_current",
            pm_actor_ref=pm_actor_ref,
            pm_generation_ref=pm_generation_ref,
            mandate_ref=rights["mandate_ref"],
        )
        delegation = self._delegation(
            delegation,
            rights=rights,
            pm_actor_ref=pm_actor_ref,
            pm_generation_ref=pm_generation_ref,
        )
        _require(
            delegation["delegation_ref"] == receipt["delegation_ref"]
            and delegation["administrative_grant_ref"] == receipt["administrative_grant_ref"]
            and delegation["owner_revision"] >= receipt["owner_revision"],
            "pm-delegation-linked-readback-mismatch",
        )
        return {
            "rights": rights,
            "delegation": delegation,
            "bind_receipt": receipt,
        }

    @staticmethod
    def _task_basis(
        *,
        rights: dict,
        delegation: dict,
        task: dict,
        worker_generation_ref: str,
        worker_actor_ref: str,
    ) -> dict:
        return {
            "mandate_ref": rights["mandate_ref"],
            "mandate_semantic_revision": rights["semantic_revision"],
            "mandate_basis_sha256": rights["basis_sha256"],
            "human_decision_refs": copy.deepcopy(rights["human_decision_refs"]),
            "delegation_ref": delegation["delegation_ref"],
            "pm_actor_ref": delegation["pm_actor_ref"],
            "pm_generation_ref": delegation["pm_generation_ref"],
            "worker_generation_ref": worker_generation_ref,
            "worker_actor_ref": worker_actor_ref,
            "decision_ref": task["decision_ref"],
            "task_ref": task["task_ref"],
            "task_revision": task["task_revision"],
            "task_sha256": task["task_sha256"],
            "operation": task["operation"],
            "authority_scope": copy.deepcopy(task["authority_scope"]),
            "idempotency_key": task["idempotency_key"],
            "effect_target_ref": task["effect_target_ref"],
        }

    def read_current_consumer_authority(
        self,
        *,
        mandate_ref: str,
        pm_actor_ref: str,
        pm_generation_ref: str,
        worker_generation_ref: str,
        worker_actor_ref: str,
        source_revision: str,
        method_fingerprint: str,
        task_ref: str,
        expected_task_revision: int,
        expected_task_sha256: str,
    ) -> dict:
        rights = self.read_current_rights(mandate_ref)
        delegation = self._port(
            self.delegation_reader,
            "read_current",
            pm_actor_ref=pm_actor_ref,
            pm_generation_ref=pm_generation_ref,
            mandate_ref=mandate_ref,
        )
        delegation = self._delegation(
            delegation,
            rights=rights,
            pm_actor_ref=pm_actor_ref,
            pm_generation_ref=pm_generation_ref,
        )
        task = self.worker_policy.read_current_task(
            generation_ref=worker_generation_ref,
            actor_ref=worker_actor_ref,
            source_revision=source_revision,
            method_fingerprint=method_fingerprint,
            task_ref=task_ref,
        )
        _require(
            task.get("authority_schema") == PM_TASK_AUTHORITY_SCHEMA
            and task.get("authority_mandate_ref") == rights["mandate_ref"]
            and task.get("authority_mandate_revision") == rights["semantic_revision"]
            and task.get("delegation_ref") == delegation["delegation_ref"]
            and task.get("pm_actor_ref") == pm_actor_ref
            and task.get("pm_generation_ref") == pm_generation_ref
            and type(task.get("task_revision")) is int
            and task["task_revision"] == expected_task_revision
            and _hex64(task.get("task_sha256"))
            and task["task_sha256"] == expected_task_sha256
            and _ref(task.get("operation"))
            and isinstance(task.get("authority_scope"), list)
            and task["authority_scope"]
            and all(_ref(x) for x in task["authority_scope"])
            and _ref(task.get("idempotency_key"))
            and _ref(task.get("effect_target_ref")),
            "pm-v02-task-basis-mismatch",
        )
        operation = task["operation"]
        _require(operation in rights["operations"], "pm-operation-outside-human-rights")
        _require(operation not in rights.get("held_operations", []), "pm-operation-class-held")
        _require(operation in delegation["operations"], "pm-operation-outside-delegation")
        _require(
            set(task["authority_scope"]) <= set(rights["operation_scopes"][operation])
            and set(task["authority_scope"]) <= set(delegation["scopes"]),
            "pm-task-authority-scope-exceeds-current-rights",
        )
        basis = self._task_basis(
            rights=rights,
            delegation=delegation,
            task=task,
            worker_generation_ref=worker_generation_ref,
            worker_actor_ref=worker_actor_ref,
        )
        return {
            "rights": rights,
            "delegation": delegation,
            "task": task,
            "immutable_basis": basis,
            "basis_fingerprint": _canonical_sha256(basis),
        }

    def nal_transition_parent_fence_factory(self, snapshot: dict):
        """Bind one NAL transition to the already-validated PM authority snapshot.

        The returned factory grants no authority itself.  PgOwnerBindingEvidence
        composes it with the child binding row fence inside the NAL transaction.
        Production remains unbound until the same trusted PM owner ports used by
        this object are lawfully wired to the NAL adapter.
        """
        basis = snapshot.get("immutable_basis")
        _require(
            isinstance(basis, dict)
            and snapshot.get("basis_fingerprint") == _canonical_sha256(basis),
            "pm-v02-snapshot-basis-tampered",
        )

        def factory(*, binding_ref: str, transition: str, task: dict,
                    expected_binding: dict, cursor=None):
            _require(
                transition in {"WORK_CONSUME", "WORK_START", "WORK_TERMINAL", "TASK_CLOSE"},
                "nal-transition-kind-invalid",
            )
            task_decision = snapshot.get("task")
            _require(isinstance(task_decision, dict), "nal-parent-task-snapshot-required")
            exact = (
                expected_binding.get("binding_ref") == binding_ref
                and expected_binding.get("actor_ref") == basis["worker_actor_ref"]
                and expected_binding.get("generation_ref") == basis["worker_generation_ref"]
                and expected_binding.get("task_ref") == basis["task_ref"]
                and expected_binding.get("claim_ref") == task_decision.get("claim_ref")
                and expected_binding.get("packet_ref") == task_decision.get("packet_ref")
                and expected_binding.get("queue_ref") == task_decision.get("queue_ref")
                and expected_binding.get("task_payload_sha256") == basis["task_sha256"]
                and task.get("task_ref") == basis["task_ref"]
                and task.get("task_payload_sha256") == basis["task_sha256"]
                and task.get("claim_ref") == task_decision.get("claim_ref")
                and task.get("packet_ref") == task_decision.get("packet_ref")
                and task.get("queue_ref") == task_decision.get("queue_ref")
            )
            _require(exact, "nal-parent-child-authority-link-mismatch")
            if "task_revision" in task:
                _require(
                    task.get("task_revision") == basis["task_revision"]
                    and task.get("decision_ref") == basis["decision_ref"]
                    and task.get("provenance_ref") == task_decision.get("readback_ref"),
                    "nal-parent-task-provenance-mismatch",
                )
            else:
                raise WorkerContextAuthError("nal-parent-task-provenance-required")
            return self._effect_fence(snapshot, cursor=cursor)

        return factory

    def _effect_fence(self, snapshot: dict, *, cursor=None):
        call = getattr(self.effect_owner, "hold_current", None)
        _require(callable(call), "pm-effect-owner-fence-unbound")
        try:
            arguments = {
                "basis": copy.deepcopy(snapshot["immutable_basis"]),
                "basis_fingerprint": snapshot["basis_fingerprint"],
                "expected_mandate_owner_revision": snapshot["rights"]["owner_revision"],
                "expected_delegation_owner_revision": snapshot["delegation"]["owner_revision"],
                "expected_delegation_fence_revision": snapshot["delegation"]["fence_revision"],
            }
            if cursor is not None:
                arguments["cursor"] = cursor
                arguments["expected_task"] = {
                    key: snapshot["task"][key] for key in
                    ("claim_ref", "packet_ref", "queue_ref", "readback_ref")
                }
            fence = call(**arguments)
        except WorkerContextAuthError:
            raise
        except Exception as exc:
            raise WorkerContextAuthError("pm-effect-owner-fence-unavailable") from exc
        _require(
            callable(getattr(fence, "__enter__", None))
            and callable(getattr(fence, "__exit__", None))
            and callable(getattr(fence, "assert_current", None)),
            "pm-effect-owner-fence-invalid",
        )
        return fence

    @staticmethod
    def _receipt(value: dict, snapshot: dict) -> dict:
        basis = snapshot["immutable_basis"]
        _require(
            value.get("schema") == PM_EFFECT_RECEIPT_SCHEMA
            and value.get("state") == "COMMITTED"
            and value.get("decision_ref") == basis["decision_ref"]
            and value.get("task_ref") == basis["task_ref"]
            and value.get("idempotency_key") == basis["idempotency_key"]
            and value.get("basis_fingerprint") == snapshot["basis_fingerprint"]
            and value.get("readback_verified") is True
            and _ref(value.get("result_ref"))
            and _ref(value.get("readback_ref"))
            and type(value.get("owner_revision")) is int
            and value["owner_revision"] > 0,
            "pm-effect-commit-receipt-invalid",
        )
        return copy.deepcopy(value)

    def _read_effect_outcome(self, snapshot: dict) -> dict:
        basis = snapshot["immutable_basis"]
        value = self._port(
            self.effect_owner,
            "read_outcome",
            decision_ref=basis["decision_ref"],
            idempotency_key=basis["idempotency_key"],
        )
        _require(
            value.get("schema") == PM_EFFECT_OUTCOME_SCHEMA
            and value.get("decision_ref") == basis["decision_ref"]
            and value.get("idempotency_key") == basis["idempotency_key"]
            and value.get("basis_fingerprint") == snapshot["basis_fingerprint"]
            and value.get("readback_verified") is True,
            "pm-effect-outcome-readback-invalid",
        )
        state = value.get("state")
        _require(state in {"COMMITTED", "NO_COMMIT", "UNKNOWN"}, "pm-effect-outcome-state-invalid")
        if state == "COMMITTED":
            receipt = value.get("receipt")
            _require(isinstance(receipt, dict), "pm-effect-committed-receipt-required")
            return {"state": state, "receipt": self._receipt(receipt, snapshot)}
        return {"state": state}

    def apply_current_task_once(self, snapshot: dict) -> dict:
        """Apply one already-authorized ordinary operation under an owner fence.

        If the owner port was invoked but a durable outcome cannot be proven,
        return UNKNOWN_COMMIT.  Never translate ambiguity into zero effect.
        """
        _require(self.enabled, "pm-authority-v02-default-off")
        basis = snapshot.get("immutable_basis")
        _require(
            isinstance(basis, dict)
            and snapshot.get("basis_fingerprint") == _canonical_sha256(basis),
            "pm-v02-snapshot-basis-tampered",
        )
        fence = self._effect_fence(snapshot)
        invoked = False
        try:
            with fence:
                fence.assert_current()
                invoked = True
                call = getattr(self.effect_owner, "apply_once", None)
                _require(callable(call), "pm-effect-owner-apply-unbound")
                call(
                    basis=copy.deepcopy(basis),
                    basis_fingerprint=snapshot["basis_fingerprint"],
                )
        except WorkerContextAuthError:
            raise
        except Exception:
            if not invoked:
                raise WorkerContextAuthError("pm-effect-denied-before-owner-invocation")
            return {
                "result": UNKNOWN_COMMIT,
                "basis_fingerprint": snapshot["basis_fingerprint"],
                "decision_ref": basis["decision_ref"],
                "idempotency_key": basis["idempotency_key"],
            }

        try:
            outcome = self._read_effect_outcome(snapshot)
        except WorkerContextAuthError:
            return {
                "result": UNKNOWN_COMMIT,
                "basis_fingerprint": snapshot["basis_fingerprint"],
                "decision_ref": basis["decision_ref"],
                "idempotency_key": basis["idempotency_key"],
            }
        if outcome["state"] == "COMMITTED":
            return {
                "result": "APPLIED",
                "receipt": outcome["receipt"],
                "basis_fingerprint": snapshot["basis_fingerprint"],
            }
        if outcome["state"] == "NO_COMMIT":
            return {
                "result": "NO_COMMIT_PROVEN",
                "basis_fingerprint": snapshot["basis_fingerprint"],
            }
        return {
            "result": UNKNOWN_COMMIT,
            "basis_fingerprint": snapshot["basis_fingerprint"],
            "decision_ref": basis["decision_ref"],
            "idempotency_key": basis["idempotency_key"],
        }

    def reconcile_unknown(self, snapshot: dict) -> dict:
        _require(
            snapshot.get("basis_fingerprint")
            == _canonical_sha256(snapshot.get("immutable_basis")),
            "pm-v02-snapshot-basis-tampered",
        )
        try:
            outcome = self._read_effect_outcome(snapshot)
        except WorkerContextAuthError:
            return {
                "result": UNKNOWN_COMMIT,
                "basis_fingerprint": snapshot["basis_fingerprint"],
            }
        if outcome["state"] == "COMMITTED":
            return {
                "result": "APPLIED_RECONCILED",
                "receipt": outcome["receipt"],
                "basis_fingerprint": snapshot["basis_fingerprint"],
            }
        if outcome["state"] == "NO_COMMIT":
            return {
                "result": "NO_COMMIT_PROVEN",
                "basis_fingerprint": snapshot["basis_fingerprint"],
            }
        return {
            "result": UNKNOWN_COMMIT,
            "basis_fingerprint": snapshot["basis_fingerprint"],
        }
