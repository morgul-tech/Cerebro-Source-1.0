"""Offline real MCP/HTTP serialization smoke for the protected BK07 read port."""

from __future__ import annotations

import base64
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from signalvev_client import local_runtime_provider_port as port
from signalvev_client import local_runtime_admission as admission


@unittest.skipUnless(importlib.util.find_spec("httpx2") and importlib.util.find_spec("mcp"),
                     "locked BK07 runtime dependencies are required")
class ProtectedTransportSmoke(unittest.TestCase):
    def test_real_token_and_mcp_import_serialization_without_network_or_secret(self):
        import httpx2

        body = {"iss": port.ISSUER, "aud": port.AUDIENCE,
                "azp": port.CLIENT_ID, "sub": port.CLIENT_ID + "@clients",
                "gty": "client-credentials", "scope": port.SCOPE,
                "iat": 1000, "exp": 1300}
        middle = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
        token = "header." + middle + ".signature"
        seen = []

        def token_post(url, *, json, timeout):
            self.assertEqual(url, port.ISSUER + "oauth/token")
            self.assertEqual(json["grant_type"], "client_credentials")
            self.assertEqual(json["scope"], port.SCOPE)
            self.assertEqual(json["client_secret"], "synthetic-secret")
            return httpx2.Response(200, json={"access_token": token,
                                               "token_type": "Bearer", "expires_in": 300})

        async def handler(request):
            self.assertEqual(str(request.url), port.MCP_URL)
            self.assertEqual(request.headers["authorization"], "Bearer " + token)
            message = json.loads(request.content)
            method = message.get("method")
            seen.append((request.method, method, message.get("params")))
            if request.method == "DELETE":
                return httpx2.Response(200)
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "offline-context", "version": "1"}}
            elif method == "tools/call":
                name = message["params"]["name"]
                self.assertIn(name, {port.READ_TOOL, port.RESOLVE_TOOL})
                self.assertEqual(message["params"]["arguments"],
                                 {} if name == port.READ_TOOL else {"event_id": "pm-event:synthetic"})
                self.assertEqual(set(message["params"]) - {"name", "arguments"}, {"_meta"})
                content = ({"schema": "cerebro-local-runtime-binding-read/v1",
                            "binding": {"session_ref": "local:synthetic"},
                            "currentness": "CURRENT", "repository_permission_required": False}
                           if name == port.READ_TOOL else
                           {"schema": "cerebro-signalvev-transport-resolution/v1",
                            "state": "HOLD", "event_id": "pm-event:synthetic"})
                result = {"content": [], "structuredContent": content, "isError": False}
            elif method == "tools/list":
                result = {"tools": [{"name": name,
                                     "inputSchema": {"type": "object", "properties": {}}}
                                    for name in (port.READ_TOOL, port.RESOLVE_TOOL)]}
            else:
                return httpx2.Response(202)
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                                              "result": result})

        real_client = httpx2.AsyncClient
        transport = httpx2.MockTransport(handler)

        def client_factory(*args, **kwargs):
            return real_client(*args, transport=transport, **kwargs)

        with (patch.object(Path, "read_bytes", return_value=b"synthetic-ciphertext"),
              patch.object(port, "_unprotect", return_value=b"synthetic-secret"),
              patch.object(httpx2, "post", side_effect=token_post),
              patch.object(httpx2, "AsyncClient", side_effect=client_factory),
              patch.object(admission, "read_admitted_local_session", return_value={}) as admitted):
            self.assertEqual(port.read_current_binding(), {"session_ref": "local:synthetic"})
            resolved = port.resolve_current_notice("pm-event:synthetic")
            self.assertEqual(resolved["structuredContent"]["state"], "HOLD")
            admitted.assert_called_once_with(profile_path=port.PROFILE_PATH, state_dir=port.STATE_DIR)
            with self.assertRaisesRegex(port.PortUnavailable, "BK07_EVENT_ID_INVALID"):
                port.resolve_current_notice("foreign-event")
            admitted.side_effect = RuntimeError("revoked")
            with self.assertRaisesRegex(port.PortUnavailable, "BK07_ADMITTED_RESOLVE_UNAVAILABLE"):
                port.resolve_current_notice("pm-event:revoked")
        self.assertEqual([method for _, method, _ in seen if method == "tools/call"],
                         ["tools/call", "tools/call"])


if __name__ == "__main__":
    unittest.main()
