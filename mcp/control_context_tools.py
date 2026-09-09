#!/usr/bin/env python3
"""Transport-neutral MCP tool adapter for hierarchical project-control state.

An HTTP/SSE MCP server can bind these handlers to its transport. This module does
not open a network listener and does not provide a production persistence backend.
It deliberately derives identity from a verified OAuth context and host metadata,
never from tool arguments, and it exposes no repository mutation capability.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import sys
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SOURCE_ROOT = Path(__file__).resolve().parents[1]
CONTEXT_TOOLING = SOURCE_ROOT / "tooling" / "context"
VALIDATOR_TOOLING = SOURCE_ROOT / "tooling" / "validator"
for path in (CONTEXT_TOOLING, VALIDATOR_TOOLING):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from control_context_state_port import BEGIN_SCHEMA, StateBindingError
from control_context_registry import actor_generation_shadow_fingerprint, validate_actor_generation_shadow
import project_manager_control_governor
from control_resolution_host import consume_operational_pulse
from control_owner_effect_receipt import validate_owner_effect_receipt  # noqa: E402
from human_navigation_surface_validation import (  # noqa: E402
    validate_navigation_options,
    validate_navigation_options_candidate,
)


STATE_SCOPES = frozenset({"project_state:read", "project_state:transition"})
FORBIDDEN_IDENTITY_ARGUMENTS = frozenset(
    {"tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref", "scopes"}
)


class ControlContextToolError(RuntimeError):
    pass


class ControlContextToolAuthorizationError(ControlContextToolError):
    pass


ATTESTATION_SCHEMA = "cerebro-mcp-control-resolution-attestation/v1"
LIFECYCLE_EFFECT_VERIFICATION_SCHEMA = "cerebro-context-lifecycle-effect-verification/v1"


@dataclass(frozen=True)
class VerifiedMcpIdentity:
    tenant_ref: str
    workspace_ref: str
    principal_ref: str
    scopes: frozenset[str]
    token_verified: bool
    consumer_ref: str = "CHATGPT_REMOTE_MCP"

    def validate(self) -> None:
        if self.token_verified is not True:
            raise ControlContextToolAuthorizationError("verified-OAuth-identity-required")
        for field in ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ControlContextToolAuthorizationError(f"verified-identity-{field}-required")

    @property
    def state_scopes(self) -> set[str]:
        self.validate()
        return set(self.scopes.intersection(STATE_SCOPES))


@dataclass(frozen=True)
class McpToolCallContext:
    identity: VerifiedMcpIdentity
    request_meta: Mapping[str, Any]

    def session_ref(self) -> str:
        self.identity.validate()
        raw = self.request_meta.get("openai/session")
        if not isinstance(raw, str) or not raw.strip():
            raw = self.request_meta.get("cerebro/session")
        if not isinstance(raw, str) or not raw.strip():
            raise ControlContextToolAuthorizationError("stable-control-session-metadata-required")
        namespace = "chatgpt" if "openai/session" in self.request_meta else "local"
        return f"{namespace}:{raw.strip()}"

    @property
    def subject_correlation(self) -> str | None:
        value = self.request_meta.get("openai/subject")
        return value.strip() if isinstance(value, str) and value.strip() else None


def _require_args(args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ControlContextToolError("tool-arguments-object-required")
    forbidden = sorted(FORBIDDEN_IDENTITY_ARGUMENTS.intersection(args))
    if forbidden:
        raise ControlContextToolAuthorizationError(
            "identity-or-authority-arguments-prohibited:" + ",".join(forbidden)
        )
    return args


def _require_text(args: dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ControlContextToolError(f"{field}-required")
    return value.strip()


def _session_binding_id(context: McpToolCallContext, project_ref: str | None) -> str:
    identity = context.identity
    subject = {
        "tenant_ref": identity.tenant_ref,
        "workspace_ref": identity.workspace_ref,
        "principal_ref": identity.principal_ref,
        "consumer_ref": identity.consumer_ref,
        "session_ref": context.session_ref(),
        "project_ref": project_ref,
    }
    raw = json.dumps(subject, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "CSB-" + hashlib.sha256(raw).hexdigest()[:24].upper()


def _attestation_subject(*, operation: str, payload: dict[str, Any], context: McpToolCallContext) -> dict[str, Any]:
    identity = context.identity
    identity.validate()
    return {
        "tenant_ref": identity.tenant_ref,
        "workspace_ref": identity.workspace_ref,
        "principal_ref": identity.principal_ref,
        "consumer_ref": identity.consumer_ref,
        "session_ref": context.session_ref(),
        "operation": operation,
        "payload": copy.deepcopy(payload),
    }


class HmacControlResolutionAttestor:
    """Reference/test attestor; production keys belong in a secret manager or HSM."""

    def __init__(self, *, key_id: str, secret: bytes):
        if not isinstance(key_id, str) or not key_id.strip():
            raise ValueError("attestation-key-id-required")
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("attestation-secret-minimum-32-bytes")
        self._key_id = key_id.strip()
        self._secret = secret

    def seal(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        context: McpToolCallContext,
    ) -> dict[str, Any]:
        subject = _attestation_subject(operation=operation, payload=payload, context=context)
        raw = json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return {
            "schema": ATTESTATION_SCHEMA,
            "algorithm": "HMAC-SHA256",
            "key_id": self._key_id,
            "subject_fingerprint": hashlib.sha256(raw).hexdigest(),
            "signature": hmac.new(self._secret, raw, hashlib.sha256).hexdigest(),
        }

    def verify(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        attestation: dict[str, Any],
        context: McpToolCallContext,
    ) -> None:
        if not isinstance(attestation, dict):
            raise ControlContextToolAuthorizationError("control-resolution-attestation-required")
        expected = self.seal(
            operation=operation,
            payload=payload,
            context=context,
        )
        for field in ("schema", "algorithm", "key_id", "subject_fingerprint"):
            if attestation.get(field) != expected[field]:
                raise ControlContextToolAuthorizationError(f"control-resolution-attestation-{field}-mismatch")
        signature = attestation.get("signature")
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected["signature"]):
            raise ControlContextToolAuthorizationError("control-resolution-attestation-signature-invalid")



PRINCIPAL_SUCCESSION_PERMIT_SCHEMA = "cerebro-principal-succession-permit/v1"
_RECEIPT_KEYS = {"receipt_ref", "receipt_fingerprint", "durable", "readback_verified"}
_PERMIT_EVIDENCE_KEYS = {
    "living_ledger", "livspuls", "etterklang_review", "stambok_seal",
    "gjenklang_publication", "human_readability", "predecessor_closeout",
    "cold_successor_canary",
}


def _principal_permit_fingerprint(permit: dict[str, Any]) -> str:
    subject = copy.deepcopy(permit)
    subject.pop("permit_fingerprint", None)
    return hashlib.sha256(
        json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _validate_content_blind_receipt(value: Any, *, pass_required: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlContextToolAuthorizationError("principal-succession-receipt-object-required")
    allowed = set(_RECEIPT_KEYS)
    if pass_required:
        allowed.add("result")
    if set(value) != allowed:
        raise ControlContextToolAuthorizationError("principal-succession-receipt-fields-invalid")
    ref = value.get("receipt_ref")
    fingerprint = value.get("receipt_fingerprint")
    if not isinstance(ref, str) or not ref or any(token in ref.lower() for token in ("http:", "https:", "file:", "\\", "/", "diary", "stambok")):
        raise ControlContextToolAuthorizationError("principal-succession-private-or-locator-ref-prohibited")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise ControlContextToolAuthorizationError("principal-succession-receipt-fingerprint-invalid")
    if value.get("durable") is not True or value.get("readback_verified") is not True:
        raise ControlContextToolAuthorizationError("principal-succession-durable-readback-required")
    if pass_required and value.get("result") != "PASS":
        raise ControlContextToolAuthorizationError("principal-cold-successor-canary-pass-required")
    return copy.deepcopy(value)


def qualify_lived_continuity_event(
    event: dict[str, Any],
    *,
    machine_diary_effect_verifier: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ControlContextToolAuthorizationError("lived-continuity-event-object-required")
    allowed = {
        "event_id", "event_fingerprint", "qualification", "debt_state",
        "machine_diary_effect_receipt",
    }
    if set(event).difference(allowed):
        raise ControlContextToolAuthorizationError("lived-continuity-private-content-or-unknown-field-prohibited")
    event_id = event.get("event_id")
    fingerprint = event.get("event_fingerprint")
    qualification = str(event.get("qualification") or "").upper()
    debt_state = str(event.get("debt_state") or "").upper()
    if not isinstance(event_id, str) or not event_id:
        raise ControlContextToolAuthorizationError("lived-continuity-event-id-required")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise ControlContextToolAuthorizationError("lived-continuity-event-fingerprint-invalid")
    if qualification not in {"CAPTURE", "NO_CAPTURE", "UNKNOWN"}:
        raise ControlContextToolAuthorizationError("lived-continuity-qualification-invalid")
    receipt = event.get("machine_diary_effect_receipt")
    if qualification == "CAPTURE":
        receipt = _validate_content_blind_receipt(receipt)
        verify = getattr(machine_diary_effect_verifier, "verify_machine_diary_effect", None)
        if not callable(verify):
            raise ControlContextToolAuthorizationError("constructor-bound-machine-diary-effect-verifier-required")
        verified = verify(event=copy.deepcopy(event), receipt=copy.deepcopy(receipt))
        if not isinstance(verified, dict) or verified.get("result") != "PASS":
            raise ControlContextToolAuthorizationError("machine-diary-effect-receipt-verification-nonpass")
        if debt_state != "CLEAR":
            raise ControlContextToolAuthorizationError("captured-lived-continuity-debt-must-be-clear")
    elif qualification == "NO_CAPTURE":
        if receipt is not None:
            raise ControlContextToolAuthorizationError("no-capture-must-not-create-fake-diary-effect")
        if debt_state != "CLEAR":
            raise ControlContextToolAuthorizationError("no-capture-lived-continuity-debt-must-be-clear")
    else:
        if receipt is not None:
            raise ControlContextToolAuthorizationError("unknown-lived-continuity-must-not-claim-diary-effect")
        if debt_state != "UNKNOWN":
            raise ControlContextToolAuthorizationError("unknown-lived-continuity-debt-required")
    return {
        "event_id": event_id,
        "event_fingerprint": fingerprint,
        "qualification": qualification,
        "debt_state": debt_state,
        "permit_eligible": qualification != "UNKNOWN",
        "result": "PASS" if qualification != "UNKNOWN" else "UNKNOWN_HOLD",
    }


def validate_principal_succession_permit(
    permit: dict[str, Any],
    *,
    expected_ref: str,
    expected_fingerprint: str,
    expected_source_head: str,
    machine_diary_effect_verifier: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(permit, dict):
        raise ControlContextToolAuthorizationError("principal-succession-permit-object-required")
    required = {
        "schema", "permit_id", "predecessor_generation_id", "successor_generation_id",
        "source_head", "currentness", "provider_revision", "covered_through_frontier",
        "lived_continuity", "evidence", "post_state_readback_verified", "permit_fingerprint",
    }
    if set(permit) != required:
        raise ControlContextToolAuthorizationError("principal-succession-permit-fields-invalid")
    if permit.get("schema") != PRINCIPAL_SUCCESSION_PERMIT_SCHEMA:
        raise ControlContextToolAuthorizationError("principal-succession-permit-schema-mismatch")
    if permit.get("permit_id") != expected_ref:
        raise ControlContextToolAuthorizationError("principal-succession-permit-ref-mismatch")
    supplied = permit.get("permit_fingerprint")
    if supplied != expected_fingerprint or supplied != _principal_permit_fingerprint(permit):
        raise ControlContextToolAuthorizationError("principal-succession-permit-fingerprint-mismatch")
    if permit.get("source_head") != expected_source_head:
        raise ControlContextToolAuthorizationError("principal-succession-permit-source-stale")
    if permit.get("currentness") != "CURRENT" or permit.get("post_state_readback_verified") is not True:
        raise ControlContextToolAuthorizationError("principal-succession-permit-current-readback-required")
    if not isinstance(permit.get("provider_revision"), int) or permit["provider_revision"] < 0:
        raise ControlContextToolAuthorizationError("principal-succession-provider-revision-invalid")
    if not isinstance(permit.get("covered_through_frontier"), int) or permit["covered_through_frontier"] < 0:
        raise ControlContextToolAuthorizationError("principal-succession-covered-frontier-invalid")
    lived = qualify_lived_continuity_event(
        permit.get("lived_continuity"),
        machine_diary_effect_verifier=machine_diary_effect_verifier,
    )
    if lived["permit_eligible"] is not True:
        raise ControlContextToolAuthorizationError("unknown-lived-continuity-debt-blocks-permit")
    evidence = permit.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != _PERMIT_EVIDENCE_KEYS:
        raise ControlContextToolAuthorizationError("principal-succession-evidence-set-incomplete")
    verified_evidence = {
        name: _validate_content_blind_receipt(
            evidence[name], pass_required=name == "cold_successor_canary"
        )
        for name in sorted(_PERMIT_EVIDENCE_KEYS)
    }
    return {
        "schema": PRINCIPAL_SUCCESSION_PERMIT_SCHEMA,
        "result": "PASS",
        "permit_id": permit["permit_id"],
        "permit_fingerprint": supplied,
        "predecessor_generation_id": permit["predecessor_generation_id"],
        "successor_generation_id": permit["successor_generation_id"],
        "source_head": permit["source_head"],
        "provider_revision": permit["provider_revision"],
        "covered_through_frontier": permit["covered_through_frontier"],
        "lived_continuity": lived,
        "evidence": verified_evidence,
    }


class ContextLifecycleEffectAdapter:
    """Constructor-bound Packet534 bridge over the existing actor shadow state port.

    READY_CURRENT is derived effect evidence only. The persisted shadow remains
    SHADOW_ONLY and stores lifecycle READY plus the exact target source revision.
    """

    _ROLES = ("ASSISTANT", "IMPLEMENTER", "PRINCIPAL", "PROJECT_MANAGER", "RESEARCHER", "WORKER")

    def __init__(
        self,
        state_port: Any,
        profile_verifier: Any,
        principal_succession_reader: Any | None = None,
        machine_diary_effect_verifier: Any | None = None,
    ):
        if not callable(getattr(profile_verifier, "verify", None)):
            raise ControlContextToolAuthorizationError("pm-profile-verifier-required")
        if principal_succession_reader is not None and not callable(
            getattr(principal_succession_reader, "read_principal_succession_permit", None)
        ):
            raise ControlContextToolAuthorizationError("principal-succession-reader-invalid")
        self._state_port = state_port
        self._profile_verifier = profile_verifier
        self._principal_succession_reader = principal_succession_reader
        self._machine_diary_effect_verifier = (
            machine_diary_effect_verifier
            if machine_diary_effect_verifier is not None
            else principal_succession_reader
        )

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise ControlContextToolAuthorizationError(message)

    def verify(self, *, binding: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        return self._profile_verifier.verify(
            binding=copy.deepcopy(binding),
            session=copy.deepcopy(session),
        )

    def _state_call(self, method: str, **kwargs: Any) -> Any:
        target = getattr(self._state_port, method, None)
        self._require(callable(target), f"lifecycle-state-port-method-required:{method}")
        parameters = inspect.signature(target).parameters
        return target(**{key: value for key, value in kwargs.items() if key in parameters})

    @staticmethod
    def _pre_effect_fingerprint(candidate: dict[str, Any]) -> str:
        subject = copy.deepcopy(candidate)
        subject.pop("effect_evidence", None)
        subject.pop("candidate_fingerprint", None)
        return hashlib.sha256(
            json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _effect_receipt_fingerprint(
        *,
        commit_ref: str,
        commit_fingerprint: str,
        candidate_fingerprint: str,
        shadow: dict[str, Any],
    ) -> str:
        subject = {
            "schema": "cerebro-context-lifecycle-effect-receipt/v1",
            "context_commit_ref": commit_ref,
            "context_commit_fingerprint": commit_fingerprint,
            "candidate_fingerprint": candidate_fingerprint,
            "actor_shadow_fingerprint": shadow["fingerprint"],
            "actor_shadow_revision": shadow["revision"],
        }
        return hashlib.sha256(
            json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _validate_pre_effect_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        self._require(isinstance(candidate, dict), "actor-lifecycle-candidate-object-required")
        self._require(candidate.get("effect_evidence") is None, "pre-effect-candidate-must-not-contain-effect-evidence")
        self._require(candidate.get("authority_source") == "PROJECT_MANAGER+MCP", "actor-lifecycle-authority-source-mismatch")
        try:
            gate = project_manager_control_governor._lifecycle_mutation_gate(
                {"lifecycle_mutation": copy.deepcopy(candidate)}
            )
        except Exception as exc:
            raise ControlContextToolAuthorizationError(f"actor-lifecycle-candidate-invalid:{exc}") from exc
        self._require(
            gate.get("result") == "PASS_CANDIDATE_READY_FOR_CONTEXT"
            and gate.get("ready_effect_allowed") is False,
            "actor-lifecycle-candidate-not-ready-for-context",
        )
        return gate

    def _read_unique_shadow(
        self,
        *,
        tenant_ref: str,
        workspace_ref: str,
        principal_ref: str,
        generation_ref: str,
    ) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        for role in self._ROLES:
            try:
                state = self._state_call(
                    "read_actor_generation_shadow",
                    tenant_ref=tenant_ref,
                    workspace_ref=workspace_ref,
                    role=role,
                    generation_ref=generation_ref,
                    principal_ref=principal_ref,
                    scopes={"project_state:read"},
                )
            except StateBindingError:
                continue
            validate_actor_generation_shadow(state)
            matches.append(state)
        self._require(
            len(matches) == 1,
            f"actor-generation-shadow-unique-match-required:{generation_ref}:{len(matches)}",
        )
        return matches[0]

    def verify_principal_succession(
        self,
        *,
        candidate: dict[str, Any],
        session: dict[str, Any],
    ) -> dict[str, Any]:
        self._require(isinstance(candidate, dict), "principal-succession-candidate-object-required")
        generation_ref = str(candidate.get("actor_generation_id") or "")
        self._require(generation_ref, "principal-succession-generation-required")
        for field in ("tenant_ref", "workspace_ref", "principal_ref"):
            self._require(
                isinstance(session.get(field), str) and bool(session[field].strip()),
                f"principal-succession-session-{field}-required",
            )
        shadow = self._read_unique_shadow(
            tenant_ref=session["tenant_ref"],
            workspace_ref=session["workspace_ref"],
            principal_ref=session["principal_ref"],
            generation_ref=generation_ref,
        )
        binding = candidate.get("principal_succession_permit_binding")
        if shadow["role"] != "PRINCIPAL":
            self._require(binding is None, "principal-permit-binding-on-nonprincipal-prohibited")
            return {
                "schema": "cerebro-principal-succession-verification/v1",
                "applicable": False,
                "result": "PASS_NON_PRINCIPAL_UNCHANGED",
                "trusted_actor_role": shadow["role"],
                "generation_ref": generation_ref,
            }
        self._require(isinstance(binding, dict), "principal-succession-permit-binding-required")
        self._require(
            set(binding) == {"permit_ref", "permit_fingerprint"},
            "principal-succession-permit-binding-fields-invalid",
        )
        reader = getattr(self._principal_succession_reader, "read_principal_succession_permit", None)
        self._require(callable(reader), "constructor-bound-principal-succession-reader-required")
        permit = reader(
            permit_ref=binding["permit_ref"],
            tenant_ref=session["tenant_ref"],
            workspace_ref=session["workspace_ref"],
            principal_ref=session["principal_ref"],
            actor_generation_id=generation_ref,
        )
        verified = validate_principal_succession_permit(
            permit,
            expected_ref=binding["permit_ref"],
            expected_fingerprint=binding["permit_fingerprint"],
            expected_source_head=shadow["source_revision"],
            machine_diary_effect_verifier=self._machine_diary_effect_verifier,
        )
        operation = str(candidate.get("operation") or "").upper()
        if operation == "RETIRE":
            self._require(
                verified["predecessor_generation_id"] == generation_ref,
                "principal-retire-predecessor-generation-mismatch",
            )
        elif operation == "START":
            self._require(
                verified["successor_generation_id"] == generation_ref,
                "principal-ready-successor-generation-mismatch",
            )
        else:
            raise ControlContextToolAuthorizationError(
                "principal-succession-permit-only-valid-for-retire-or-start"
            )
        return {
            "schema": "cerebro-principal-succession-verification/v1",
            "applicable": True,
            "result": "PASS",
            "trusted_actor_role": "PRINCIPAL",
            "operation": operation,
            "generation_ref": generation_ref,
            "permit_id": verified["permit_id"],
            "permit_fingerprint": verified["permit_fingerprint"],
            "provider_revision": verified["provider_revision"],
            "covered_through_frontier": verified["covered_through_frontier"],
            "post_state_readback_verified": True,
        }

    def execute_lifecycle_effect(
        self,
        *,
        candidate: dict[str, Any],
        context: McpToolCallContext,
        completion: dict[str, Any],
        bound_directive: dict[str, Any],
    ) -> dict[str, Any]:
        gate = self._validate_pre_effect_candidate(candidate)
        identity = context.identity
        identity.validate()
        generation_ref = candidate["actor_generation_id"]
        expected_revision = candidate["expected_lifecycle_revision"]
        previous_head = gate["previous_source_head"]
        target_head = gate["target_source_head"]
        pre_fingerprint = self._pre_effect_fingerprint(candidate)
        self._require(
            bound_directive.get("actor_lifecycle_mutation_candidate_fingerprint") == pre_fingerprint,
            "context-directive-lifecycle-candidate-fingerprint-mismatch",
        )
        state_commit = completion.get("state_commit")
        self._require(isinstance(state_commit, dict), "durable-state-commit-receipt-required")
        commit_ref = str(state_commit.get("commit_ref") or "")
        commit_fingerprint = str(state_commit.get("commit_fingerprint") or "")
        self._require(commit_ref, "durable-state-commit-ref-required")
        self._require(len(commit_fingerprint) == 64, "durable-state-commit-fingerprint-required")

        current = self._read_unique_shadow(
            tenant_ref=identity.tenant_ref,
            workspace_ref=identity.workspace_ref,
            principal_ref=identity.principal_ref,
            generation_ref=generation_ref,
        )
        self._require(current["authority"] == "SHADOW_ONLY", "actor-shadow-authority-must-remain-shadow-only")
        self._require(current["lifecycle"] == "READY", "requalification-shadow-must-be-ready")

        if current["revision"] == expected_revision and current["source_revision"] == previous_head:
            after = copy.deepcopy(current)
            after.update(
                lifecycle="READY",
                source_revision=target_head,
                revision=current["revision"] + 1,
            )
            after["fingerprint"] = actor_generation_shadow_fingerprint(after)
            validate_actor_generation_shadow(after)
            self._state_call(
                "write_actor_generation_shadow",
                state=after,
                expected_revision=current["revision"],
                principal_ref=identity.principal_ref,
                scopes={"project_state:transition"},
            )
        elif (
            current["revision"] == expected_revision + 1
            and current["source_revision"] == target_head
            and current["lifecycle"] == "READY"
        ):
            after = current
        else:
            raise ControlContextToolAuthorizationError(
                "actor-generation-shadow-stale-or-unexpected-poststate"
            )

        readback = self._read_unique_shadow(
            tenant_ref=identity.tenant_ref,
            workspace_ref=identity.workspace_ref,
            principal_ref=identity.principal_ref,
            generation_ref=generation_ref,
        )
        self._require(readback == after, "actor-generation-shadow-exact-readback-mismatch")
        self._require(readback["authority"] == "SHADOW_ONLY", "actor-shadow-authority-promotion-prohibited")
        self._require(readback["lifecycle"] == "READY", "actor-shadow-ready-readback-required")
        self._require(readback["source_revision"] == target_head, "actor-shadow-target-source-readback-required")
        receipt_fingerprint = self._effect_receipt_fingerprint(
            commit_ref=commit_ref,
            commit_fingerprint=commit_fingerprint,
            candidate_fingerprint=pre_fingerprint,
            shadow=readback,
        )
        return {
            "receipt_id": commit_ref,
            "context_commit_result": "COMMITTED",
            "durable": True,
            "post_state_readback_verified": True,
            "post_actor_generation_id": generation_ref,
            "post_lifecycle_state": "READY_CURRENT",
            "post_source_head": target_head,
            "provider_revision": readback["revision"],
            "receipt_fingerprint": receipt_fingerprint,
        }

    def verify_lifecycle_effect(
        self,
        *,
        evidence: dict[str, Any],
        candidate: dict[str, Any],
        session: dict[str, Any],
    ) -> dict[str, Any]:
        self._require(isinstance(evidence, dict), "lifecycle-effect-evidence-object-required")
        self._require(isinstance(candidate, dict), "lifecycle-effect-candidate-object-required")
        for field in ("tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref"):
            self._require(
                isinstance(session.get(field), str) and bool(session[field].strip()),
                f"lifecycle-session-{field}-required",
            )
        self._require(evidence.get("context_commit_result") == "COMMITTED", "lifecycle-context-commit-required")
        self._require(evidence.get("durable") is True, "lifecycle-durable-commit-required")
        self._require(
            evidence.get("post_state_readback_verified") is True,
            "lifecycle-poststate-readback-required",
        )
        pre_fingerprint = self._pre_effect_fingerprint(candidate)
        commit_ref = str(evidence.get("receipt_id") or "")
        bundle = self._state_call(
            "read_state_commit_evidence",
            tenant_ref=session["tenant_ref"],
            workspace_ref=session["workspace_ref"],
            principal_ref=session["principal_ref"],
            consumer_ref=session["consumer_ref"],
            session_ref=session["session_ref"],
            commit_ref=commit_ref,
            scopes={"project_state:read"},
        )
        self._require(isinstance(bundle, dict), "durable-context-commit-evidence-bundle-required")
        directive = bundle.get("directive")
        commit = bundle.get("commit")
        self._require(
            isinstance(directive, dict) and isinstance(commit, dict),
            "durable-context-commit-directive-required",
        )
        self._require(commit.get("commit_ref") == commit_ref, "durable-context-commit-ref-mismatch")
        self._require(
            directive.get("actor_lifecycle_mutation_candidate_fingerprint") == pre_fingerprint,
            "durable-context-commit-lifecycle-candidate-mismatch",
        )
        readback = self._read_unique_shadow(
            tenant_ref=session["tenant_ref"],
            workspace_ref=session["workspace_ref"],
            principal_ref=session["principal_ref"],
            generation_ref=candidate["actor_generation_id"],
        )
        transition = candidate.get("source_transition") or {}
        self._require(
            readback["authority"] == "SHADOW_ONLY",
            "verified-shadow-authority-must-remain-shadow-only",
        )
        self._require(readback["lifecycle"] == "READY", "verified-shadow-ready-required")
        self._require(
            readback["source_revision"] == transition.get("target_source_head"),
            "verified-shadow-target-source-mismatch",
        )
        self._require(
            readback["revision"] == evidence.get("provider_revision"),
            "verified-shadow-provider-revision-mismatch",
        )
        expected_fingerprint = self._effect_receipt_fingerprint(
            commit_ref=commit_ref,
            commit_fingerprint=commit["commit_fingerprint"],
            candidate_fingerprint=pre_fingerprint,
            shadow=readback,
        )
        self._require(
            evidence.get("receipt_fingerprint") == expected_fingerprint,
            "lifecycle-effect-receipt-fingerprint-mismatch",
        )
        self._require(
            evidence.get("post_actor_generation_id") == candidate["actor_generation_id"],
            "verified-lifecycle-generation-mismatch",
        )
        self._require(
            evidence.get("post_lifecycle_state") == "READY_CURRENT",
            "verified-lifecycle-derived-state-mismatch",
        )
        self._require(
            evidence.get("post_source_head") == readback["source_revision"],
            "verified-lifecycle-source-mismatch",
        )
        return {
            "schema": LIFECYCLE_EFFECT_VERIFICATION_SCHEMA,
            "result": "PASS",
            "receipt_id": evidence["receipt_id"],
            "post_actor_generation_id": evidence["post_actor_generation_id"],
            "post_lifecycle_state": evidence["post_lifecycle_state"],
            "post_source_head": evidence["post_source_head"],
            "provider_revision": evidence["provider_revision"],
            "receipt_fingerprint": evidence["receipt_fingerprint"],
            "verifier_ref": "CONTEXT-ACTOR-SHADOW-DURABLE-READBACK",
        }


def activate_committed_navigation_options(
    candidate: dict[str, Any] | None,
    completion: dict[str, Any],
) -> dict[str, Any] | None:
    """Promote a non-renderable MCP candidate only after exact committed proof."""

    if candidate is None:
        return None
    if not isinstance(candidate, dict):
        raise ControlContextToolError("navigation-options-candidate-object-required")
    if not isinstance(completion, dict):
        raise ControlContextToolError("event-completion-object-required")
    project = completion.get("project")
    session = completion.get("session")
    receipt = completion.get("receipt")
    if not all(isinstance(value, dict) for value in (project, session, receipt)):
        raise ControlContextToolError("completion-project-session-and-receipt-required")
    if candidate.get("schema") != "cerebro-mcp-context-navigation-options-candidate/v1":
        raise ControlContextToolError("navigation-options-candidate-schema-mismatch")
    if candidate.get("render_authorized") is not False:
        raise ControlContextToolError("precommit-navigation-candidate-cannot-authorize-render")
    try:
        validate_navigation_options_candidate(candidate, project, session, receipt)
    except Exception as exc:
        raise ControlContextToolError(str(exc)) from exc
    if candidate.get("expected_transition_receipt_ref") != receipt.get("receipt_id"):
        raise ControlContextToolError("navigation-options-receipt-ref-mismatch")
    if candidate.get("expected_transition_receipt_fingerprint") != receipt.get("receipt_fingerprint"):
        raise ControlContextToolError("navigation-options-receipt-fingerprint-mismatch")
    for candidate_field, state_value in (
        ("project_ref", project.get("project_ref")),
        ("session_ref", session.get("session_ref")),
        ("source_context_ref", session.get("active_context_ref")),
        ("project_revision", project.get("revision")),
        ("session_revision", session.get("session_revision")),
        ("project_fingerprint", project.get("fingerprint")),
        ("session_fingerprint", session.get("fingerprint")),
    ):
        if candidate.get(candidate_field) != state_value:
            raise ControlContextToolError(f"navigation-options-committed-{candidate_field}-mismatch")
    for candidate_field, receipt_field in (
        ("project_revision", "project_revision_after"),
        ("session_revision", "session_revision_after"),
        ("project_fingerprint", "project_fingerprint_after"),
        ("session_fingerprint", "session_fingerprint_after"),
    ):
        if candidate.get(candidate_field) != receipt.get(receipt_field):
            raise ControlContextToolError(f"navigation-options-receipt-{candidate_field}-mismatch")

    options = copy.deepcopy(candidate)
    for field in (
        "render_authorized", "activation_precondition", "expected_transition_receipt_ref",
        "expected_transition_receipt_fingerprint", "candidate_fingerprint",
    ):
        options.pop(field, None)
    options.update(
        schema="cerebro-mcp-context-navigation-options/v1",
        state_basis="COMMITTED_STATE",
        render_precondition="COMMIT_RECEIPT_AND_STATE_MATCH_VERIFIED",
        commit_verified=True,
        commit_receipt_ref=receipt["receipt_id"],
        commit_receipt_fingerprint=receipt["receipt_fingerprint"],
        options_fingerprint="",
    )
    subject = copy.deepcopy(options)
    subject.pop("options_fingerprint", None)
    options["options_fingerprint"] = hashlib.sha256(
        json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    try:
        validate_navigation_options(options, project, session, receipt)
    except Exception as exc:
        raise ControlContextToolError(str(exc)) from exc
    return options


def _attestation_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "algorithm", "key_id", "subject_fingerprint", "signature"],
        "properties": {
            "schema": {"const": ATTESTATION_SCHEMA},
            "algorithm": {"enum": ["HMAC-SHA256", "ED25519", "OPAQUE-SERVICE-SEAL"]},
            "key_id": {"type": "string", "minLength": 1},
            "subject_fingerprint": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "signature": {"type": "string", "minLength": 32},
        },
    }


def _object_output_schema(
    *,
    required: tuple[str, ...],
    properties: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "required": list(required),
        "properties": copy.deepcopy(dict(properties)),
    }


def tool_definitions() -> list[dict[str, Any]]:
    """Return MCP-compatible tool descriptors with conservative annotations."""

    return [
        {
            "name": "read_project_control_state",
            "title": "Read project control state",
            "description": "Use this when the user needs the authenticated workspace's current project-control snapshot.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project_ref"],
                "properties": {"project_ref": {"type": "string", "minLength": 1}},
            },
            "outputSchema": _object_output_schema(
                required=("project", "repository_permission_required"),
                properties={
                    "project": {"type": "object"},
                    "repository_permission_required": {"const": False},
                },
            ),
            "securitySchemes": [{"type": "oauth2", "scopes": ["project_state:read"]}],
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "begin_project_control_event",
            "title": "Begin project control event",
            "description": "Use this before project reasoning to bind this host session and begin one idempotent control event.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event_id", "idempotency_key"],
                "properties": {
                    "event_id": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                    "project_ref": {"type": "string", "minLength": 1},
                },
            },
            "outputSchema": _object_output_schema(
                required=(
                    "schema", "event_id", "project", "session",
                    "expected_project_revision", "expected_project_fingerprint",
                    "expected_session_revision", "expected_session_fingerprint",
                    "repository_permission_required",
                ),
                properties={
                    "schema": {"const": "cerebro-control-context-event-binding/v1"},
                    "event_id": {"type": "string"},
                    "project": {"type": "object"},
                    "session": {"type": "object"},
                    "expected_project_revision": {"type": "integer", "minimum": 1},
                    "expected_project_fingerprint": {"type": "string"},
                    "expected_session_revision": {"type": "integer", "minimum": 1},
                    "expected_session_fingerprint": {"type": "string"},
                    "repository_permission_required": {"const": False},
                },
            ),
            "securitySchemes": [{"type": "oauth2", "scopes": ["project_state:transition"]}],
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "complete_project_control_event",
            "title": "Complete project control event",
            "description": "Use this after canonical resolution to atomically commit its attested directive and matching navigation options.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event_id", "directive", "control_resolution_attestation"],
                "properties": {
                    "event_id": {"type": "string", "minLength": 1},
                    "directive": {"type": "object"},
                    "navigation_options_candidate": {"type": "object"},
                    "context_owner_effect_candidate": {"type": "object"},
                    "actor_lifecycle_mutation_candidate": {"type": "object"},
                    "operational_pulse_request": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["stage", "actor_role", "objective_ref"],
                        "properties": {
                            "stage": {"enum": ["FRESH_WORLD", "CURRENT_CONTACT", "PRE_CLOSE"]},
                            "actor_role": {"type": "string", "minLength": 1},
                            "objective_ref": {"type": "string", "minLength": 1},
                            "prior_watermarks": {"type": "object"},
                        },
                    },
                    "control_resolution_attestation": _attestation_input_schema(),
                },
            },
            "outputSchema": _object_output_schema(
                required=(
                    "result", "project", "session", "receipt",
                    "mcp_context_navigation_options", "human_navigation_surface_required",
                    "navigation_activation", "repository_permission_required",
                ),
                properties={
                    "result": {"const": "PASS"},
                    "project": {"type": "object"},
                    "session": {"type": "object"},
                    "receipt": {"type": "object"},
                    "mcp_context_navigation_options": {
                        "anyOf": [{"type": "object"}, {"type": "null"}]
                    },
                    "human_navigation_surface_required": {"type": "boolean"},
                    "navigation_activation": {"type": "object"},
                    "operational_pulse": {"type": "object"},
                    "repository_permission_required": {"const": False},
                },
            ),
            "securitySchemes": [{"type": "oauth2", "scopes": ["project_state:transition"]}],
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "create_project_control_instance",
            "title": "Create project control instance",
            "description": "Use this when an authorized new project needs a distinct instance and single root context.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "project_ref", "aggregate_id", "source_revision", "event_id", "decision_ref", "root",
                    "control_resolution_attestation"
                ],
                "properties": {
                    "project_ref": {"type": "string", "minLength": 1},
                    "aggregate_id": {"type": "string", "minLength": 1},
                    "source_revision": {"type": "string", "minLength": 1},
                    "event_id": {"type": "string", "minLength": 1},
                    "decision_ref": {"type": "string", "minLength": 1},
                    "root": {"type": "object"},
                    "make_default": {"type": "boolean"},
                    "control_resolution_attestation": _attestation_input_schema(),
                },
            },
            "outputSchema": _object_output_schema(
                required=("project", "receipt", "repository_permission_required"),
                properties={
                    "project": {"type": "object"},
                    "receipt": {"type": "object"},
                    "repository_permission_required": {"const": False},
                },
            ),
            "securitySchemes": [{"type": "oauth2", "scopes": ["project_state:transition"]}],
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "set_default_project_control_instance",
            "title": "Set default project control instance",
            "description": "Use this to set the authenticated principal's default project for newly bound sessions.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project_ref", "control_resolution_attestation"],
                "properties": {
                    "project_ref": {"type": "string", "minLength": 1},
                    "control_resolution_attestation": _attestation_input_schema()
                },
            },
            "outputSchema": _object_output_schema(
                required=("result", "project_ref", "repository_permission_required"),
                properties={
                    "result": {"const": "PASS"},
                    "project_ref": {"type": "string"},
                    "repository_permission_required": {"const": False},
                },
            ),
            "securitySchemes": [{"type": "oauth2", "scopes": ["project_state:transition"]}],
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        },
    ]


class ControlContextMcpTools:
    """MCP handler collection over an injected state-port implementation."""

    def __init__(
        self,
        state_port: Any,
        resolution_attestation_verifier: Any,
        lifecycle_effect_adapter: Any | None = None,
        provider_tail_reader: Any | None = None,
    ):
        self._state_port = state_port
        if not callable(getattr(resolution_attestation_verifier, "verify", None)):
            raise ControlContextToolAuthorizationError("control-resolution-attestation-verifier-required")
        if lifecycle_effect_adapter is not None:
            for method in ("verify", "execute_lifecycle_effect", "verify_lifecycle_effect"):
                if not callable(getattr(lifecycle_effect_adapter, method, None)):
                    raise ControlContextToolAuthorizationError(f"lifecycle-effect-adapter-method-required:{method}")
        if provider_tail_reader is not None and not callable(
            getattr(provider_tail_reader, "read_operational_tails", None)
        ):
            raise ControlContextToolAuthorizationError("provider-tail-reader-invalid")
        self._resolution_attestation_verifier = resolution_attestation_verifier
        self._lifecycle_effect_adapter = lifecycle_effect_adapter
        self._provider_tail_reader = provider_tail_reader

    @staticmethod
    def _identity(context: McpToolCallContext) -> VerifiedMcpIdentity:
        context.identity.validate()
        return context.identity

    @staticmethod
    def _result(value: dict[str, Any], context: McpToolCallContext) -> dict[str, Any]:
        return {
            "structuredContent": value,
            "_meta": {
                "cerebro/repositoryPermissionRequired": False,
                "cerebro/sessionCorrelationPresent": True,
                "cerebro/subjectCorrelationPresent": context.subject_correlation is not None,
            },
        }

    def dispatch(self, tool_name: str, args: Any, context: McpToolCallContext) -> dict[str, Any]:
        handlers = {
            "read_project_control_state": self.read_project_control_state,
            "begin_project_control_event": self.begin_project_control_event,
            "complete_project_control_event": self.complete_project_control_event,
            "create_project_control_instance": self.create_project_control_instance,
            "set_default_project_control_instance": self.set_default_project_control_instance,
        }
        if tool_name not in handlers:
            raise ControlContextToolError(f"unknown-control-context-tool:{tool_name}")
        return handlers[tool_name](_require_args(args), context)

    def read_project_control_state(self, args: dict[str, Any], context: McpToolCallContext) -> dict[str, Any]:
        identity = self._identity(context)
        project = self._state_port.read_project(
            tenant_ref=identity.tenant_ref,
            workspace_ref=identity.workspace_ref,
            principal_ref=identity.principal_ref,
            project_ref=_require_text(args, "project_ref"),
            scopes=identity.state_scopes,
        )
        return self._result({"project": project, "repository_permission_required": False}, context)

    def begin_project_control_event(self, args: dict[str, Any], context: McpToolCallContext) -> dict[str, Any]:
        identity = self._identity(context)
        project_ref = args.get("project_ref")
        if project_ref is not None and (not isinstance(project_ref, str) or not project_ref.strip()):
            raise ControlContextToolError("project_ref-must-be-nonempty-when-present")
        project_ref = project_ref.strip() if isinstance(project_ref, str) else None
        session_ref = context.session_ref()
        self._state_port.bind_session(
            tenant_ref=identity.tenant_ref,
            workspace_ref=identity.workspace_ref,
            principal_ref=identity.principal_ref,
            consumer_ref=identity.consumer_ref,
            session_ref=session_ref,
            session_binding_id=_session_binding_id(context, project_ref),
            scopes=identity.state_scopes,
            project_ref=project_ref,
        )
        binding = self._state_port.begin_event(
            {
                "schema": BEGIN_SCHEMA,
                "tenant_ref": identity.tenant_ref,
                "workspace_ref": identity.workspace_ref,
                "principal_ref": identity.principal_ref,
                "consumer_ref": identity.consumer_ref,
                "session_ref": session_ref,
                "event_id": _require_text(args, "event_id"),
                "idempotency_key": _require_text(args, "idempotency_key"),
            },
            scopes=identity.state_scopes,
        )
        return self._result(binding, context)

    def complete_project_control_event(self, args: dict[str, Any], context: McpToolCallContext) -> dict[str, Any]:
        identity = self._identity(context)
        directive = args.get("directive")
        if not isinstance(directive, dict):
            raise ControlContextToolError("directive-object-required")
        candidate = args.get("navigation_options_candidate")
        if candidate is not None and not isinstance(candidate, dict):
            raise ControlContextToolError("navigation-options-candidate-object-required")
        owner_candidate = args.get("context_owner_effect_candidate")
        if owner_candidate is not None:
            if not isinstance(owner_candidate, dict):
                raise ControlContextToolError("context-owner-effect-candidate-object-required")
            try:
                validated_owner_candidate = validate_owner_effect_receipt(
                    owner_candidate,
                    expected_owner="context",
                    expected_control_decision_ref=directive.get("decision_ref"),
                    expected_effect="REFRESH_GOVERNING_REFS",
                )
            except Exception as exc:
                raise ControlContextToolError(str(exc)) from exc
            if validated_owner_candidate["current"] is not False or owner_candidate.get("result") != "CANDIDATE":
                raise ControlContextToolError("context-owner-effect-precommit-candidate-required")
        lifecycle_candidate = args.get("actor_lifecycle_mutation_candidate")
        if lifecycle_candidate is not None:
            if not isinstance(lifecycle_candidate, dict):
                raise ControlContextToolError("actor-lifecycle-mutation-candidate-object-required")
            if self._lifecycle_effect_adapter is None:
                raise ControlContextToolAuthorizationError("actor-lifecycle-effect-adapter-unbound")
        pulse_request = args.get("operational_pulse_request")
        if pulse_request is not None and not isinstance(pulse_request, dict):
            raise ControlContextToolError("operational-pulse-request-object-required")
        event_id = _require_text(args, "event_id")
        signed_payload = {
            "event_id": event_id,
            "directive": copy.deepcopy(directive),
            "navigation_options_candidate": copy.deepcopy(candidate),
        }
        if owner_candidate is not None:
            signed_payload["context_owner_effect_candidate"] = copy.deepcopy(owner_candidate)
        if lifecycle_candidate is not None:
            signed_payload["actor_lifecycle_mutation_candidate"] = copy.deepcopy(lifecycle_candidate)
        if pulse_request is not None:
            signed_payload["operational_pulse_request"] = copy.deepcopy(pulse_request)
        self._resolution_attestation_verifier.verify(
            operation="complete_project_control_event",
            payload=signed_payload,
            attestation=args.get("control_resolution_attestation"),
            context=context,
        )
        operational_pulse = None
        if pulse_request is not None:
            operational_pulse = consume_operational_pulse(
                self._provider_tail_reader,
                stage=pulse_request.get("stage"),
                actor_role=pulse_request.get("actor_role"),
                objective_ref=pulse_request.get("objective_ref"),
                prior_watermarks=pulse_request.get("prior_watermarks"),
            )
            if operational_pulse.get("result") == "UNKNOWN_HOLD":
                raise ControlContextToolAuthorizationError(
                    "operational-pulse-unknown-hold:" + str(operational_pulse.get("exact_blocker"))
                )
        state_directive = copy.deepcopy(directive)
        operational_pulse_ref = None
        if operational_pulse is not None:
            pulse_material = json.dumps(
                operational_pulse["persistence_directive"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            operational_pulse_ref = "OPERATIONAL-PULSE-" + hashlib.sha256(pulse_material).hexdigest()
            if operational_pulse.get("delta") == "DELTA":
                project_operations = state_directive.get("project_operations")
                if not isinstance(project_operations, list):
                    raise ControlContextToolError("operational-pulse-project-operations-required")
                refreshes = [
                    item for item in project_operations
                    if isinstance(item, dict) and item.get("operation") == "REFRESH_GOVERNING_REFS"
                ]
                if len(refreshes) != 1:
                    raise ControlContextToolError("operational-pulse-single-refresh-governing-refs-required")
                basis_refs = refreshes[0].get("basis_refs")
                if not isinstance(basis_refs, list):
                    raise ControlContextToolError("operational-pulse-refresh-basis-refs-required")
                refreshes[0]["basis_refs"] = list(dict.fromkeys([*basis_refs, operational_pulse_ref]))
            operational_pulse["persistence_ref"] = operational_pulse_ref
        if lifecycle_candidate is not None:
            state_directive["actor_lifecycle_mutation_candidate_fingerprint"] = (
                self._lifecycle_effect_adapter._pre_effect_fingerprint(lifecycle_candidate)
            )
        completion = self._state_port.complete_event(
            {
                "tenant_ref": identity.tenant_ref,
                "workspace_ref": identity.workspace_ref,
                "principal_ref": identity.principal_ref,
                "consumer_ref": identity.consumer_ref,
                "session_ref": context.session_ref(),
                "event_id": event_id,
                "directive": state_directive,
                "navigation_options_candidate_fingerprint": (
                    candidate.get("candidate_fingerprint") if isinstance(candidate, dict) else None
                ),
                "owner_effect_candidate": copy.deepcopy(owner_candidate),
            },
            scopes=identity.state_scopes,
        )
        if operational_pulse is not None:
            receipt = completion.get("receipt")
            if not isinstance(receipt, dict):
                raise ControlContextToolError("operational-pulse-persistence-readback-required")
            if operational_pulse.get("delta") == "DELTA":
                project_readback = completion.get("project")
                if (
                    not isinstance(project_readback, dict)
                    or operational_pulse_ref not in json.dumps(project_readback, sort_keys=True)
                ):
                    raise ControlContextToolError("operational-pulse-persistence-ref-readback-mismatch")
            operational_pulse["persistence_readback_verified"] = True
            operational_pulse["persistence_receipt_ref"] = (
                receipt.get("receipt_ref") or receipt.get("event_id") or event_id
            )
            completion["operational_pulse"] = operational_pulse
        if lifecycle_candidate is not None:
            if isinstance(lifecycle_candidate.get("source_transition"), dict):
                completion["actor_lifecycle_effect_evidence"] = (
                    self._lifecycle_effect_adapter.execute_lifecycle_effect(
                        candidate=copy.deepcopy(lifecycle_candidate),
                        context=context,
                        completion=completion,
                        bound_directive=state_directive,
                    )
                )
            else:
                completion["principal_succession_verification"] = (
                    self._lifecycle_effect_adapter.verify_principal_succession(
                        candidate=copy.deepcopy(lifecycle_candidate),
                        session={
                            "tenant_ref": identity.tenant_ref,
                            "workspace_ref": identity.workspace_ref,
                            "principal_ref": identity.principal_ref,
                            "consumer_ref": identity.consumer_ref,
                            "session_ref": context.session_ref(),
                        },
                    )
                )
        navigation_error = None
        try:
            options = activate_committed_navigation_options(candidate, completion)
        except ControlContextToolError as exc:
            options = None
            navigation_error = str(exc)
        completion["mcp_context_navigation_options"] = options
        completion["human_navigation_surface_required"] = completion["mcp_context_navigation_options"] is not None
        completion["navigation_activation"] = {
            "result": "BLOCK" if navigation_error else ("PASS" if options is not None else "NOT_REQUESTED"),
            "error": navigation_error,
            "recovery": "RELOAD_AND_RERESOLVE" if navigation_error else None,
            "state_commit_remains_valid": True,
        }
        return self._result(completion, context)

    def create_project_control_instance(self, args: dict[str, Any], context: McpToolCallContext) -> dict[str, Any]:
        identity = self._identity(context)
        root = args.get("root")
        if not isinstance(root, dict):
            raise ControlContextToolError("root-object-required")
        signed_payload = {key: copy.deepcopy(value) for key, value in args.items() if key != "control_resolution_attestation"}
        self._resolution_attestation_verifier.verify(
            operation="create_project_control_instance",
            payload=signed_payload,
            attestation=args.get("control_resolution_attestation"),
            context=context,
        )
        created = self._state_port.bootstrap_project(
            tenant_ref=identity.tenant_ref,
            workspace_ref=identity.workspace_ref,
            principal_ref=identity.principal_ref,
            project_ref=_require_text(args, "project_ref"),
            aggregate_id=_require_text(args, "aggregate_id"),
            source_revision=_require_text(args, "source_revision"),
            event_id=_require_text(args, "event_id"),
            decision_ref=_require_text(args, "decision_ref"),
            root=root,
            scopes=identity.state_scopes,
            make_default=args.get("make_default", True) is True,
        )
        created["repository_permission_required"] = False
        return self._result(created, context)

    def set_default_project_control_instance(self, args: dict[str, Any], context: McpToolCallContext) -> dict[str, Any]:
        identity = self._identity(context)
        project_ref = _require_text(args, "project_ref")
        self._resolution_attestation_verifier.verify(
            operation="set_default_project_control_instance",
            payload={"project_ref": project_ref},
            attestation=args.get("control_resolution_attestation"),
            context=context,
        )
        self._state_port.set_default_project(
            tenant_ref=identity.tenant_ref,
            workspace_ref=identity.workspace_ref,
            principal_ref=identity.principal_ref,
            project_ref=project_ref,
            scopes=identity.state_scopes,
        )
        return self._result(
            {"result": "PASS", "project_ref": project_ref, "repository_permission_required": False},
            context,
        )
