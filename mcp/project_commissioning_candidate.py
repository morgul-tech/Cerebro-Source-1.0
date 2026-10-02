"""Default-off, server-owned Project commissioning bridge candidate.

This module has no transport registration. The existing authenticated service
may call it only when an exact host binding is supplied at construction.
Project Basis initialization, owner effects and Signalvev return are out of
this A2 candidate and cannot be triggered here.
"""

from __future__ import annotations

import copy
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from control_context_tools import ControlContextMcpTools, VerifiedMcpIdentity
from control_context_state_postgres import StateBindingError


class ProjectCommissioningHold(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProjectCommissioningHold(reason)


@dataclass(frozen=True)
class _ServerProjectContext:
    identity: VerifiedMcpIdentity
    minted_session_ref: str
    request_meta: Mapping[str, Any]

    def session_ref(self) -> str:
        return self.minted_session_ref

    @property
    def subject_correlation(self) -> None:
        return None


class ProjectCommissioningBridge:
    """Internal operation with no caller-visible signer or general mint route."""

    _START_FIELDS = frozenset({
        "project_ref", "aggregate_id", "source_revision", "event_id",
        "decision_ref", "root",
    })
    _RESUME_FIELDS = frozenset({"project_ref", "aggregate_id", "session_handle"})

    def __init__(self, *, state_port: Any, tools: ControlContextMcpTools,
                 verifier: Any, issuer: Any, lineage_authorizer: Any):
        _require(issuer is verifier and callable(getattr(issuer, "seal", None))
                 and callable(getattr(verifier, "verify", None)),
                 "existing-private-attestor-must-be-shared")
        _require(callable(getattr(lineage_authorizer, "authorize", None)),
                 "server-bound-project-lineage-authorizer-required")
        self._state_port = state_port
        self._tools = tools
        self._issuer = issuer
        self._lineage_authorizer = lineage_authorizer

    @staticmethod
    def _session_ref(handle: str) -> str:
        _require(isinstance(handle, str)
                 and re.fullmatch(r"[A-Za-z0-9_-]{43}", handle) is not None,
                 "project-session-handle-invalid")
        return "project-commissioning:" + handle

    @staticmethod
    def _binding_id(handle: str) -> str:
        return "PCB-" + hashlib.sha256(handle.encode("ascii")).hexdigest()[:32].upper()

    def _authorize(self, identity: VerifiedMcpIdentity, args: dict[str, Any]) -> None:
        identity.validate()
        _require({"project_state:read", "project_state:transition"}
                 <= identity.state_scopes, "project-commissioning-scopes-required")
        for field in ("project_ref", "aggregate_id"):
            _require(isinstance(args.get(field), str) and bool(args[field].strip()),
                     f"{field}-required")
        decision = self._lineage_authorizer.authorize(
            identity=identity, project_ref=args["project_ref"],
            aggregate_id=args["aggregate_id"],
        )
        _require(isinstance(decision, dict) and decision.get("result") == "PASS"
                 and decision.get("project_ref") == args["project_ref"]
                 and decision.get("aggregate_id") == args["aggregate_id"]
                 and isinstance(decision.get("lineage_ref"), str)
                 and bool(decision["lineage_ref"]),
                 "project-lineage-unproven")

    @staticmethod
    def _scope(identity: VerifiedMcpIdentity) -> dict[str, Any]:
        return {"tenant_ref": identity.tenant_ref,
                "workspace_ref": identity.workspace_ref,
                "principal_ref": identity.principal_ref}

    def _default(self, identity: VerifiedMcpIdentity) -> dict[str, Any]:
        value = self._state_port.read_principal_default_binding(
            **self._scope(identity), scopes=identity.state_scopes)
        _require(isinstance(value, dict) and value.get("status") in {"PRESENT", "ABSENT"},
                 "default-binding-readback-invalid")
        return value

    def _read_session(self, identity: VerifiedMcpIdentity, handle: str) -> dict[str, Any]:
        session = self._state_port.read_session(
            **self._scope(identity), consumer_ref=identity.consumer_ref,
            session_ref=self._session_ref(handle), scopes=identity.state_scopes)
        _require(isinstance(session, dict)
                 and session.get("tenant_ref") == identity.tenant_ref
                 and session.get("workspace_ref") == identity.workspace_ref
                 and session.get("principal_ref") == identity.principal_ref
                 and session.get("consumer_ref") == identity.consumer_ref
                 and session.get("session_ref") == self._session_ref(handle)
                 and session.get("session_binding_id") == self._binding_id(handle)
                 and isinstance(session.get("fingerprint"), str)
                 and bool(session["fingerprint"])
                 and type(session.get("session_revision")) is int
                 and session["session_revision"] >= 1,
                 "server-session-readback-mismatch")
        return session

    def resume(self, *, identity: VerifiedMcpIdentity, args: dict[str, Any]) -> dict[str, Any]:
        _require(isinstance(args, dict) and set(args) == self._RESUME_FIELDS,
                 "project-resume-exact-fields-required")
        self._authorize(identity, args)
        handle = args["session_handle"]
        session = self._read_session(identity, handle)
        _require(session["project_ref"] == args["project_ref"],
                 "project-resume-project-mismatch")
        project = self._state_port.read_project(
            **self._scope(identity), project_ref=args["project_ref"],
            scopes=identity.state_scopes)
        _require(project.get("aggregate_id") == args["aggregate_id"]
                 and session["project_revision"] == project.get("revision"),
                 "project-resume-currentness-mismatch")
        return {"result": "PASS", "phase": "RESUME", "project_ref": args["project_ref"],
                "session_handle": handle, "session_fingerprint": session["fingerprint"],
                "project_fingerprint": project["fingerprint"]}

    def start(self, *, identity: VerifiedMcpIdentity, args: dict[str, Any]) -> dict[str, Any]:
        recovery = isinstance(args, dict) and args.get("recover_partial") is True
        allowed = self._START_FIELDS | ({"recover_partial"} if recovery else set())
        _require(isinstance(args, dict) and set(args) == allowed,
                 "project-start-exact-fields-required")
        self._authorize(identity, args)
        for field in ("source_revision", "event_id", "decision_ref"):
            _require(isinstance(args[field], str) and bool(args[field].strip()),
                     f"{field}-required")
        _require(isinstance(args["root"], dict) and bool(args["root"]),
                 "project-root-required")
        existing_project = None
        try:
            existing_project = self._state_port.read_project(
                **self._scope(identity), project_ref=args["project_ref"],
                scopes=identity.state_scopes)
        except StateBindingError as exc:
            _require(str(exc) == "project-instance-not-found", "project-collision-read-failed")
        if recovery:
            _require(isinstance(existing_project, dict)
                     and existing_project.get("aggregate_id") == args["aggregate_id"],
                     "partial-context-recovery-project-required")
            _require(self._state_port.has_project_commissioning_session(
                **self._scope(identity), consumer_ref=identity.consumer_ref,
                project_ref=args["project_ref"], scopes=identity.state_scopes) is False,
                "partial-context-recovery-session-already-bound")
        else:
            _require(existing_project is None, "project-ref-collision")
        before_default = self._default(identity)
        handle = secrets.token_urlsafe(32)
        session_ref = self._session_ref(handle)
        context = _ServerProjectContext(identity, session_ref, {})
        try:
            self._read_session(identity, handle)
        except StateBindingError as exc:
            _require(str(exc) == "control-session-not-bound", "session-collision-read-failed")
        else:
            raise ProjectCommissioningHold("commissioning-session-collision")
        payload = {k: copy.deepcopy(v) for k, v in args.items()
                   if k != "recover_partial"}
        payload["make_default"] = False
        attestation = self._issuer.seal(
            operation="create_project_control_instance", payload=payload,
            context=context)
        # The seal never leaves this method; the existing tool verifies it.
        try:
            created = self._tools.create_project_control_instance(
                {**payload, "control_resolution_attestation": attestation}, context,
            )["structuredContent"]
        except Exception as exc:
            raise ProjectCommissioningHold("context-bootstrap-unknown-no-auto-retry") from exc
        try:
            project = self._state_port.read_project(
                **self._scope(identity), project_ref=args["project_ref"],
                scopes=identity.state_scopes)
            _require(project == created["project"], "context-bootstrap-readback-mismatch")
            if recovery:
                _require(project == existing_project,
                         "partial-context-recovery-project-drift")
            bound = self._state_port.bind_session(
                **self._scope(identity), consumer_ref=identity.consumer_ref,
                session_ref=session_ref, session_binding_id=self._binding_id(handle),
                scopes=identity.state_scopes, project_ref=args["project_ref"],
                reject_existing=True, reject_project_commissioning_existing=True)
            session = self._read_session(identity, handle)
            _require(bound == session and session["project_ref"] == args["project_ref"]
                     and session["project_revision"] == project["revision"],
                     "context-session-readback-mismatch")
            after_default = self._default(identity)
            _require(before_default == after_default, "default-binding-changed")
        except Exception as exc:
            raise ProjectCommissioningHold("PARTIAL_CONTEXT_ONLY:readback-or-binding-failed") from exc
        return {"result": "PASS", "phase": "RECOVERED_CONTEXT_ONLY" if recovery else "CONTEXT_ONLY",
                "project_ref": args["project_ref"],
                "project_fingerprint": project["fingerprint"],
                "session_handle": handle, "session_fingerprint": session["fingerprint"],
                "default_binding": after_default, "project_basis_initialized": False}
