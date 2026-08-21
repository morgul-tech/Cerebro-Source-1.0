#!/usr/bin/env python3
"""Normal host binding for canonical MCP owner-effect resolution and execution.

The host constructs capability and persistence-verification dependencies from
trusted runtime objects.  Event payloads may carry owner receipts, but they can
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
PROHIBITED_RUNTIME_INJECTION_KEYS = {
    "persistence_evidence_verifier",
    "owner_persistence_verifier",
    "runtime_capability_resolver",
    "capability_resolver",
    "capability_available",
    "verifier_callable",
    "executor_callable",
    "pm_profile_verifier",
    "project_manager_profile_verifier",
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
        pm_profile_verifier: Any,
        canonical_resolver: Callable[..., dict[str, Any]] = control_resolution.resolve,
    ):
        _require(callable(getattr(persistence_verifier, "verify", None)), "host-persistence-verifier-required")
        _require(callable(getattr(capability_resolver, "is_available", None)), "host-capability-resolver-required")
        _require(callable(getattr(capability_resolver, "executor", None)), "host-capability-executor-binding-required")
        _require(callable(getattr(pm_profile_verifier, "verify", None)), "host-pm-profile-verifier-required")
        _require(callable(canonical_resolver), "canonical-control-resolver-required")
        self._persistence_verifier = persistence_verifier
        self._capability_resolver = capability_resolver
        self._pm_profile_verifier = pm_profile_verifier
        self._canonical_resolver = canonical_resolver

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
