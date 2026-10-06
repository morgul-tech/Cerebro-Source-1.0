#!/usr/bin/env python3
"""Normal host binding for canonical MCP owner-effect resolution and execution.

The host constructs capability and persistence-verification dependencies from
trusted runtime objects. Event payloads may carry owner receipts, but they can
neither provide these dependencies nor self-assert executability or durability.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

import control_owner_routing
import control_resolution
from human_t3_break_glass import HumanT3BreakGlassHost


OWNER_ORDER = ("project", "quality", "convergence", "context")
EXPECTED_EFFECT = {
    "project": "REVISION_REQUIRED",
    "quality": "INVALIDATE_AFFECTED",
    "convergence": "REVALIDATE_AFFECTED",
    "context": "REFRESH_GOVERNING_REFS",
}
PERSISTENCE_VERIFICATION_SCHEMA = "cerebro-owner-state-persistence-verification/v1"
PM_AUTHORIZED_COMMAND_STATE_SCHEMA = "cerebro-pm-authorized-command-state/v1"
PM_AUTHORIZED_COMMAND_CONSUMPTION_SCHEMA = "cerebro-pm-authorized-command-consumption/v1"
PM_COMMAND_CHAIN_SCHEMA = "cerebro-pm-command-fixed-point/v1"
OPERATIONAL_PULSE_SCHEMA = "cerebro-operational-pulse-consumption/v1"
OPERATIONAL_PULSE_STAGES = ("FRESH_WORLD", "CURRENT_CONTACT", "PRE_CLOSE")
OPERATIONAL_DEBT_KINDS = ("CORRECTION", "REPORT")
A7_PREPUBLICATION_BASIS_SCHEMA = "cerebro-a7-pm-prepublication-basis/v1"
A7_PREPUBLICATION_DECISION_SCHEMA = "cerebro-a7-pm-prepublication-decision/v1"
PM_GUARDED_PUBLICATION_SCHEMA = "cerebro-pm-guarded-disposition-publication/v1"
PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA = "cerebro-pm-guarded-disposition-publication-receipt/v1"
PM_DISPOSITION_REQUEST_SCHEMA = "cerebro-pm-disposition-publication-request/v1"
A7_FLOW_REDUCING_DISPOSITIONS = frozenset({
    "HOLD", "WAIT", "UNBOUND", "BLOCK", "NOT_READY", "OMIT_BIND", "OMIT_SEND",
    "ADOPTION_DELAY", "OWNER_HUNT", "RESEARCH_DETOUR", "LOCAL_TO_GLOBAL_STOP",
})
A7_RELEVANT_CASE_REFS = frozenset({
    "A7-P22-PM-FLOW-STOP-008",
    "A7-P22-NATS-CLIENT-HOST-MISMATCH-009",
    "A7-P22-PASSIVE-HOLD-NEGATIVE-010",
})
PM_FIXED_POINT_STOP_REASONS = (
    "QUIESCENT",
    "OTHER_OWNER_WAIT",
    "REAL_HUMAN_GATE",
    "CAPABILITY_MISSING",
    "CONFLICT",
    "UNKNOWN_HOLD",
    "NONPROGRESS_CYCLE",
)
PROHIBITED_RUNTIME_INJECTION_KEYS = {
    "persistence_evidence_verifier",
    "owner_persistence_verifier",
    "runtime_capability_resolver",
    "capability_resolver",
    "capability_available",
    "verifier_callable",
    "executor_callable",
    "pm_command_executor",
    "authorized_command_executor",
    "command_executor",
    "pm_profile_verifier",
    "lifecycle_effect_verifier",
    "human_t3_host",
    "human_t3_current_reader",
    "human_t3_effect_capability",
    "effect_capability",
    "current_reader",
    "a7_predecision_basis_reader",
    "prepublication_basis_reader",
    "pm_disposition_publisher",
    "publisher_port",
    "prepublication_guard",
}


class ControlResolutionHostError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlResolutionHostError(message)


def _reject_runtime_authority_injection(value: Any, path: str = "request") -> None:
    if isinstance(value, dict):
        forbidden = sorted(PROHIBITED_RUNTIME_INJECTION_KEYS.intersection(value))
        _require(not forbidden, f"runtime-authority-injection-prohibited:{path}:{','.join(forbidden)}")
        for key, item in value.items():
            _reject_runtime_authority_injection(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_runtime_authority_injection(item, f"{path}[{index}]")


def consume_operational_pulse(
    provider_tail_reader: Any | None,
    *,
    stage: str,
    actor_role: str,
    objective_ref: str,
    prior_watermarks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read authoritative external tails and derive bounded debt/currentness effects."""

    stage = str(stage or "").upper()
    actor_role = str(actor_role or "").upper()
    objective_ref = str(objective_ref or "").strip()
    _require(stage in OPERATIONAL_PULSE_STAGES, "operational-pulse-stage-invalid")
    _require(actor_role, "operational-pulse-actor-role-required")
    _require(objective_ref, "operational-pulse-objective-ref-required")
    prior = copy.deepcopy(prior_watermarks) if isinstance(prior_watermarks, dict) else {}
    read_tails = getattr(provider_tail_reader, "read_operational_tails", None)
    if not callable(read_tails):
        return {
            "schema": OPERATIONAL_PULSE_SCHEMA,
            "stage": stage,
            "result": "UNKNOWN_HOLD",
            "currentness": "UNKNOWN",
            "delta": "NONE",
            "exact_blocker": "PROVIDER_TAIL_READER_UNBOUND",
            "terminal_blocked": stage == "PRE_CLOSE",
            "watermarks": {},
            "relevant_debt": [],
            "persistence_directive": None,
        }

    observed = read_tails(
        stage=stage,
        actor_role=actor_role,
        objective_ref=objective_ref,
        prior_watermarks=copy.deepcopy(prior),
    )
    _require(isinstance(observed, dict), "operational-pulse-provider-read-object-required")
    control = observed.get("control")
    protobox = observed.get("protobox")
    if not isinstance(control, dict) or not isinstance(protobox, dict):
        return {
            "schema": OPERATIONAL_PULSE_SCHEMA,
            "stage": stage,
            "result": "UNKNOWN_HOLD",
            "currentness": "UNKNOWN",
            "delta": "NONE",
            "exact_blocker": "ADMINPULSE_BOTH_PROVIDER_TAILS_REQUIRED",
            "terminal_blocked": stage == "PRE_CLOSE",
            "watermarks": {},
            "relevant_debt": [],
            "persistence_directive": None,
        }

    control_currentness = str(control.get("currentness") or "UNKNOWN").upper()
    protobox_currentness = str(protobox.get("currentness") or "UNKNOWN").upper()
    _require(control_currentness in {"CURRENT", "STALE", "UNKNOWN"}, "control-tail-currentness-invalid")
    _require(protobox_currentness in {"CURRENT", "STALE", "UNKNOWN"}, "protobox-tail-currentness-invalid")
    watermarks = {
        "control": {
            "carrier_ref": str(control.get("carrier_ref") or "").strip(),
            "provider_revision": control.get("provider_revision"),
            "event_frontier": str(control.get("event_frontier") or "").strip(),
        },
        "protobox": {
            "channel_ref": str(protobox.get("channel_ref") or "").strip(),
            "observed_revision": protobox.get("observed_revision"),
        },
    }
    exact_watermarks = (
        bool(watermarks["control"]["carrier_ref"])
        and isinstance(watermarks["control"]["provider_revision"], int)
        and watermarks["control"]["provider_revision"] >= 0
        and bool(watermarks["control"]["event_frontier"])
        and bool(watermarks["protobox"]["channel_ref"])
        and isinstance(watermarks["protobox"]["observed_revision"], int)
        and watermarks["protobox"]["observed_revision"] >= 0
    )
    if not exact_watermarks or control_currentness != "CURRENT" or protobox_currentness != "CURRENT":
        currentness = "STALE" if "STALE" in {control_currentness, protobox_currentness} else "UNKNOWN"
        return {
            "schema": OPERATIONAL_PULSE_SCHEMA,
            "stage": stage,
            "result": "UNKNOWN_HOLD",
            "currentness": currentness,
            "delta": "NONE",
            "exact_blocker": "ADMINPULSE_CURRENT_EXACT_WATERMARKS_REQUIRED",
            "terminal_blocked": stage == "PRE_CLOSE",
            "watermarks": watermarks,
            "relevant_debt": [],
            "persistence_directive": None,
        }

    relevant: list[dict[str, Any]] = []
    for tail in (control, protobox):
        debts = tail.get("debts", [])
        _require(isinstance(debts, list), "operational-pulse-debts-array-required")
        for debt in debts:
            _require(isinstance(debt, dict), "operational-pulse-debt-object-required")
            kind = str(debt.get("kind") or "").upper()
            owner = str(debt.get("owner_role") or "").upper()
            target = str(debt.get("objective_ref") or "").strip()
            is_relevant = (
                kind in OPERATIONAL_DEBT_KINDS
                and owner in {actor_role, "*"}
                and target in {objective_ref, "*"}
                and debt.get("currentness") == "CURRENT"
                and debt.get("relevant") is True
                and debt.get("historical") is not True
                and debt.get("resolved") is not True
            )
            if is_relevant:
                debt_id = str(debt.get("debt_id") or "").strip()
                _require(debt_id, "operational-pulse-relevant-debt-id-required")
                relevant.append(copy.deepcopy(debt))

    same_watermark = prior == watermarks
    delta = "NONE" if same_watermark and not relevant else "DELTA"
    result = "DRAIN_REQUIRED" if relevant else ("NONE" if delta == "NONE" else "PASS")
    directive = {
        "effect": "REFRESH_GOVERNING_REFS",
        "stage": stage,
        "watermarks": copy.deepcopy(watermarks),
        "delta": delta,
        "debt_result": result,
        "unresolved_debt_ids": [item["debt_id"] for item in relevant],
    }
    return {
        "schema": OPERATIONAL_PULSE_SCHEMA,
        "stage": stage,
        "result": result,
        "currentness": "CURRENT",
        "delta": delta,
        "terminal_blocked": bool(relevant) and stage == "PRE_CLOSE",
        "watermarks": watermarks,
        "relevant_debt": relevant,
        "persistence_directive": directive,
    }


