"""Compose the UNCHANGED selected Context service objects over the fixture cluster (no MCP SDK / HTTP hop).

``ControlContextRemoteMcpService.invoke`` is the real authentication + dispatch boundary of the Context service:
bearer verification, scope/issuer/audience/expiry checks, request-meta session filtering, then tool dispatch into
the host objects. The streamable-HTTP binding (mcp==2.0.0 / uvicorn) around it is NOT exercised here.
"""
from __future__ import annotations

from typing import Any

from .context_pg import ISSUER, RESOURCE, ContextPostgres, FixtureTokens, context_imports


class _NeverUsedResolutionVerifier:
    """Constructor requirement of ControlContextMcpTools; no tool in this batch reaches it."""

    def verify(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("RESOLUTION_ATTESTATION_NOT_PART_OF_THIS_FIXTURE")


class ContextServiceFixture:
    def __init__(self, pg: ContextPostgres, tokens: FixtureTokens, *, d1_outbox: bool = True,
                 runtime_dsn_role: str = "runtime") -> None:
        context_imports(pg.context_root)
        from control_context_remote_service import ControlContextRemoteMcpService, RemoteMcpServiceConfig
        from control_context_state_postgres import PostgresControlContextStatePort, make_psycopg_connection_factory
        from control_context_tools import ControlContextMcpTools
        from pm_owner_atomic import PmOwnerAssignment, PostgresPmOwner
        from x9_receiver_host import X9ReceiverHost, configuration_from_mapping
        from x9_producer_host import X9ProducerHost
        self.pg, self.tokens = pg, tokens
        self.factory = make_psycopg_connection_factory(pg.dsn(runtime_dsn_role), connect_timeout_seconds=5,
                                                       application_name="fixture-context-runtime")
        self.state_port = PostgresControlContextStatePort(self.factory)
        self.pm_assignment = PmOwnerAssignment.from_mapping(pg.pm_assignment_mapping())
        self.x9_config = configuration_from_mapping(pg.x9_configuration_mapping())
        self.d1 = None
        if d1_outbox:
            from pm_owner_d1_outbox import D1PublisherAssignment, PmOwnerD1Outbox
            self.d1 = PmOwnerD1Outbox(state_port=self.state_port, pm_assignment=self.pm_assignment,
                                      publisher_assignment=D1PublisherAssignment.from_mapping(
                                          pg.publisher_assignment_mapping()),
                                      x9_configuration=self.x9_config)
        extra = {"d1_outbox": self.d1} if self.d1 is not None else {}
        self.pm_owner = PostgresPmOwner(state_port=self.state_port, assignment=self.pm_assignment, **extra)
        self.tools = ControlContextMcpTools(self.state_port, _NeverUsedResolutionVerifier(), pm_owner=self.pm_owner)
        from control_context_remote_service import LocalRuntimeOAuthBinding
        from .context_pg import PROJECT, TENANT, WORKSPACE
        # The existing local-runtime M2M credential (read/resolve-only); configured here so the authenticator's
        # "runtime credential is resolve-only" rule is exercised against the new D1 operations.
        self.runtime_client_id = "syn-runtime-client"
        runtime = LocalRuntimeOAuthBinding(issuer=ISSUER, client_id=self.runtime_client_id, tenant_ref=TENANT,
                                           workspace_ref=WORKSPACE, project_ref=PROJECT,
                                           principal_ref=self.runtime_client_id + "@clients",
                                           runtime_audience=RESOURCE + "/bk07-runtime", enabled=False)
        config = RemoteMcpServiceConfig(resource=RESOURCE, authorization_servers=(ISSUER,),
                                        resource_documentation=RESOURCE + "/docs", local_runtime_binding=runtime)
        self.service = ControlContextRemoteMcpService(config=config, tools=self.tools,
                                                      token_verifier=tokens.verifier(),
                                                      readiness_probe=lambda: True)
        auth = self.service._authenticator
        self.tools.bind_x9_receiver(X9ReceiverHost(config=self.x9_config, authenticator=auth,
                                                   connection_factory=self.factory,
                                                   pm_owner_assignment=self.pm_assignment))
        self.tools.bind_x9_producer(X9ProducerHost(config=self.x9_config, pm_owner_assignment=self.pm_assignment,
                                                   authenticator=auth, connection_factory=self.factory))
        if self.d1 is not None:
            from pm_owner_d1_outbox import D1OutboxHost
            self.tools.bind_pm_owner_d1_outbox(D1OutboxHost(outbox=self.d1, authenticator=auth,
                                                            connection_factory=self.factory))

    def invoke(self, who: str, tool: str, args: dict[str, Any], *, scopes: str | None = None,
               session: str | None = None, token: str | None = None) -> dict[str, Any]:
        from .context_pg import PRINCIPALS, SESSIONS
        scopes = scopes or "project_state:read project_state:transition"
        token = token or self.tokens.token(PRINCIPALS[who], scopes)
        meta = {"openai/session": (session or SESSIONS[who]).removeprefix("chatgpt:")}
        return self.service.invoke(tool_name=tool, args=args, headers={"authorization": "Bearer " + token},
                                   request_meta=meta)


class ContextToolClient:
    """Thin Source-side client boundary: one authenticated principal/session, one versioned Context operation set.
    In this fixture the transport is the in-process service.invoke; in deployment it is the MCP HTTP binding."""

    D1_SERVICE_OWNED_CONTEXT_CLIENT = True

    def __init__(self, fixture: ContextServiceFixture, who: str, *, token: str | None = None,
                 scopes: str | None = None) -> None:
        self._fx, self.who, self._token, self._scopes = fixture, who, token, scopes
        self.calls = 0
        self.fail_next: list[str] = []          # synthetic client-boundary fault: tool names to refuse once

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if tool in self.fail_next:
            self.fail_next.remove(tool)
            raise ConnectionError("SYNTHETIC_CONTEXT_TRANSPORT_FAILURE")
        result = self._fx.invoke(self.who, tool, args, token=self._token, scopes=self._scopes)
        if result.get("isError"):
            raise PermissionError("CONTEXT_AUTHENTICATION_OR_AUTHORIZATION_REFUSED")
        return result["structuredContent"]
