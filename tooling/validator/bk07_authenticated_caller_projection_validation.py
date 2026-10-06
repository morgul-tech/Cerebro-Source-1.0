"""Focused BK07 validation of the existing project-read caller projection."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "tooling" / "context"))

from control_context_state_port import StateBindingError
from control_context_tools import (ControlContextMcpTools, ControlContextToolAuthorizationError,
                                   McpToolCallContext, VerifiedMcpIdentity, tool_definitions)


class StatePort:
    def __init__(self, session=None, error=None):
        self.session = session
        self.error = error
        self.calls = []

    def read_project(self, **kwargs):
        self.calls.append(("read_project", kwargs))
        return {"project_ref": "P", "tenant_ref": "T", "workspace_ref": "W", "revision": 1}

    def read_session(self, **kwargs):
        self.calls.append(("read_session", kwargs))
        if self.error:
            raise self.error
        return self.session


def context(*, verified=True, meta=None):
    return McpToolCallContext(
        identity=VerifiedMcpIdentity("T", "W", "VERIFIED-PRINCIPAL",
                                     frozenset({"project_state:read"}), verified),
        request_meta={"openai/session": "S"} if meta is None else meta,
    )


def read(port, call_context):
    tools = object.__new__(ControlContextMcpTools)
    tools._state_port = port
    return tools.read_project_control_state({"project_ref": "P"}, call_context)["structuredContent"]


class AuthenticatedCallerProjection(unittest.TestCase):
    def test_unbound_session_keeps_verified_principal_without_claiming_session(self):
        port = StatePort(error=StateBindingError("control-session-not-bound"))
        result = read(port, context())
        caller = result["authenticated_caller"]
        self.assertEqual((caller["principal_ref"], caller["consumer_ref"]),
                         ("VERIFIED-PRINCIPAL", "CHATGPT_REMOTE_MCP"))
        self.assertEqual(caller["session_status"], "NO_CURRENT_PERSISTED_SESSION")
        self.assertIsNone(caller["persisted_session"])
        self.assertEqual([name for name, _ in port.calls], ["read_project", "read_session"])
        self.assertEqual(port.calls[1][1]["session_ref"], "chatgpt:S")

    def test_only_matching_persisted_current_session_is_projected(self):
        session = {"tenant_ref": "T", "workspace_ref": "W", "principal_ref": "VERIFIED-PRINCIPAL",
                   "consumer_ref": "CHATGPT_REMOTE_MCP", "session_ref": "chatgpt:S",
                   "project_ref": "P", "session_binding_id": "CSB-1", "session_revision": 2,
                   "fingerprint": "a" * 64, "project_revision": 1}
        caller = read(StatePort(session=session), context())["authenticated_caller"]
        self.assertEqual(caller["session_status"], "CURRENT_PERSISTED_SESSION")
        self.assertEqual(caller["persisted_session"]["session_binding_id"], "CSB-1")
        self.assertEqual(caller["persisted_session"]["session_ref"], "chatgpt:S")

    def test_mismatched_persisted_identity_fails_closed(self):
        session = {"tenant_ref": "T", "workspace_ref": "W", "principal_ref": "OTHER",
                   "consumer_ref": "CHATGPT_REMOTE_MCP", "session_ref": "chatgpt:S",
                   "project_ref": "P", "project_revision": 1}
        with self.assertRaises(ControlContextToolAuthorizationError):
            read(StatePort(session=session), context())

    def test_other_project_session_preserves_authorized_project_read(self):
        session = {"tenant_ref": "T", "workspace_ref": "W", "principal_ref": "VERIFIED-PRINCIPAL",
                   "consumer_ref": "CHATGPT_REMOTE_MCP", "session_ref": "chatgpt:S",
                   "project_ref": "P1", "project_revision": 1}
        result = read(StatePort(session=session), context())
        self.assertEqual(result["project"]["project_ref"], "P")
        self.assertEqual(result["authenticated_caller"]["session_status"],
                         "PERSISTED_SESSION_BOUND_TO_OTHER_PROJECT")
        self.assertIsNone(result["authenticated_caller"]["persisted_session"])

    def test_mismatched_project_revision_fails_closed(self):
        session = {"tenant_ref": "T", "workspace_ref": "W", "principal_ref": "VERIFIED-PRINCIPAL",
                   "consumer_ref": "CHATGPT_REMOTE_MCP", "session_ref": "chatgpt:S",
                   "project_ref": "P", "project_revision": 2}
        with self.assertRaises(ControlContextToolAuthorizationError):
            read(StatePort(session=session), context())

    def test_no_host_session_metadata_is_not_a_bound_session(self):
        port = StatePort()
        caller = read(port, context(meta={}))["authenticated_caller"]
        self.assertEqual(caller["session_status"], "NO_HOST_SESSION_METADATA")
        self.assertEqual([name for name, _ in port.calls], ["read_project"])

    def test_unverified_context_cannot_project_any_identity(self):
        port = StatePort()
        with self.assertRaises(ControlContextToolAuthorizationError):
            read(port, context(verified=False))
        self.assertEqual(port.calls, [])

    def test_existing_tool_keeps_the_same_read_scope_and_no_identity_inputs(self):
        definition = next(item for item in tool_definitions()
                          if item["name"] == "read_project_control_state")
        self.assertEqual(definition["securitySchemes"],
                         [{"type": "oauth2", "scopes": ["project_state:read"]}])
        self.assertEqual(set(definition["inputSchema"]["properties"]), {"project_ref"})
        self.assertIn("authenticated_caller", definition["outputSchema"]["required"])


if __name__ == "__main__":
    unittest.main()
