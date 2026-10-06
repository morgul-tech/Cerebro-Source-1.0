"""Explicit fail-closed PM/X9 host composition over the existing Source providers.

This adapter does not start NATS, send a PM event, install the X9 schema, bind a role,
or create a control session. A live runner must inject its current authenticated
control-session readbacks and existing provider/credential objects on each invocation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping

from signalvev_client.config import ClientConfig
from signalvev_client.pm_x9 import PmX9HostPorts, PmX9Settings, PmX9Unbound

from .pm_owner_projection import PmProjectionBinding
from .x9_role_binding import (ProviderSessionTuple, TrustedCustodyConfigurationPort,
                              read_trusted_configuration)


@dataclass(frozen=True)
class CurrentControlSession:
    """Provider-authenticated identity plus its current persisted session binding."""

    role: str
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

    def as_tuple(self) -> ProviderSessionTuple:
        value = ProviderSessionTuple(
            tenant_ref=self.tenant_ref, workspace_ref=self.workspace_ref,
            principal_ref=self.principal_ref, consumer_ref=self.consumer_ref,
            session_ref=self.session_ref, project_ref=self.project_ref,
            project_revision=self.project_revision, session_binding_id=self.session_binding_id,
            session_revision=self.session_revision, session_fingerprint=self.session_fingerprint,
        )
        value.validate()
        return value


@dataclass(frozen=True)
class LiveRuntimeBindings:
    """Objects already owned by the Source host; no credentials are stored here."""

    pm_control_readback: Mapping[str, Any]
    x9_control_readback: Mapping[str, Any]
    pm_projection_binding: PmProjectionBinding
    x9_authenticator: Any
    x9_header_provider: Callable[[], Mapping[str, str]]
    x9_connection_factory: Callable[[], Any]
    x9_configuration_provider: TrustedCustodyConfigurationPort
    client_config: ClientConfig
    clock: Callable[[], Any]
    connect_fn: Callable[..., Any] | None = None
    enabled: bool = False


class LiveHostError(ValueError):
    def __init__(self, code: str, missing_fields: tuple[str, ...] = (), mismatched_fields: tuple[str, ...] = ()):
        self.code = code
        self.missing_fields = missing_fields
        self.mismatched_fields = mismatched_fields
        super().__init__(code)

    def diagnostic(self) -> dict[str, str]:
        detail = self.code
        if self.missing_fields:
            detail += ";MISSING=" + ",".join(self.missing_fields)
        if self.mismatched_fields:
            detail += ";MISMATCH=" + ",".join(self.mismatched_fields)
        return {"port": "current_control_session", "status": "MISSING", "detail": detail}


def _unbound(error: LiveHostError) -> PmX9Unbound:
    return PmX9Unbound("PM_X9_CURRENT_HOST_COMPOSITION_UNAVAILABLE", [error.diagnostic()])


def read_current_control_session(readback: Mapping[str, Any], *, role: str) -> CurrentControlSession:
    """Adapt one existing read_project_control_state result; transport metadata alone is never enough."""
    if not isinstance(readback, Mapping):
        raise LiveHostError("CONTROL_SESSION_READBACK_REQUIRED", (role + ".structuredContent",))
    result = readback.get("structuredContent", readback)
    if not isinstance(result, Mapping):
        raise LiveHostError("CONTROL_SESSION_READBACK_INVALID", (role + ".structuredContent",))
    caller = result.get("authenticated_caller")
    project = result.get("project")
    if not isinstance(caller, Mapping):
        raise LiveHostError("AUTHENTICATED_CALLER_REQUIRED", (role + ".authenticated_caller",))
    if not isinstance(project, Mapping):
        raise LiveHostError("PROJECT_REVISION_REQUIRED", (role + ".project.revision",))
    if caller.get("schema") != "cerebro-authenticated-project-read-caller/v2" or caller.get("oauth_verified") is not True:
        raise LiveHostError("CURRENT_OAUTH_VERIFICATION_REQUIRED", (role + ".authenticated_caller.oauth_verified",))
    if caller.get("consumer_source") != "SERVICE_BOUND":
        raise LiveHostError("CONSUMER_SOURCE_NOT_PROVIDER_BOUND", (role + ".authenticated_caller.consumer_source",))
    transport = caller.get("transport_session")
    if not isinstance(transport, Mapping) or transport.get("status") != "AVAILABLE":
        raise LiveHostError("CURRENT_TRANSPORT_SESSION_REQUIRED", (role + ".authenticated_caller.transport_session",))
    if transport.get("authority") != "TRANSPORT_METADATA_ONLY":
        raise LiveHostError("TRANSPORT_AUTHORITY_CEILING_MISMATCH", (role + ".transport_session.authority",))
    persisted = caller.get("persisted_session")
    if not isinstance(persisted, Mapping) or caller.get("session_status") != "CURRENT_PERSISTED_SESSION":
        raise LiveHostError("CURRENT_PERSISTED_SESSION_REQUIRED", tuple(
            role + ".authenticated_caller.persisted_session." + name
            for name in ("session_ref", "session_binding_id", "session_revision", "session_fingerprint", "project_revision")
        ))
    project_revision = project.get("revision")
    fields = {
        "tenant_ref": caller.get("tenant_ref"),
        "workspace_ref": caller.get("workspace_ref"),
        "principal_ref": caller.get("principal_ref"),
        "consumer_ref": caller.get("consumer_ref"),
        "session_ref": persisted.get("session_ref"),
        "project_ref": project.get("project_ref"),
        "project_revision": project_revision,
        "session_binding_id": persisted.get("session_binding_id"),
        "session_revision": persisted.get("session_revision"),
        "session_fingerprint": persisted.get("session_fingerprint"),
    }
    missing = tuple(role + "." + key for key, value in fields.items()
                    if value is None or (isinstance(value, str) and not value.strip()))
    if missing:
        raise LiveHostError("PERSISTED_SESSION_FIELD_MISSING", missing)
    mismatches = []
    if fields["session_ref"] != transport.get("session_ref"):
        mismatches.append(role + ".persisted_session.session_ref!=transport_session.session_ref")
    if persisted.get("project_revision") != project_revision:
        mismatches.append(role + ".persisted_session.project_revision!=project.revision")
    if type(project_revision) is not int or project_revision < 1:
        mismatches.append(role + ".project.revision_invalid")
    if type(fields["session_revision"]) is not int or fields["session_revision"] < 1:
        mismatches.append(role + ".persisted_session.session_revision_invalid")
    fingerprint = fields["session_fingerprint"]
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        mismatches.append(role + ".persisted_session.session_fingerprint_invalid")
    if mismatches:
        raise LiveHostError("CURRENT_SESSION_READBACK_MISMATCH", mismatched_fields=tuple(mismatches))
    value = CurrentControlSession(role=role, **fields)
    value.as_tuple()
    return value


def _tuple_mismatches(expected: ProviderSessionTuple, actual: CurrentControlSession, label: str) -> tuple[str, ...]:
    observed = actual.as_tuple()
    return tuple(label + "." + name for name in (
        "tenant_ref", "workspace_ref", "principal_ref", "consumer_ref", "session_ref", "project_ref",
        "project_revision", "session_binding_id", "session_revision", "session_fingerprint",
    ) if getattr(expected, name) != getattr(observed, name))


def _require_distinct_current_sessions(pm: CurrentControlSession, x9: CurrentControlSession) -> None:
    if pm.session_ref == x9.session_ref:
        raise LiveHostError("PM_X9_DISTINCT_CURRENT_SESSIONS_REQUIRED", mismatched_fields=("pm.session_ref==x9.session_ref",))
    mismatches = tuple("project." + name for name in ("tenant_ref", "workspace_ref", "project_ref", "project_revision")
                       if getattr(pm, name) != getattr(x9, name))
    if mismatches:
        raise LiveHostError("PM_X9_CURRENT_PROJECT_REVISION_MISMATCH", mismatched_fields=mismatches)


def _bind_pm_current_tuple(binding: PmProjectionBinding,
                           current: CurrentControlSession) -> PmProjectionBinding:
    """Pin PM's existing per-operation credential port to the freshly read exact session tuple."""
    from .pm_owner_sqlite import ContextPmCredentialPort

    credential_port = binding.credentials
    if type(credential_port) is not ContextPmCredentialPort:
        raise LiveHostError("PM_CONTEXT_CURRENTNESS_PORT_REQUIRED", ("pm_projection_binding.credentials",))
    custody = credential_port.custody
    stable_fields = ("tenant_ref", "workspace_ref", "project_ref", "principal_ref", "consumer_ref", "session_ref")
    mismatches = tuple("pm_context_custody." + name for name in stable_fields
                       if getattr(custody, name) != getattr(current, name))
    if mismatches:
        raise LiveHostError("PM_CONTEXT_CUSTODY_IDENTITY_MISMATCH", mismatched_fields=mismatches)
    exact_custody = replace(
        custody,
        project_revision=current.project_revision,
        session_binding_id=current.session_binding_id,
        session_revision=current.session_revision,
        session_fingerprint=current.session_fingerprint,
    )
    exact_port = ContextPmCredentialPort(
        custody=exact_custody, authenticator=credential_port.authenticator,
        state_port=credential_port.state_port, credential_reader=credential_port.credential_reader,
    )
    return replace(binding, credentials=exact_port)


