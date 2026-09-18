"""Same-State-Service custody adapter. Constructor dependencies are trust seams.

No event payload can install a producer/currentness/verifier. The final permit's
readback boolean is fingerprint-bound data, not authority. Only a fresh committed
DB readback plus MCP-owned semantic checks makes it eligible for reader return.
Nothing here deploys, migrates, creates a Principal, or automatically issues.
"""
from __future__ import annotations

import copy
from typing import Any

from control_context_state_port import (
    SUCCESSION_IDENTITY_FIELDS, SUCCESSION_RECORD_SCHEMA, StateAuthorizationError,
    StateBindingError, _sha256, validate_principal_succession_custody_record,
)
from control_context_tools import (
    PRINCIPAL_SUCCESSION_PERMIT_SCHEMA, _principal_permit_fingerprint,
    _validate_content_blind_id,
    validate_principal_succession_permit,
)


INPUT_FIELDS = {
    "predecessor_generation_id", "successor_generation_id", "source_head",
    "provider_revision", "covered_through_frontier", "principal_continuity_baseline",
    "lived_continuity", "evidence",
}


class PrincipalSuccessionPermitProvider:
    def __init__(self, *, state_port, trusted_inputs_reader, mcp_authorizer,
                 machine_diary_effect_verifier):
        for dependency, method in (
            (state_port, "commit_principal_succession_custody"),
            (state_port, "read_principal_succession_custody"),
            (trusted_inputs_reader, "read_principal_succession_inputs"),
            (mcp_authorizer, "authorize_principal_succession_custody"),
            (machine_diary_effect_verifier, "verify_machine_diary_effect"),
        ):
            if not callable(getattr(dependency, method, None)):
                raise StateBindingError("succession-constructor-trusted-dependency-required:" + method)
        self.state_port = state_port
        self._inputs = trusted_inputs_reader
        self._authorizer = mcp_authorizer
        self._diary = machine_diary_effect_verifier

    def _current(self, identity, actor_generation_id):
        if (set(identity) != set(SUCCESSION_IDENTITY_FIELDS) or
                any(not isinstance(v, str) or not v or v != v.strip() for v in identity.values())):
            raise StateBindingError("succession-exact-authenticated-identity-required")
        _validate_content_blind_id(actor_generation_id)
        value = copy.deepcopy(self._inputs.read_principal_succession_inputs(
            **identity, actor_generation_id=actor_generation_id))
        if not isinstance(value, dict) or set(value) != INPUT_FIELDS:
            raise StateBindingError("succession-exact-trusted-inputs-required")
        if actor_generation_id not in {value["predecessor_generation_id"], value["successor_generation_id"]}:
            raise StateAuthorizationError("succession-generation-reservation-mismatch")
        return value

    def _validate(self, permit, identity, actor_generation_id, operation):
        current = self._current(identity, actor_generation_id)
        validate_principal_succession_permit(
            permit, expected_ref=permit.get("permit_id"),
            expected_fingerprint=permit.get("permit_fingerprint"),
            expected_source_head=current["source_head"],
            machine_diary_effect_verifier=self._diary)
        if any(permit.get(key) != value for key, value in current.items()):
            raise StateAuthorizationError("succession-current-trusted-inputs-mismatch")
        grant = self._authorizer.authorize_principal_succession_custody(
            operation=operation, identity=copy.deepcopy(identity),
            actor_generation_id=actor_generation_id, permit=copy.deepcopy(permit))
        expected = {"result": "PASS", "operation": operation, "identity": identity,
                    "actor_generation_id": actor_generation_id,
                    "permit_fingerprint": permit["permit_fingerprint"]}
        if grant != expected:
            raise StateAuthorizationError("succession-exact-MCP-authorization-required")

    def persist_principal_succession_permit(self, *, permit_ref, request_ref,
                                           expected_revision, actor_generation_id,
                                           tenant_ref, workspace_ref, principal_ref):
        """Authorized internal custody operation, never a public issuance tool."""
        identity = dict(tenant_ref=tenant_ref, workspace_ref=workspace_ref, principal_ref=principal_ref)
        current = self._current(identity, actor_generation_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise StateBindingError("succession-expected-revision-required")
        permit = {"schema": PRINCIPAL_SUCCESSION_PERMIT_SCHEMA, "permit_id": permit_ref,
                  **current, "currentness": "CURRENT", "post_state_readback_verified": True}
        permit["permit_fingerprint"] = _principal_permit_fingerprint(permit)
        self._validate(permit, identity, actor_generation_id, "WRITE_CUSTODY")
        record = {"schema": SUCCESSION_RECORD_SCHEMA, "identity": identity,
                  "revision": expected_revision + 1, "request_ref": request_ref, "permit": permit}
        record["fingerprint"] = _sha256(record)
        validate_principal_succession_custody_record(record)
        committed = self.state_port.commit_principal_succession_custody(
            record=record, expected_revision=expected_revision, scopes={"project_state:transition"})
        observed = self.state_port.read_principal_succession_custody(
            **identity, permit_ref=permit_ref, scopes={"project_state:read"})
        if (observed.get("independent_committed_readback_verified") is not True or
                observed.get("record") != record or observed.get("receipt") != committed):
            raise StateBindingError("succession-committed-exact-readback-required")
        self._validate(observed["record"]["permit"], identity, actor_generation_id, "READ_CUSTODY")
        return copy.deepcopy(observed)

    def read_principal_succession_permit(self, *, permit_ref, tenant_ref, workspace_ref,
                                        principal_ref, actor_generation_id):
        identity = dict(tenant_ref=tenant_ref, workspace_ref=workspace_ref, principal_ref=principal_ref)
        self._current(identity, actor_generation_id)  # reject wrong generations before DB access
        observed = self.state_port.read_principal_succession_custody(
            **identity, permit_ref=permit_ref, scopes={"project_state:read"})
        if observed.get("independent_committed_readback_verified") is not True:
            raise StateBindingError("succession-independent-committed-readback-required")
        record = observed.get("record")
        validate_principal_succession_custody_record(record)
        if record["identity"] != identity or record["permit"]["permit_id"] != permit_ref:
            raise StateBindingError("succession-reader-exact-scope-required")
        self._validate(record["permit"], identity, actor_generation_id, "READ_CUSTODY")
        return copy.deepcopy(record["permit"])
