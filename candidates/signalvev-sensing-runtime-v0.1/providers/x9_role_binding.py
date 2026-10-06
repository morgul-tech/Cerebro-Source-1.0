"""Host-owned, explicit role/tuple configuration for X9 receipt custody.

This module defines the seam only. Production values must come from an
independently reviewed host role-assignment source and the authenticated
project/session projection. OAuth principal names, consumer names, display
aliases, and request data never assign receiver or producer roles.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from adapters.x9_channel_ingress import CHANNEL, HEX64, ID

Role = Literal["receiver", "producer"]


@dataclass(frozen=True)
class ProviderSessionTuple:
    tenant_ref: str
    workspace_ref: str
    principal_ref: str
    consumer_ref: str
    session_ref: str
    project_ref: str
    project_revision: int
    session_binding_id: str
    session_revision: int
    session_fingerprint: str

    def validate(self) -> None:
        for value in (self.tenant_ref, self.workspace_ref, self.principal_ref,
                      self.consumer_ref, self.session_ref, self.project_ref, self.session_binding_id):
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
                raise ValueError("INVALID_PROVIDER_SESSION_TUPLE")
        if (type(self.project_revision) is not int or self.project_revision < 1
                or type(self.session_revision) is not int or self.session_revision < 1
                or not isinstance(self.session_fingerprint, str)
                or not HEX64.fullmatch(self.session_fingerprint)):
            raise ValueError("INVALID_PERSISTED_SESSION_BINDING")


@dataclass(frozen=True)
class RoleAssignment:
    role: Role
    assignment_ref: str
    source_ref: str
    identity: ProviderSessionTuple

    def validate(self) -> None:
        if self.role not in ("receiver", "producer"):
            raise ValueError("INVALID_CUSTODY_ROLE")
        if not isinstance(self.assignment_ref, str) or not ID.fullmatch(self.assignment_ref):
            raise ValueError("INVALID_ROLE_ASSIGNMENT_REF")
        if not isinstance(self.source_ref, str) or not self.source_ref.strip() or len(self.source_ref.encode("utf-8")) > 512:
            raise ValueError("INVALID_ROLE_ASSIGNMENT_SOURCE")
        self.identity.validate()


@dataclass(frozen=True)
class TrustedCustodyConfiguration:
    """One explicitly assigned receiver/producer pair from trusted host config."""

    config_ref: str
    channel: str
    receiver: RoleAssignment
    producer: RoleAssignment

    def validate(self) -> None:
        if not isinstance(self.config_ref, str) or not ID.fullmatch(self.config_ref):
            raise ValueError("INVALID_CUSTODY_CONFIG_REF")
        if self.channel != CHANNEL:
            raise ValueError("INVALID_CUSTODY_CHANNEL")
        self.receiver.validate()
        self.producer.validate()
        if self.receiver.role != "receiver" or self.producer.role != "producer":
            raise ValueError("CUSTODY_ROLE_PAIR_MISMATCH")
        receiver = self.receiver.identity
        producer = self.producer.identity
        if (receiver.tenant_ref, receiver.workspace_ref, receiver.project_ref, receiver.project_revision) != (
                producer.tenant_ref, producer.workspace_ref, producer.project_ref, producer.project_revision):
            raise ValueError("CUSTODY_PROJECT_TUPLE_MISMATCH")
        if receiver.session_ref == producer.session_ref:
            raise ValueError("CUSTODY_SESSIONS_NOT_DISTINCT")
        if self.receiver.assignment_ref == self.producer.assignment_ref:
            raise ValueError("CUSTODY_ASSIGNMENTS_NOT_DISTINCT")


class TrustedCustodyConfigurationPort(Protocol):
    """Host adapter for an independently governed, read-only role assignment."""

    def read_configuration(self, channel: str) -> TrustedCustodyConfiguration: ...


def read_trusted_configuration(port: TrustedCustodyConfigurationPort) -> TrustedCustodyConfiguration:
    if port is None or not callable(getattr(port, "read_configuration", None)):
        raise ValueError("TRUSTED_CUSTODY_CONFIGURATION_SOURCE_REQUIRED")
    config = port.read_configuration(CHANNEL)
    if not isinstance(config, TrustedCustodyConfiguration):
        raise ValueError("UNVERIFIED_CUSTODY_CONFIGURATION")
    config.validate()
    return config