def make_ports(settings: PmX9Settings, *, host_runtime: LiveRuntimeBindings | None = None) -> PmX9HostPorts:
    """Build the existing production ports; caller remains responsible for explicit operation start."""
    try:
        if not isinstance(settings, PmX9Settings):
            raise LiveHostError("PM_X9_SETTINGS_REQUIRED", ("settings",))
        if type(host_runtime) is not LiveRuntimeBindings or host_runtime.enabled is not True:
            raise LiveHostError("EXPLICIT_CURRENT_HOST_RUNTIME_REQUIRED", ("host_runtime.enabled",))
        runtime = host_runtime
        pm_current = read_current_control_session(runtime.pm_control_readback, role="pm")
        x9_current = read_current_control_session(runtime.x9_control_readback, role="x9")
        _require_distinct_current_sessions(pm_current, x9_current)

        config = read_trusted_configuration(runtime.x9_configuration_provider)
        role_mismatches = (
            *_tuple_mismatches(config.producer.identity, pm_current, "trusted_producer"),
            *_tuple_mismatches(config.receiver.identity, x9_current, "trusted_receiver"),
        )
        if role_mismatches:
            raise LiveHostError("TRUSTED_ROLE_CURRENT_SESSION_MISMATCH", mismatched_fields=role_mismatches)

        pm_binding = runtime.pm_projection_binding
        if type(pm_binding) is not PmProjectionBinding or pm_binding.enabled is not True:
            raise LiveHostError("PM_OWNER_PROVIDER_BINDING_REQUIRED", ("pm_projection_binding.enabled",))
        expectation = pm_binding.expectation
        settings_fields = ("owner_ref", "claim_ref", "packet_ref", "queue_ref", "packet_sha256")
        mismatches = tuple("settings." + name + "!=pm_projection_binding.expectation"
                           for name in settings_fields if getattr(settings, name) != getattr(expectation, name))
        mismatches += _tuple_mismatches(config.producer.identity, pm_current, "pm_producer")
        if (settings.producer_principal != pm_current.principal_ref
                or settings.x9_principal != x9_current.principal_ref
                or settings.x9_session_ref != x9_current.session_ref):
            mismatches += ("settings.principal_or_session!=trusted_current_provider_tuple",)
        if (pm_binding.principal_ref, pm_binding.session_ref) != (pm_current.principal_ref, pm_current.session_ref):
            mismatches += ("pm_projection_binding.current_principal_or_session",)
        if mismatches:
            raise LiveHostError("PM_X9_HOST_TUPLE_MISMATCH", mismatched_fields=mismatches)
        if not isinstance(runtime.client_config, ClientConfig):
            raise LiveHostError("EXISTING_NATS_CLIENT_CONFIG_REQUIRED", ("client_config",))
        if not callable(runtime.clock):
            raise LiveHostError("EXISTING_HOST_CLOCK_REQUIRED", ("clock",))
        if (not callable(runtime.x9_header_provider) or not callable(runtime.x9_connection_factory)
                or runtime.x9_authenticator is None):
            raise LiveHostError("EXISTING_X9_AUTH_AND_POSTGRES_PORTS_REQUIRED", (
                "x9_authenticator", "x9_header_provider", "x9_connection_factory"))

        pm_binding = _bind_pm_current_tuple(pm_binding, pm_current)

        # Keep the readback/preflight path importable without initializing the host's live
        # Postgres/OAuth service modules. Those providers are needed only after preflight passes.
        from .pm_owner_projection import create_pm_source_port
        from .x9_postgres_custody import PostgresPMProducerChannel, PostgresX9SessionAPI
        from .x9_session_channel import X9SessionChannelPort

        pm_port = create_pm_source_port(pm_binding)
        receiver_api = PostgresX9SessionAPI(
            authenticator=runtime.x9_authenticator, header_provider=runtime.x9_header_provider,
            connection_factory=runtime.x9_connection_factory,
            configuration_provider=runtime.x9_configuration_provider, enabled=True, role="receiver")
        producer_api = PostgresX9SessionAPI(
            authenticator=runtime.x9_authenticator, header_provider=runtime.x9_header_provider,
            connection_factory=runtime.x9_connection_factory,
            configuration_provider=runtime.x9_configuration_provider, enabled=True, role="producer")
        ports = PmX9HostPorts(
            pm_port=pm_port,
            producer_channel=PostgresPMProducerChannel(producer_api),
            x9_channel=X9SessionChannelPort(api=receiver_api, enabled=True),
            clock=runtime.clock, client_config=runtime.client_config,
            connect_fn=runtime.connect_fn, pm_reread_port=pm_port,
        )
        return ports
    except LiveHostError as exc:
        raise _unbound(exc) from None
    except PmX9Unbound:
        raise
    except Exception as exc:
        # Never surface credentials, DSNs, request headers, or provider exception text.
        raise PmX9Unbound("PM_X9_HOST_COMPOSITION_FAILED", [
            {"port": "live_host", "status": "MISSING", "detail": type(exc).__name__}
        ]) from None
