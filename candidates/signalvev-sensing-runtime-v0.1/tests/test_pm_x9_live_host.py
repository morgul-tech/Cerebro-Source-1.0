"""Focused controls for authenticated PM/X9 current-session composition; never live proof."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "candidates" / "signalvev-client-v0.1" / "src"))
RUNTIME = REPO / "candidates" / "signalvev-sensing-runtime-v0.1"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(RUNTIME / "src"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp"))

from providers.pm_x9_live_host import (  # noqa: E402
    CurrentControlSession,
    LiveRuntimeBindings,
    LiveHostError,
    _require_distinct_current_sessions,
    _bind_pm_current_tuple,
    make_ports,
    read_current_control_session,
)
from signalvev_client.pm_x9 import PmX9Settings, PmX9Unbound  # noqa: E402
from providers.pm_owner_projection import PmProjectionBinding  # noqa: E402
from providers.pm_owner_sqlite import ContextPmCredentialPort, PmContextCustody  # noqa: E402


def _readback(role: str, session: str, *, project_revision: int = 7,
              persisted_revision: int | None = None, transport_session: str | None = None,
              persisted: bool = True) -> dict:
    transport_session = session if transport_session is None else transport_session
    caller = {
        "schema": "cerebro-authenticated-project-read-caller/v2",
        "tenant_ref": "TENANT", "workspace_ref": "WORKSPACE",
        "principal_ref": "SHARED_OAUTH_PRINCIPAL", "consumer_ref": "CHATGPT_REMOTE_MCP",
        "consumer_source": "SERVICE_BOUND", "project_ref": "PROJECT",
        "oauth_verified": True,
        "session_status": "CURRENT_PERSISTED_SESSION" if persisted else "NO_CURRENT_PERSISTED_SESSION",
        "transport_session": {"status": "AVAILABLE", "source": "openai/session",
                              "session_ref": transport_session, "authority": "TRANSPORT_METADATA_ONLY"},
        "persisted_session": ({"session_ref": session, "session_binding_id": "BINDING_" + role,
                               "session_revision": 3, "session_fingerprint": "a" * 64,
                               "project_revision": project_revision if persisted_revision is None else persisted_revision}
                              if persisted else None),
    }
    return {"structuredContent": {"project": {"project_ref": "PROJECT", "revision": project_revision},
                                  "authenticated_caller": caller}}


class CurrentSessionReadbackTests(unittest.TestCase):
    def test_current_control_session_positive(self):
        value = read_current_control_session(_readback("pm", "chatgpt:PM"), role="pm")
        self.assertEqual((value.role, value.session_ref, value.project_revision), ("pm", "chatgpt:PM", 7))
        self.assertEqual(value.as_tuple().session_binding_id, "BINDING_pm")

    def test_missing_persisted_mapping_returns_exact_required_fields(self):
        with self.assertRaises(LiveHostError) as cm:
            read_current_control_session(_readback("x9", "chatgpt:X9", persisted=False), role="x9")
        self.assertEqual(cm.exception.code, "CURRENT_PERSISTED_SESSION_REQUIRED")
        self.assertEqual(cm.exception.missing_fields, tuple(
            "x9.authenticated_caller.persisted_session." + name
            for name in ("session_ref", "session_binding_id", "session_revision", "session_fingerprint", "project_revision")
        ))

    def test_wrong_transport_session_refused(self):
        with self.assertRaises(LiveHostError) as cm:
            read_current_control_session(_readback("x9", "chatgpt:BOUND", transport_session="chatgpt:OTHER"), role="x9")
        self.assertEqual(cm.exception.code, "CURRENT_SESSION_READBACK_MISMATCH")
        self.assertIn("x9.persisted_session.session_ref!=transport_session.session_ref", cm.exception.mismatched_fields)

    def test_stale_project_revision_refused(self):
        with self.assertRaises(LiveHostError) as cm:
            read_current_control_session(_readback("pm", "chatgpt:PM", persisted_revision=6), role="pm")
        self.assertEqual(cm.exception.code, "CURRENT_SESSION_READBACK_MISMATCH")
        self.assertIn("pm.persisted_session.project_revision!=project.revision", cm.exception.mismatched_fields)

    def test_duplicate_pm_x9_session_refused_even_with_shared_oauth_principal(self):
        pm = read_current_control_session(_readback("pm", "chatgpt:SAME"), role="pm")
        x9 = read_current_control_session(_readback("x9", "chatgpt:SAME"), role="x9")
        with self.assertRaises(LiveHostError) as cm:
            _require_distinct_current_sessions(pm, x9)
        self.assertEqual(cm.exception.code, "PM_X9_DISTINCT_CURRENT_SESSIONS_REQUIRED")

    def test_distinct_current_sessions_on_same_revision_pass_pair_control(self):
        pm = read_current_control_session(_readback("pm", "chatgpt:PM"), role="pm")
        x9 = read_current_control_session(_readback("x9", "chatgpt:X9"), role="x9")
        _require_distinct_current_sessions(pm, x9)

    def test_host_binds_pm_provider_to_exact_fresh_current_tuple(self):
        current = read_current_control_session(_readback("pm", "chatgpt:PM"), role="pm")
        custody = PmContextCustody(
            "owner:pm", "audience:pm", current.tenant_ref, current.workspace_ref,
            current.project_ref, current.principal_ref, current.consumer_ref, current.session_ref,
            6, "OLD_BINDING", 2, "b" * 64, frozenset({"read_current"}))
        class Authenticator:
            def authenticate(self, headers, *, required_scope):
                raise AssertionError("helper must not authenticate")
        class State:
            def read_session(self, **kwargs):
                raise AssertionError("helper must not read provider state")
        credential_port = ContextPmCredentialPort(custody=custody, authenticator=Authenticator(),
            state_port=State(), credential_reader=lambda: "not-read")
        binding = PmProjectionBinding(
            expectation=SimpleNamespace(owner_ref="owner:pm", claim_ref="claim:pm", packet_ref="packet:pm",
                                        queue_ref="queue:pm", packet_sha256="a" * 64),
            receipt_ref="receipt:pm", audience="audience:pm", provider_ref="provider:pm",
            principal_ref=current.principal_ref, session_ref=current.session_ref,
            credentials=credential_port, projections=object(), credential_reader=lambda: "not-read", enabled=True)
        updated = _bind_pm_current_tuple(binding, current)
        self.assertIsNot(updated, binding)
        self.assertIsNot(updated.credentials, credential_port)
        self.assertEqual((updated.credentials.custody.project_revision,
                          updated.credentials.custody.session_binding_id,
                          updated.credentials.custody.session_revision,
                          updated.credentials.custody.session_fingerprint),
                         (current.project_revision, current.session_binding_id,
                          current.session_revision, current.session_fingerprint))
        self.assertIs(updated.credentials.authenticator, credential_port.authenticator)
        self.assertIs(updated.credentials.state_port, credential_port.state_port)

    def test_composition_returns_exact_missing_session_fields_before_other_ports(self):
        runtime = LiveRuntimeBindings(
            pm_control_readback=_readback("pm", "chatgpt:PM", persisted=False),
            x9_control_readback=_readback("x9", "chatgpt:X9", persisted=False),
            pm_projection_binding=None, x9_authenticator=None, x9_header_provider=None,
            x9_connection_factory=None, x9_configuration_provider=None, client_config=None,
            clock=None, enabled=True)
        settings = PmX9Settings(
            mode="PRODUCTION", owner_ref="owner:one", claim_ref="claim:one", packet_ref="packet:one",
            queue_ref="queue:one", packet_sha256="a" * 64, producer_principal="principal:pm",
            x9_principal="principal:x9", x9_session_ref="session:x9", attempt_ref="attempt:one")
        with self.assertRaises(PmX9Unbound) as cm:
            make_ports(settings, host_runtime=runtime)
        self.assertEqual(cm.exception.code, "PM_X9_CURRENT_HOST_COMPOSITION_UNAVAILABLE")
        self.assertIn("pm.authenticated_caller.persisted_session.session_ref",
                      cm.exception.diagnostics[0]["detail"])


if __name__ == "__main__":
    unittest.main()
