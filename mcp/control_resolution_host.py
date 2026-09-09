#!/usr/bin/env python3
"""Normal host binding for canonical MCP owner-effect resolution and execution.

The host constructs capability and persistence-verification dependencies from
trusted runtime objects. Event payloads may carry owner receipts, but they can
neither provide these dependencies nor self-assert executability or durability.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Mapping

import control_owner_routing
import control_resolution


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
    ):
        _require(callable(getattr(persistence_verifier, "verify", None)), "host-persistence-verifier-required")
        _require(callable(getattr(capability_resolver, "is_available", None)), "host-capability-resolver-required")
        _require(callable(getattr(capability_resolver, "executor", None)), "host-capability-executor-binding-required")
        _require(callable(canonical_resolver), "canonical-control-resolver-required")
        if pm_profile_verifier is not None:
            _require(callable(getattr(pm_profile_verifier, "verify", None)), "host-pm-profile-verifier-invalid")
            _require(callable(getattr(pm_profile_verifier, "verify_lifecycle_effect", None)), "host-lifecycle-effect-verifier-invalid")
        self._persistence_verifier = persistence_verifier
        self._capability_resolver = capability_resolver
        self._canonical_resolver = canonical_resolver
        self._pm_profile_verifier = pm_profile_verifier

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
    root: Path = control_resolution.SOURCE_ROOT,
    require_git_ancestry: bool = True,
) -> dict[str, Any]:
    """Consume the bounded PM-owned command frontier to a declared fixed point.

    The reader is a constructor-bound host dependency.  After every verified
    mutation it must return fresh canonical state; payloads cannot manufacture
    currentness, authority, or the next command.
    """

    _require(isinstance(max_steps, int) and 1 <= max_steps <= 32, "pm-command-chain-bound-invalid")
    read_current = getattr(current_state_reader, "read_current", None)
    _require(callable(read_current), "pm-command-chain-current-state-reader-required")
    current_governance = copy.deepcopy(governance)
    current_command = copy.deepcopy(command_state)
    current_carrier = copy.deepcopy(carrier)
    steps: list[dict[str, Any]] = []
    observed_frontiers: set[tuple[Any, ...]] = set()

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
        }
        if blocker:
            value["exact_blocker"] = blocker
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