def _validate_persistence_verification(
    *,
    owner: str,
    receipt: dict[str, Any],
    verification: Any,
) -> None:
    _require(isinstance(verification, dict), f"owner-execution-verification-object-required:{owner}")
    expected = {
        "schema": PERSISTENCE_VERIFICATION_SCHEMA,
        "result": "PASS",
        "owner": owner,
        "owner_effect_receipt_ref": receipt.get("receipt_ref"),
        "owner_effect_receipt_fingerprint": receipt.get("receipt_fingerprint"),
        "persistence_evidence_ref": receipt.get("persistence_evidence_ref"),
        "output_state_ref": receipt.get("output_state_ref"),
        "output_state_fingerprint": receipt.get("output_state_fingerprint"),
    }
    for field, value in expected.items():
        _require(
            verification.get(field) == value,
            f"owner-execution-verification-{field}-mismatch:{owner}",
        )
    _require(
        isinstance(verification.get("verifier_ref"), str)
        and bool(verification["verifier_ref"].strip()),
        f"owner-execution-verifier-ref-required:{owner}",
    )


class BoundRuntimeCapabilityResolver:
    """Capability truth derived only from constructor-bound runtime executors."""

    def __init__(
        self,
        *,
        executors: Mapping[str, Any],
        enabled_owners: set[str] | None = None,
    ):
        unknown = set(executors).difference(OWNER_ORDER)
        _require(not unknown, "runtime-capability-unknown-owner:" + ",".join(sorted(unknown)))
        for owner, executor in executors.items():
            _require(callable(getattr(executor, "execute", None)), f"runtime-owner-executor-invalid:{owner}")
        enabled = set(executors) if enabled_owners is None else set(enabled_owners)
        _require(enabled.issubset(executors), "enabled-runtime-owner-missing-executor")
        self._executors = dict(executors)
        self._enabled = enabled

    def is_available(self, *, owner: str, effect: str) -> bool:
        return (
            owner in self._enabled
            and owner in self._executors
            and EXPECTED_EFFECT.get(owner) == effect
            and callable(getattr(self._executors[owner], "execute", None))
        )

    def executor(self, *, owner: str, effect: str) -> Any:
        _require(self.is_available(owner=owner, effect=effect), f"runtime-owner-capability-unavailable:{owner}")
        return self._executors[owner]


class CompositeOwnerPersistenceVerifier:
    """Route each receipt to a trusted, constructor-bound owner verifier."""

    def __init__(self, *, verifiers: Mapping[str, Any]):
        unknown = set(verifiers).difference(OWNER_ORDER)
        _require(not unknown, "owner-persistence-verifier-unknown-owner:" + ",".join(sorted(unknown)))
        for owner, verifier in verifiers.items():
            _require(callable(getattr(verifier, "verify", None)), f"owner-persistence-verifier-invalid:{owner}")
        self._verifiers = dict(verifiers)

    def verify(self, *, receipt: dict[str, Any]) -> dict[str, Any]:
        _require(isinstance(receipt, dict), "owner-persistence-receipt-object-required")
        owner = receipt.get("owner")
        _require(owner in self._verifiers, f"owner-persistence-verifier-unbound:{owner}")
        return self._verifiers[owner].verify(receipt=copy.deepcopy(receipt))


class BoundControlResolutionHost:
    """Canonical resolver wrapper and ordered normal owner-effect consumer."""

    def __init__(
        self,
        *,
        persistence_verifier: Any,
        capability_resolver: BoundRuntimeCapabilityResolver,
        canonical_resolver: Callable[..., dict[str, Any]] = control_resolution.resolve,
        pm_profile_verifier: Any | None = None,
        pm_disposition_publisher: Any | None = None,
    ):
        _require(callable(getattr(persistence_verifier, "verify", None)), "host-persistence-verifier-required")
        _require(callable(getattr(capability_resolver, "is_available", None)), "host-capability-resolver-required")
        _require(callable(getattr(capability_resolver, "executor", None)), "host-capability-executor-binding-required")
        _require(callable(canonical_resolver), "canonical-control-resolver-required")
        if pm_profile_verifier is not None:
            _require(callable(getattr(pm_profile_verifier, "verify", None)), "host-pm-profile-verifier-invalid")
            _require(callable(getattr(pm_profile_verifier, "verify_lifecycle_effect", None)), "host-lifecycle-effect-verifier-invalid")
        if pm_disposition_publisher is not None:
            _require(
                callable(getattr(pm_disposition_publisher, "publish", None)),
                "host-pm-disposition-publisher-invalid",
            )
        self._persistence_verifier = persistence_verifier
        self._capability_resolver = capability_resolver
        self._canonical_resolver = canonical_resolver
        self._pm_profile_verifier = pm_profile_verifier
        self._pm_disposition_publisher = pm_disposition_publisher

    def publish_pm_durable_disposition(self, proposal: dict[str, Any]) -> dict[str, Any]:
        _require(
            self._pm_disposition_publisher is not None,
            "normal-pm-durable-disposition-publisher-unbound",
        )
        result = self._pm_disposition_publisher.publish(copy.deepcopy(proposal))
        _require(
            isinstance(result, dict),
            "normal-pm-durable-disposition-publication-result-required",
        )
        return result

    def resolve(
        self,
        request: dict[str, Any],
        *,
        root: Path = control_resolution.SOURCE_ROOT,
        require_git_ancestry: bool = True,
    ) -> dict[str, Any]:
        _require(isinstance(request, dict), "host-control-request-object-required")
        _reject_runtime_authority_injection(request)
        return self._canonical_resolver(
            copy.deepcopy(request),
            root=root,
            require_git_ancestry=require_git_ancestry,
            owner_persistence_verifier=self._persistence_verifier,
            runtime_capability_resolver=self._capability_resolver,
            pm_profile_verifier=self._pm_profile_verifier,
        )

    def execute_owner_sequence(
        self,
        *,
        decision: dict[str, Any],
        consolidation_result: dict[str, Any],
        execution_inputs: Mapping[str, dict[str, Any]],
        initial_owner_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute required owners serially and re-resolve after every commit."""

        _require(isinstance(execution_inputs, Mapping), "owner-execution-inputs-mapping-required")
        unknown_inputs = set(execution_inputs).difference(OWNER_ORDER)
        _require(not unknown_inputs, "owner-execution-input-unknown-owner:" + ",".join(sorted(unknown_inputs)))
        for owner, value in execution_inputs.items():
            _require(isinstance(value, dict), f"owner-execution-input-object-required:{owner}")
            _reject_runtime_authority_injection(value, f"execution_inputs.{owner}")
        owner_state = copy.deepcopy(initial_owner_state) if isinstance(initial_owner_state, dict) else {}
        completions: list[dict[str, Any]] = []
        executed: set[str] = set()
        for _ in range(len(OWNER_ORDER) + 1):
            plan = control_owner_routing.build_owner_effect_plan(
                decision,
                consolidation_result,
                owner_state,
                persistence_evidence_verifier=self._persistence_verifier,
                capability_resolver=self._capability_resolver,
            )
            first_incomplete = next(
                (step for step in plan["ordered_owner_steps"] if step["status"] != "SATISFIED"),
                None,
            )
            if first_incomplete is None:
                return {
                    "schema": "cerebro-bound-owner-effect-sequence/v1",
                    "result": "PASS",
                    "normal_consumer_exercised": True,
                    "automatic_cross_owner_transaction": False,
                    "owner_state": owner_state,
                    "completions": completions,
                    "final_plan": plan,
                }
            owner = first_incomplete["owner"]
            effect = first_incomplete["effect"]
            if not self._capability_resolver.is_available(owner=owner, effect=effect):
                return {
                    "schema": "cerebro-bound-owner-effect-sequence/v1",
                    "result": "PENDING_CAPABILITY",
                    "normal_consumer_exercised": True,
                    "automatic_cross_owner_transaction": False,
                    "blocked_owner": owner,
                    "owner_state": owner_state,
                    "completions": completions,
                    "final_plan": plan,
                }
            _require(owner not in executed, f"owner-sequence-repeat-before-satisfaction:{owner}")
            prior_receipt_refs = [
                step["receipt_ref"]
                for step in plan["ordered_owner_steps"]
                if step["status"] == "SATISFIED" and isinstance(step.get("receipt_ref"), str)
            ]
            executor = self._capability_resolver.executor(owner=owner, effect=effect)
            completion = executor.execute(
                owner_effect=copy.deepcopy(plan["owner_effects"][owner]),
                control_decision=copy.deepcopy(decision),
                consolidation_result=copy.deepcopy(consolidation_result),
                prerequisite_receipt_refs=prior_receipt_refs,
                execution_input=copy.deepcopy(execution_inputs.get(owner, {})),
            )
            _require(isinstance(completion, dict), f"owner-execution-completion-object-required:{owner}")
            _require(completion.get("result") == "PASS", f"owner-execution-completion-PASS-required:{owner}")
            _require(completion.get("owner") == owner, f"owner-execution-completion-owner-mismatch:{owner}")
            receipt = completion.get("receipt")
            _require(isinstance(receipt, dict), f"owner-execution-current-receipt-required:{owner}")
            verification = self._persistence_verifier.verify(receipt=copy.deepcopy(receipt))
            _validate_persistence_verification(
                owner=owner,
                receipt=receipt,
                verification=verification,
            )
            _require(
                set(prior_receipt_refs).issubset(set(receipt.get("evidence_refs", []))),
                f"owner-execution-prerequisite-evidence-missing:{owner}",
            )
            owner_state[owner] = {"receipt": copy.deepcopy(receipt)}
            completions.append(copy.deepcopy(completion))
            executed.add(owner)
        raise ControlResolutionHostError("owner-sequence-did-not-converge")



def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class BoundPmDispositionPublisher:
    """Default-off A7 guard around one authoritative PM disposition publisher.

    The caller supplies only the requested disposition identity and expected
    referent revision.  All A7/currentness/Way Home/blocker/safety/parallel-lane
    evidence comes from a constructor-bound owner reader.  This adapter grants
    no PM Sheet or provider authority by itself.  A missing publisher port is a
    typed live cut, not a local-code failure.
    """

    _REQUEST_KEYS = frozenset({
        "schema",
        "disposition_ref",
        "requested_disposition",
        "expected_referent_ref",
        "expected_referent_revision",
        "requested_scope_ref",
    })
    _BASIS_RELATIONS = frozenset({"SAME_AS_LAST_PROVEN_WAY_HOME", "CHANGED"})
    _BLOCKER_NECESSITY = frozenset({"OBSERVED_REQUIRED", "ASSUMED", "UNKNOWN"})

    def __init__(
        self,
        *,
        basis_reader: Any,
        publisher_port: Any | None = None,
        enabled: bool = False,
    ):
        _require(
            callable(getattr(basis_reader, "read_prepublication_basis", None)),
            "a7-prepublication-basis-reader-required",
        )
        if publisher_port is not None:
            _require(
                callable(getattr(publisher_port, "publish_and_readback", None)),
                "pm-guarded-publisher-port-invalid",
            )
        self._basis_reader = basis_reader
        self._publisher_port = publisher_port
        self._enabled = enabled

    @staticmethod
    def _ref(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _hex64(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value)
        )

    def _proposal(self, proposal: Any) -> dict[str, Any]:
        _require(self._enabled, "a7-prepublication-guard-default-off")
        _require(isinstance(proposal, dict), "pm-disposition-request-object-required")
        extra = sorted(set(proposal) - self._REQUEST_KEYS)
        _require(
            not extra,
            "caller-prepublication-evidence-prohibited:" + ",".join(extra),
        )
        _require(
            proposal.get("schema") == PM_DISPOSITION_REQUEST_SCHEMA,
            "pm-disposition-request-schema-mismatch",
        )
        disposition_ref = str(proposal.get("disposition_ref") or "").strip()
        requested = str(proposal.get("requested_disposition") or "").strip().upper()
        referent_ref = str(proposal.get("expected_referent_ref") or "").strip()
        referent_revision = proposal.get("expected_referent_revision")
        scope_ref = str(proposal.get("requested_scope_ref") or "").strip()
        _require(disposition_ref, "pm-disposition-ref-required")
        _require(
            requested in A7_FLOW_REDUCING_DISPOSITIONS,
            "pm-flow-reducing-disposition-required",
        )
        _require(referent_ref, "pm-disposition-referent-ref-required")
        _require(
            type(referent_revision) is int and referent_revision >= 0,
            "pm-disposition-referent-revision-invalid",
        )
        _require(scope_ref, "pm-disposition-scope-ref-required")
        return {
            "schema": PM_DISPOSITION_REQUEST_SCHEMA,
            "disposition_ref": disposition_ref,
            "requested_disposition": requested,
            "expected_referent_ref": referent_ref,
            "expected_referent_revision": referent_revision,
            "requested_scope_ref": scope_ref,
        }

    def _read_basis(self, proposal: dict[str, Any]) -> dict[str, Any]:
        try:
            basis = self._basis_reader.read_prepublication_basis(
                disposition_ref=proposal["disposition_ref"],
                expected_referent_ref=proposal["expected_referent_ref"],
                expected_referent_revision=proposal["expected_referent_revision"],
            )
        except Exception as exc:
            raise ControlResolutionHostError(
                f"a7-prepublication-basis-read-failed:{exc}"
            ) from exc
        return self._validate_basis(proposal, basis)

    def _validate_basis(self, proposal: dict[str, Any], basis: Any) -> dict[str, Any]:
        _require(isinstance(basis, dict), "a7-prepublication-basis-object-required")
        _require(
            basis.get("schema") == A7_PREPUBLICATION_BASIS_SCHEMA,
            "a7-prepublication-basis-schema-mismatch",
        )
        _require(
            basis.get("disposition_ref") == proposal["disposition_ref"],
            "a7-prepublication-disposition-ref-mismatch",
        )
        _require(
            basis.get("referent_ref") == proposal["expected_referent_ref"]
            and basis.get("referent_revision") == proposal["expected_referent_revision"],
            "a7-prepublication-referent-currentness-mismatch",
        )
        _require(
            self._ref(basis.get("referent_status")),
            "a7-prepublication-referent-status-required",
        )
        _require(
            basis.get("currentness") == "CURRENT"
            and basis.get("readback_verified") is True
            and basis.get("revoked") is False,
            "a7-prepublication-owner-basis-not-current",
        )
        _require(
            self._ref(basis.get("owner_ref"))
            and type(basis.get("owner_revision")) is int
            and basis["owner_revision"] >= 0
            and self._ref(basis.get("basis_ref"))
            and self._hex64(basis.get("basis_fingerprint"))
            and self._ref(basis.get("readback_ref")),
            "a7-prepublication-owner-basis-identity-invalid",
        )
        _require(
            type(basis.get("publication_revision")) is int
            and basis["publication_revision"] >= 0,
            "a7-prepublication-publication-revision-invalid",
        )
        _require(
            self._ref(basis.get("scope_ref")),
            "a7-prepublication-scope-ref-required",
        )
        relation = basis.get("basis_relation")
        _require(
            relation in self._BASIS_RELATIONS,
            "a7-prepublication-basis-relation-invalid",
        )

        cases = basis.get("relevant_a7_cases")
        _require(
            isinstance(cases, list) and bool(cases),
            "a7-prepublication-relevant-case-required",
        )
        case_refs: list[str] = []
        for index, item in enumerate(cases):
            _require(
                isinstance(item, dict),
                f"a7-prepublication-case-object-required:{index}",
            )
            case_ref = str(item.get("case_ref") or "").strip()
            _require(
                case_ref in A7_RELEVANT_CASE_REFS and case_ref not in case_refs,
                f"a7-prepublication-case-ref-invalid:{index}",
            )
            _require(
                item.get("currentness") == "CURRENT"
                and item.get("readback_verified") is True
                and item.get("revoked") is False
                and type(item.get("revision")) is int
                and item["revision"] >= 0
                and self._ref(item.get("lesson_basis_ref")),
                f"a7-prepublication-case-currentness-invalid:{case_ref}",
            )
            case_refs.append(case_ref)

        way_home = basis.get("last_proven_way_home")
        _require(
            isinstance(way_home, dict)
            and self._ref(way_home.get("way_home_ref"))
            and type(way_home.get("revision")) is int
            and way_home["revision"] >= 0
            and way_home.get("readback_verified") is True,
            "a7-prepublication-way-home-readback-required",
        )
        blocker = basis.get("blocker")
        _require(
            isinstance(blocker, dict)
            and blocker.get("necessity") in self._BLOCKER_NECESSITY,
            "a7-prepublication-blocker-necessity-invalid",
        )
        if blocker.get("necessity") == "OBSERVED_REQUIRED":
            _require(
                self._ref(blocker.get("blocker_ref"))
                and blocker.get("readback_verified") is True,
                "a7-prepublication-observed-blocker-readback-required",
            )

        reentry = basis.get("smallest_lawful_reentry")
        _require(
            isinstance(reentry, dict)
            and self._ref(reentry.get("owner_ref"))
            and self._ref(reentry.get("reentry_ref")),
            "a7-prepublication-reentry-required",
        )

        lanes = basis.get("parallel_lanes")
        _require(isinstance(lanes, list), "a7-prepublication-parallel-lanes-array-required")
        lane_refs: list[str] = []
        for index, lane in enumerate(lanes):
            _require(
                isinstance(lane, dict)
                and self._ref(lane.get("lane_ref"))
                and lane.get("currentness") == "CURRENT"
                and lane.get("readback_verified") is True
                and lane.get("independent") is True,
                f"a7-prepublication-parallel-lane-invalid:{index}",
            )
            lane_ref = lane["lane_ref"].strip()
            _require(
                lane_ref not in lane_refs,
                f"a7-prepublication-parallel-lane-duplicate:{lane_ref}",
            )
            lane_refs.append(lane_ref)

        active_hold = basis.get("active_hold")
        hold_like = proposal["requested_disposition"] in {"HOLD", "WAIT", "UNBOUND", "OMIT_BIND"}
        if hold_like and blocker.get("necessity") == "OBSERVED_REQUIRED":
            _require(isinstance(active_hold, dict), "a7-active-hold-binding-required")
            for field in (
                "blocked_edge", "first_observed_at", "owner_ref",
                "next_action", "next_check", "escalation_to",
            ):
                _require(
                    self._ref(active_hold.get(field)),
                    f"a7-active-hold-{field}-required",
                )
            _require(
                active_hold["owner_ref"] == basis["owner_ref"],
                "a7-active-hold-existing-mandate-owner-mismatch",
            )
            _require(
                active_hold["escalation_to"] == "P22",
                "a7-active-hold-escalation-must-be-P22",
            )
            _require(
                type(active_hold.get("next_check_due")) is bool
                and type(active_hold.get("orphaned")) is bool,
                "a7-active-hold-due-or-orphan-flags-required",
            )
            previous_first = active_hold.get("previous_first_observed_at")
            if previous_first is not None:
                _require(
                    previous_first == active_hold["first_observed_at"],
                    "a7-active-hold-first-observed-at-must-survive-revision",
                )
            _require(
                "A7-P22-PASSIVE-HOLD-NEGATIVE-010" in case_refs,
                "a7-active-hold-entry010-basis-required",
            )
        elif active_hold is not None:
            _require(isinstance(active_hold, dict), "a7-active-hold-object-required")

        safety = basis.get("safety")
        _require(
            isinstance(safety, dict)
            and type(safety.get("acute")) is bool,
            "a7-prepublication-safety-object-required",
        )
        if safety["acute"]:
            _require(
                self._ref(safety.get("evidence_ref"))
                and self._ref(safety.get("scope_ref"))
                and safety.get("readback_verified") is True,
                "a7-prepublication-acute-safety-readback-required",
            )

        value = copy.deepcopy(basis)
        value["relevant_case_refs"] = case_refs
        value["parallel_lane_refs"] = lane_refs
        return value

    @staticmethod
    def _decision_ref(subject: dict[str, Any]) -> tuple[str, str]:
        fingerprint = _canonical_sha256(subject)
        return "A7PRE-" + fingerprint[:20].upper(), fingerprint

    def evaluate(self, proposal: dict[str, Any]) -> dict[str, Any]:
        request = self._proposal(proposal)
        basis = self._read_basis(request)
        safety = basis["safety"]
        blocker = basis["blocker"]
        active_hold = basis.get("active_hold")
        lane_refs = list(basis["parallel_lane_refs"])
        requested_scope = request["requested_scope_ref"]
        trusted_scope = basis["scope_ref"]
        global_request = (
            request["requested_disposition"] == "LOCAL_TO_GLOBAL_STOP"
            or requested_scope.upper() == "GLOBAL"
        )

        if safety["acute"]:
            if requested_scope != safety["scope_ref"]:
                decision = "REJECT_UNSCOPED_SAFETY_STOP"
                publication_kind = "A7_CORRECTION"
                effective = "CONTINUE_REENTRY"
                publication_scope = trusted_scope
                reentry_required = True
                safety_effect_may_precede_publication = False
            else:
                decision = "ALLOW_SCOPED_SAFETY_STOP"
                publication_kind = "SCOPED_SAFETY_STOP"
                effective = request["requested_disposition"]
                publication_scope = safety["scope_ref"]
                reentry_required = True
                safety_effect_may_precede_publication = True
        elif (
            isinstance(active_hold, dict)
            and basis["basis_relation"] == "SAME_AS_LAST_PROVEN_WAY_HOME"
            and (active_hold.get("next_check_due") is True or active_hold.get("orphaned") is True)
        ):
            decision = "ESCALATE_DUE_OR_ORPHAN_ACTIVE_HOLD"
            publication_kind = "A7_HOLD_ACTION"
            effective = "CONTINUE_REENTRY"
            publication_scope = trusted_scope
            reentry_required = True
            safety_effect_may_precede_publication = False
        elif global_request:
            decision = "CORRECT_GLOBAL_STOP_PRESERVE_INDEPENDENT_LANES"
            publication_kind = "A7_CORRECTION"
            effective = "CONTINUE_REENTRY"
            publication_scope = trusted_scope
            reentry_required = True
            safety_effect_may_precede_publication = False
        elif (
            basis["basis_relation"] == "SAME_AS_LAST_PROVEN_WAY_HOME"
            and blocker["necessity"] != "OBSERVED_REQUIRED"
        ):
            decision = "CORRECT_FALSE_SAME_BASIS_STOP"
            publication_kind = "A7_CORRECTION"
            effective = "CONTINUE_REENTRY"
            publication_scope = trusted_scope
            reentry_required = True
            safety_effect_may_precede_publication = False
        elif blocker["necessity"] == "OBSERVED_REQUIRED":
            _require(
                requested_scope == trusted_scope,
                "a7-prepublication-flow-reduction-scope-mismatch",
            )
            decision = "ALLOW_SCOPED_FLOW_REDUCTION"
            publication_kind = "FLOW_REDUCTION"
            effective = request["requested_disposition"]
            publication_scope = trusted_scope
            reentry_required = True
            safety_effect_may_precede_publication = False
        else:
            decision = "REJECT_UNPROVEN_FLOW_REDUCTION"
            publication_kind = "A7_CORRECTION"
            effective = "CONTINUE_REENTRY"
            publication_scope = trusted_scope
            reentry_required = True
            safety_effect_may_precede_publication = False

        subject = {
            "schema": A7_PREPUBLICATION_DECISION_SCHEMA,
            "decision": decision,
            "requested_disposition": request["requested_disposition"],
            "effective_disposition": effective,
            "publication_kind": publication_kind,
            "disposition_ref": request["disposition_ref"],
            "referent_ref": basis["referent_ref"],
            "referent_revision": basis["referent_revision"],
            "referent_status": basis["referent_status"],
            "publication_scope_ref": publication_scope,
            "basis_ref": basis["basis_ref"],
            "basis_owner_ref": basis["owner_ref"],
            "basis_owner_revision": basis["owner_revision"],
            "basis_fingerprint": basis["basis_fingerprint"],
            "basis_relation": basis["basis_relation"],
            "a7_case_refs": list(basis["relevant_case_refs"]),
            "last_way_home_ref": basis["last_proven_way_home"]["way_home_ref"],
            "last_way_home_revision": basis["last_proven_way_home"]["revision"],
            "blocker_necessity": blocker["necessity"],
            "blocker_ref": blocker.get("blocker_ref"),
            "blocked_edge": active_hold.get("blocked_edge") if isinstance(active_hold, dict) else None,
            "first_observed_at": active_hold.get("first_observed_at") if isinstance(active_hold, dict) else None,
            "hold_owner_ref": active_hold.get("owner_ref") if isinstance(active_hold, dict) else None,
            "hold_next_action": active_hold.get("next_action") if isinstance(active_hold, dict) else None,
            "hold_next_check": active_hold.get("next_check") if isinstance(active_hold, dict) else None,
            "hold_escalation_to": active_hold.get("escalation_to") if isinstance(active_hold, dict) else None,
            "hold_next_check_due": active_hold.get("next_check_due") if isinstance(active_hold, dict) else False,
            "hold_orphaned": active_hold.get("orphaned") if isinstance(active_hold, dict) else False,
            "smallest_reentry_owner_ref": basis["smallest_lawful_reentry"]["owner_ref"],
            "smallest_reentry_ref": basis["smallest_lawful_reentry"]["reentry_ref"],
            "preserve_lane_refs": lane_refs,
            "acute_safety": safety["acute"],
            "safety_evidence_ref": safety.get("evidence_ref"),
            "reentry_required": reentry_required,
            "safety_effect_may_precede_publication": safety_effect_may_precede_publication,
            "publication_previous_revision": basis["publication_revision"],
            "authority": "NONE",
            "publisher_required": True,
        }
        decision_ref, fingerprint = self._decision_ref(subject)
        subject["decision_ref"] = decision_ref
        subject["decision_fingerprint"] = fingerprint
        subject["_basis_snapshot_fingerprint"] = _canonical_sha256(basis)
        return subject

    def publish(self, proposal: dict[str, Any]) -> dict[str, Any]:
        decision = self.evaluate(proposal)
        if self._publisher_port is None:
            public_decision = {
                key: copy.deepcopy(value)
                for key, value in decision.items()
                if not key.startswith("_")
            }
            return {
                "schema": PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA,
                "result": "BLOCK_EXACT_PUBLISHER_PORT",
                "published": False,
                "live_effect": False,
                "retry_allowed": False,
                "first_unproven_live_edge": (
                    "PM_PRINCIPAL_CHANNEL_GUARDED_PUBLISH_AND_READBACK_PORT_UNBOUND"
                ),
                "decision_receipt": public_decision,
            }

        request = self._proposal(proposal)
        fresh_basis = self._read_basis(request)
        if _canonical_sha256(fresh_basis) != decision["_basis_snapshot_fingerprint"]:
            return {
                "schema": PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA,
                "result": "HOLD_STALE_PREPUBLICATION_BASIS",
                "published": False,
                "live_effect": False,
                "retry_allowed": False,
                "decision_ref": decision["decision_ref"],
            }

        publication = {
            "schema": PM_GUARDED_PUBLICATION_SCHEMA,
            "decision_ref": decision["decision_ref"],
            "decision_fingerprint": decision["decision_fingerprint"],
            "publication_kind": decision["publication_kind"],
            "effective_disposition": decision["effective_disposition"],
            "requested_disposition": decision["requested_disposition"],
            "referent_ref": decision["referent_ref"],
            "referent_revision": decision["referent_revision"],
            "scope_ref": decision["publication_scope_ref"],
            "previous_revision": decision["publication_previous_revision"],
            "a7_case_refs": list(decision["a7_case_refs"]),
            "preserve_lane_refs": list(decision["preserve_lane_refs"]),
            "reentry_owner_ref": decision["smallest_reentry_owner_ref"],
            "reentry_ref": decision["smallest_reentry_ref"],
            "reentry_required": decision["reentry_required"],
            "acute_safety": decision["acute_safety"],
            "safety_evidence_ref": decision["safety_evidence_ref"],
        }
        publication["publication_fingerprint"] = _canonical_sha256(publication)

        try:
            receipt = self._publisher_port.publish_and_readback(
                publication=copy.deepcopy(publication)
            )
        except Exception:
            return {
                "schema": PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA,
                "result": "PUBLICATION_OUTCOME_UNKNOWN",
                "published": "UNKNOWN",
                "live_effect": "UNKNOWN",
                "retry_allowed": False,
                "decision_ref": decision["decision_ref"],
                "publication_fingerprint": publication["publication_fingerprint"],
            }

        _require(
            isinstance(receipt, dict),
            "pm-guarded-publication-receipt-object-required",
        )
        _require(
            receipt.get("schema") == PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA
            and receipt.get("result") == "COMMITTED"
            and receipt.get("decision_ref") == decision["decision_ref"]
            and receipt.get("decision_fingerprint") == decision["decision_fingerprint"]
            and receipt.get("publication_fingerprint")
                == publication["publication_fingerprint"]
            and receipt.get("referent_ref") == decision["referent_ref"]
            and receipt.get("referent_revision") == decision["referent_revision"]
            and receipt.get("scope_ref") == decision["publication_scope_ref"]
            and receipt.get("effective_disposition") == decision["effective_disposition"],
            "pm-guarded-publication-receipt-binding-mismatch",
        )
        _require(
            receipt.get("previous_revision") == decision["publication_previous_revision"]
            and receipt.get("published_revision")
                == decision["publication_previous_revision"] + 1,
            "pm-guarded-publication-revision-mismatch",
        )
        _require(
            receipt.get("preserve_lane_refs") == decision["preserve_lane_refs"],
            "pm-guarded-publication-parallel-lane-readback-mismatch",
        )
        _require(
            receipt.get("readback_verified") is True
            and type(receipt.get("provider_revision")) is int
            and receipt["provider_revision"] >= 0
            and self._ref(receipt.get("receipt_ref")),
            "pm-guarded-publication-readback-unverified",
        )
        return {
            "schema": PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA,
            "result": "PASS_GUARDED_PUBLICATION_READBACK",
            "published": True,
            "live_effect": True,
            "retry_allowed": False,
            "decision_receipt": {
                key: copy.deepcopy(value)
                for key, value in decision.items()
                if not key.startswith("_")
            },
            "publication": publication,
            "publisher_receipt": copy.deepcopy(receipt),
        }


def _pm_command_hmi(next_machine_action: str, next_owner: str) -> dict[str, str]:
    return {
        "next_machine_action": next_machine_action,
        "next_owner": next_owner,
        "human_action": "NONE",
    }


def _pm_command_exact_blocker(
    command_id: str | None,
    blocker: str,
    *,
    next_owner: str = "PROJECT_MANAGER",
    reflex_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require(isinstance(blocker, str) and bool(blocker.strip()), "pm-command-exact-blocker-required")
    return {
        "schema": PM_AUTHORIZED_COMMAND_CONSUMPTION_SCHEMA,
        "result": "EXACT_BLOCKER",
        "command_id": command_id,
        "command_executed": False,
        "state_delta_observed": False,
        "exact_blocker": blocker,
        "retry_allowed": False,
        "reflex_resolution": copy.deepcopy(reflex_resolution),
        "hmi": _pm_command_hmi("RESOLVE_BLOCKER", next_owner),
    }


def consume_pm_authorized_command(
    host: BoundControlResolutionHost,
    *,
    governance: dict[str, Any],
    command_state: dict[str, Any] | None,
    carrier: dict[str, Any],
    command_executor: Any | None,
    progress_evidence: dict[str, Any] | None = None,
    root: Path = control_resolution.SOURCE_ROOT,
    require_git_ancestry: bool = True,
) -> dict[str, Any]:
    """Consume one already-authorized PM command synchronously; never decide/admit it."""

    _require(isinstance(host, BoundControlResolutionHost), "pm-command-bound-host-required")
    _require(isinstance(governance, dict), "pm-command-governance-object-required")
    next_action = governance.get("next_action")
    _require(isinstance(next_action, dict), "pm-command-next-action-object-required")

    actionable = (
        next_action.get("owner") == "MACHINE"
        and next_action.get("pm_actor") == "PROJECT_MANAGER"
        and next_action.get("internally_executable") is True
        and next_action.get("required_before_event_closure") is True
    )
    if not actionable:
        return {
            "schema": PM_AUTHORIZED_COMMAND_CONSUMPTION_SCHEMA,
            "result": "NO_EFFECT",
            "command_id": None,
            "command_executed": False,
            "state_delta_observed": False,
            "retry_allowed": False,
            "hmi": _pm_command_hmi("NONE", "NONE"),
        }

    if command_state is None:
        return {
            "schema": PM_AUTHORIZED_COMMAND_CONSUMPTION_SCHEMA,
            "result": "NO_EFFECT",
            "command_id": None,
            "command_executed": False,
            "state_delta_observed": False,
            "retry_allowed": False,
            "hmi": _pm_command_hmi("NONE", "PROJECT_MANAGER"),
        }

    _require(isinstance(command_state, dict), "pm-command-state-object-required")
    _reject_runtime_authority_injection(command_state, "pm_command_state")
    _require(
        command_state.get("schema") == PM_AUTHORIZED_COMMAND_STATE_SCHEMA,
        "pm-command-state-schema-mismatch",
    )
    _require(
        command_state.get("authority") == "PROJECT_MANAGER",
        "pm-command-authority-must-be-PROJECT_MANAGER",
    )
    command_id = str(command_state.get("command_id") or "").strip()
    action_ref = str(next_action.get("action_ref") or "").strip()
    _require(command_id, "pm-command-id-required")
    _require(action_ref, "pm-command-action-ref-required")
    _require(command_state.get("action_ref") == action_ref, "pm-command-action-ref-mismatch")
    source_head = str(command_state.get("source_head") or "").strip()
    _require(
        len(source_head) == 40 and all(ch in "0123456789abcdef" for ch in source_head),
        "pm-command-source-head-invalid",
    )
    canonical_state_ref = str(command_state.get("canonical_state_ref") or "").strip()
    canonical_revision = command_state.get("canonical_state_revision")
    _require(canonical_state_ref, "pm-command-canonical-state-ref-required")
    _require(
        isinstance(canonical_revision, int) and canonical_revision >= 0,
        "pm-command-canonical-state-revision-invalid",
    )
    _require(isinstance(command_state.get("payload"), dict), "pm-command-payload-object-required")
    precondition = command_state.get("precondition")
    _require(isinstance(precondition, dict), "pm-command-precondition-object-required")
    pre_state_ref = str(precondition.get("state_ref") or "").strip()
    pre_fingerprint = str(precondition.get("state_fingerprint") or "").strip()
    _require(pre_state_ref, "pm-command-precondition-state-ref-required")
    _require(
        len(pre_fingerprint) == 64 and all(ch in "0123456789abcdef" for ch in pre_fingerprint),
        "pm-command-precondition-fingerprint-invalid",
    )

    _require(isinstance(carrier, dict), "pm-command-carrier-object-required")
    _reject_runtime_authority_injection(carrier, "pm_command_carrier")
    carrier_ref = str(carrier.get("carrier_ref") or "").strip()
    _require(carrier_ref, "pm-command-carrier-ref-required")
    _require(carrier.get("identity_verified") is True, "pm-command-carrier-identity-required")
    _require(carrier.get("currentness_verified") is True, "pm-command-carrier-currentness-required")
    _require(carrier.get("source_head") == source_head, "pm-command-carrier-source-mismatch")
    _require(
        carrier.get("canonical_state_ref") == canonical_state_ref,
        "pm-command-carrier-state-ref-mismatch",
    )
    _require(
        carrier.get("canonical_state_revision") == canonical_revision,
        "pm-command-carrier-state-revision-mismatch",
    )

    if isinstance(progress_evidence, dict):
        _reject_runtime_authority_injection(progress_evidence, "pm_command_progress")
        reflex_trigger = (
            progress_evidence.get("repeated_same_family") is True
            and progress_evidence.get("no_state_advance") is True
            and progress_evidence.get("material_human_recontact") is True
            and progress_evidence.get("elapsed_time_only") is not True
        )
        if reflex_trigger:
            fallback_state = str(progress_evidence.get("fallback_state") or "UNKNOWN").upper()
            if fallback_state in {"STALE", "UNKNOWN"}:
                return _pm_command_exact_blocker(
                    command_id,
                    f"FALLBACK_CURRENTNESS_UNRESOLVED:{fallback_state}",
                )
            if fallback_state == "AVAILABLE":
                _require(progress_evidence.get("fallback_proven") is True, "pm-command-proven-fallback-required")
                _require(
                    isinstance(progress_evidence.get("fallback_ref"), str)
                    and bool(progress_evidence["fallback_ref"].strip()),
                    "pm-command-fallback-ref-required",
                )
            elif fallback_state in {"NONE_PROVEN", "UNAVAILABLE"}:
                semantic_owner_ref = str(progress_evidence.get("semantic_owner_ref") or "").strip()
                _require(
                    semantic_owner_ref
                    and semantic_owner_ref.upper() not in {"HOST", "PROJECT_MANAGER", "PM"},
                    "pm-command-lawful-semantic-owner-required",
                )
            else:
                _require(
                    fallback_state in {"AVAILABLE", "NONE_PROVEN", "UNAVAILABLE"},
                    "pm-command-fallback-state-invalid",
                )

            reorientation_request = progress_evidence.get("reorientation_request")
            _require(isinstance(reorientation_request, dict), "pm-command-reorientation-request-required")
            reorientation_request = copy.deepcopy(reorientation_request)
            reorientation_request["materially_different_path_required"] = True
            reorientation_request.setdefault("authoritative_source_commit", source_head)
            routed = host.resolve(
                reorientation_request,
                root=root,
                require_git_ancestry=require_git_ancestry,
            )
            decision = routed.get("mcp_control_decision") if isinstance(routed, dict) else None
            _require(isinstance(decision, dict), "pm-command-canonical-reorientation-decision-required")
            outcome = str(decision.get("outcome") or "").upper()
            if outcome != "REORIENT":
                return _pm_command_exact_blocker(
                    command_id,
                    "NO_PROGRESS_REORIENTATION_" + (outcome or "UNRESOLVED"),
                    reflex_resolution=routed,
                )
            return {
                "schema": PM_AUTHORIZED_COMMAND_CONSUMPTION_SCHEMA,
                "result": "REORIENTED_BEFORE_IDENTICAL_RETRY",
                "command_id": command_id,
                "command_executed": False,
                "state_delta_observed": True,
                "state_delta": {
                    "kind": "CONTROL_ROUTE",
                    "from_action_ref": action_ref,
                    "to_control_decision_ref": decision.get("control_decision_id"),
                },
                "reflex_resolution": copy.deepcopy(routed),
                "retry_allowed": False,
                "hmi": _pm_command_hmi("CONSUME_REORIENTED_PM_COMMAND", "PROJECT_MANAGER"),
            }

    is_available = getattr(command_executor, "is_available", None)
    execute = getattr(command_executor, "execute", None)
    if not callable(is_available) or not callable(execute):
        return _pm_command_exact_blocker(
            command_id,
            f"CARRIER_COMMAND_EXECUTOR_UNAVAILABLE:{carrier_ref}:{action_ref}",
            next_owner=str(carrier.get("capable_carrier_ref") or "PROJECT_MANAGER"),
        )
    if is_available(command=copy.deepcopy(command_state), carrier=copy.deepcopy(carrier)) is not True:
        return _pm_command_exact_blocker(
            command_id,
            f"CARRIER_COMMAND_EXECUTOR_UNAVAILABLE:{carrier_ref}:{action_ref}",
            next_owner=str(carrier.get("capable_carrier_ref") or "PROJECT_MANAGER"),
        )

    completion = execute(
        command=copy.deepcopy(command_state),
        carrier=copy.deepcopy(carrier),
    )
    _require(isinstance(completion, dict), "pm-command-execution-completion-object-required")
    if completion.get("result") == "STALE_PRECONDITION":
        blocker = str(completion.get("exact_blocker") or "STALE_PRECONDITION").strip()
        return _pm_command_exact_blocker(command_id, blocker)

    _require(completion.get("result") == "PASS", "pm-command-execution-PASS-required")
    _require(completion.get("command_id") == command_id, "pm-command-execution-command-id-mismatch")
    _require(completion.get("action_ref") == action_ref, "pm-command-execution-action-ref-mismatch")
    _require(
        completion.get("precondition_fingerprint") == pre_fingerprint,
        "pm-command-execution-precondition-fingerprint-mismatch",
    )
    state_delta = completion.get("state_delta")
    readback = completion.get("readback")
    _require(isinstance(state_delta, dict), "pm-command-state-delta-object-required")
    _require(isinstance(readback, dict), "pm-command-readback-object-required")
    _require(state_delta.get("before_state_ref") == pre_state_ref, "pm-command-before-state-ref-mismatch")
    _require(
        state_delta.get("before_state_fingerprint") == pre_fingerprint,
        "pm-command-before-state-fingerprint-mismatch",
    )
    _require(state_delta.get("mutated") is True, "pm-command-state-delta-mutation-required")
    _require(
        state_delta.get("after_state_ref") == readback.get("state_ref"),
        "pm-command-after-state-ref-readback-mismatch",
    )
    _require(
        state_delta.get("after_state_fingerprint") == readback.get("state_fingerprint"),
        "pm-command-after-state-fingerprint-readback-mismatch",
    )
    _require(
        state_delta.get("after_state_fingerprint") != state_delta.get("before_state_fingerprint"),
        "pm-command-state-delta-required",
    )
    _require(readback.get("verified") is True, "pm-command-readback-verification-required")
    _require(
        isinstance(readback.get("provider_revision"), int) and readback["provider_revision"] >= 0,
        "pm-command-readback-provider-revision-invalid",
    )
    return {
        "schema": PM_AUTHORIZED_COMMAND_CONSUMPTION_SCHEMA,
        "result": "PASS_STATE_DELTA_READBACK",
        "command_id": command_id,
        "command_executed": True,
        "state_delta_observed": True,
        "state_delta": copy.deepcopy(state_delta),
        "readback": copy.deepcopy(readback),
        "retry_allowed": False,
        "hmi": _pm_command_hmi("RERESOLVE_CONTROL", "PROJECT_MANAGER"),
    }


def consume_pm_authorized_command_chain(
    host: BoundControlResolutionHost,
    *,
    governance: dict[str, Any],
    command_state: dict[str, Any] | None,
    carrier: dict[str, Any],
    command_executor: Any | None,
    current_state_reader: Any,
    max_steps: int = 8,
    durable_disposition_request: dict[str, Any] | None = None,
    root: Path = control_resolution.SOURCE_ROOT,
    require_git_ancestry: bool = True,
) -> dict[str, Any]:
    """Consume the bounded PM-owned command frontier to a declared fixed point.

    The reader is a constructor-bound host dependency.  After every verified
    mutation it must return fresh canonical state; payloads cannot manufacture
    currentness, authority, or the next command.
    """

    _require(isinstance(max_steps, int) and 1 <= max_steps <= 32, "pm-command-chain-bound-invalid")
    # Retained as a source-compatible argument only. Caller-authored proposals
    # are never trusted; the pre-stop proposal is derived below from canonical
    # governance, command/carrier, and current-state readback fields.
    read_current = getattr(current_state_reader, "read_current", None)
    _require(callable(read_current), "pm-command-chain-current-state-reader-required")
    current_governance = copy.deepcopy(governance)
    current_command = copy.deepcopy(command_state)
    current_carrier = copy.deepcopy(carrier)
    last_fresh_state: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = []
    observed_frontiers: set[tuple[Any, ...]] = set()

    def default_disposition_request() -> tuple[dict[str, Any] | None, str | None]:
        """Build the A7 proposal only from canonical chain/readback state."""
        if not isinstance(last_fresh_state, dict):
            return None, "A7_PRESTOP_CURRENT_STATE_READER_NOT_EXERCISED"
        if (
            str(last_fresh_state.get("currentness") or "UNKNOWN").upper() != "CURRENT"
            or last_fresh_state.get("provider_readback_verified") is not True
        ):
            return None, "A7_PRESTOP_CURRENT_STATE_READBACK_NOT_CURRENT"
        sources = [
            last_fresh_state,
            current_governance if isinstance(current_governance, dict) else {},
            current_command if isinstance(current_command, dict) else {},
            current_carrier if isinstance(current_carrier, dict) else {},
        ]

        def first_text(*keys: str) -> str | None:
            for source in sources:
                for key in keys:
                    value = source.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return None

        def first_revision(*keys: str) -> int | None:
            for source in sources:
                for key in keys:
                    value = source.get(key)
                    if type(value) is int and value >= 0:
                        return value
            return None

        referent_ref = first_text("prepublication_referent_ref", "referent_ref", "canonical_state_ref")
        referent_revision = first_revision(
            "prepublication_referent_revision", "referent_revision", "canonical_state_revision",
        )
        scope_ref = first_text("prepublication_scope_ref", "scope_ref")
        event_or_command_ref = first_text("event_ref", "command_id")
        if not event_or_command_ref:
            return None, "A7_PRESTOP_CANONICAL_EVENT_IDENTITY_UNBOUND"
        if not referent_ref:
            return None, "A7_PRESTOP_CANONICAL_REFERENT_REF_UNBOUND"
        if referent_revision is None:
            return None, "A7_PRESTOP_CANONICAL_REFERENT_REVISION_UNBOUND"
        if not scope_ref:
            return None, "A7_PRESTOP_CANONICAL_SCOPE_REF_UNBOUND"

        identity = {
            "event_or_command_ref": event_or_command_ref,
            "referent_ref": referent_ref,
            "referent_revision": referent_revision,
            "scope_ref": scope_ref,
        }
        proposal_ref = "PM-PRESTOP-" + _canonical_sha256(identity)[:24].upper()
        return ({
            "schema": PM_DISPOSITION_REQUEST_SCHEMA,
            "disposition_ref": proposal_ref,
            "requested_disposition": "HOLD",
            "expected_referent_ref": referent_ref,
            "expected_referent_revision": referent_revision,
            "requested_scope_ref": scope_ref,
        }, None)

    def stop(reason: str, *, currentness: str = "CURRENT", blocker: str | None = None) -> dict[str, Any]:
        _require(reason in PM_FIXED_POINT_STOP_REASONS, "pm-command-chain-stop-reason-invalid")
        value: dict[str, Any] = {
            "schema": PM_COMMAND_CHAIN_SCHEMA,
            "result": "FIXED_POINT",
            "stop_reason": reason,
            "currentness": currentness,
            "steps": copy.deepcopy(steps),
            "step_count": len(steps),
            "human_action": "REQUIRED" if reason == "REAL_HUMAN_GATE" else "NONE",
            "durable_disposition_consumer_exercised": reason != "REAL_HUMAN_GATE",
        }
        if blocker:
            value["exact_blocker"] = blocker
        if reason != "REAL_HUMAN_GATE":
            proposal, proposal_gap = default_disposition_request()
            if proposal_gap:
                value["durable_disposition_publication"] = {
                    "schema": PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA,
                    "result": "HOLD_LOCAL_PRESTOP_BASIS_UNBOUND",
                    "published": False,
                    "live_effect": False,
                    "retry_allowed": False,
                    "first_unproven_live_edge": proposal_gap,
                }
            elif host._pm_disposition_publisher is None:
                value["durable_disposition_publication"] = {
                    "schema": PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA,
                    "result": "BLOCK_EXACT_PUBLISHER_PORT",
                    "published": False,
                    "live_effect": False,
                    "retry_allowed": False,
                    "first_unproven_live_edge":
                        "NORMAL_PM_DURABLE_DISPOSITION_GUARDED_PUBLISHER_UNBOUND",
                }
            else:
                try:
                    value["durable_disposition_publication"] = host.publish_pm_durable_disposition(proposal)
                except Exception as exc:
                    value["durable_disposition_publication"] = {
                        "schema": PM_GUARDED_PUBLICATION_RECEIPT_SCHEMA,
                        "result": "HOLD_LOCAL_PRESTOP_A7_BASIS",
                        "published": "UNKNOWN",
                        "live_effect": "UNKNOWN",
                        "retry_allowed": False,
                        "first_unproven_live_edge": str(exc),
                    }
        return value

    for _ in range(max_steps):
        _require(isinstance(current_governance, dict), "pm-command-chain-governance-object-required")
        next_action = current_governance.get("next_action")
        if not isinstance(next_action, dict) or not next_action.get("action_ref"):
            return stop("QUIESCENT")
        if next_action.get("owner") == "HUMAN":
            return stop("REAL_HUMAN_GATE")
        if next_action.get("owner") != "MACHINE" or next_action.get("pm_actor") != "PROJECT_MANAGER":
            return stop("OTHER_OWNER_WAIT")
        if not (next_action.get("internally_executable") is True and next_action.get("required_before_event_closure") is True):
            return stop("CAPABILITY_MISSING", blocker="PM_INTERNAL_ACTION_NOT_EXECUTABLE")

        command_signature = (
            next_action.get("action_ref"),
            (current_command or {}).get("command_id"),
            (current_command or {}).get("canonical_state_revision"),
            ((current_command or {}).get("precondition") or {}).get("state_fingerprint"),
        )
        if command_signature in observed_frontiers:
            return stop("NONPROGRESS_CYCLE", blocker="REPEATED_PM_COMMAND_FRONTIER")
        observed_frontiers.add(command_signature)

        consumed = consume_pm_authorized_command(
            host,
            governance=current_governance,
            command_state=current_command,
            carrier=current_carrier,
            command_executor=command_executor,
            root=root,
            require_git_ancestry=require_git_ancestry,
        )
        steps.append(copy.deepcopy(consumed))
        if consumed.get("result") == "EXACT_BLOCKER":
            blocker = str(consumed.get("exact_blocker") or "UNKNOWN")
            if "STALE_PRECONDITION" in blocker:
                return stop("CONFLICT", blocker=blocker)
            if "UNAVAILABLE" in blocker:
                return stop("CAPABILITY_MISSING", blocker=blocker)
            return stop("UNKNOWN_HOLD", currentness="UNKNOWN", blocker=blocker)
        if consumed.get("result") == "NO_EFFECT":
            return stop("QUIESCENT")
        _require(
            consumed.get("result") in {"PASS_STATE_DELTA_READBACK", "REORIENTED_BEFORE_IDENTICAL_RETRY"},
            "pm-command-chain-consumption-result-invalid",
        )

        fresh = read_current(previous=copy.deepcopy(consumed))
        _require(isinstance(fresh, dict), "pm-command-chain-fresh-state-object-required")
        _reject_runtime_authority_injection(fresh, "pm_command_chain_fresh_state")
        last_fresh_state = copy.deepcopy(fresh)
        currentness = str(fresh.get("currentness") or "UNKNOWN").upper()
        _require(currentness in {"CURRENT", "STALE", "UNKNOWN"}, "pm-command-chain-currentness-invalid")
        if currentness != "CURRENT":
            return stop("UNKNOWN_HOLD", currentness=currentness, blocker=f"CURRENTNESS_{currentness}")
        if fresh.get("conflict") is True:
            return stop("CONFLICT", blocker=str(fresh.get("exact_blocker") or "CANONICAL_STATE_CONFLICT"))
        if fresh.get("provider_readback_verified") is not True:
            return stop("UNKNOWN_HOLD", currentness="UNKNOWN", blocker="PROVIDER_READBACK_NOT_VERIFIED")
        control_request = fresh.get("control_request")
        _require(isinstance(control_request, dict), "pm-command-chain-control-request-required")
        reresolved = host.resolve(
            control_request,
            root=root,
            require_git_ancestry=require_git_ancestry,
        )
        _require(isinstance(reresolved, dict), "pm-command-chain-canonical-reresolution-required")
        current_governance = copy.deepcopy(reresolved.get("project_manager_control_governance"))
        if not isinstance(current_governance, dict):
            decision = reresolved.get("mcp_control_decision")
            _require(isinstance(decision, dict), "pm-command-chain-control-decision-required")
            current_governance = {"next_action": copy.deepcopy(decision.get("next_action"))}
        current_command = copy.deepcopy(fresh.get("command_state"))
        current_carrier = copy.deepcopy(fresh.get("carrier"))

    return stop("NONPROGRESS_CYCLE", blocker="PM_COMMAND_CHAIN_BOUND_EXHAUSTED")
