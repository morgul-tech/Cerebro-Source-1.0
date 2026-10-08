#!/usr/bin/env python3
"""Targeted regression checks for the bounded Boot birth bridge."""

from __future__ import annotations

import sys
import json
from pathlib import Path
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "tooling" / "context")]

from boot_birth_source import (  # noqa: E402
    BootBirthSourceError, verify_current_method, verify_role_method_projection,
)
from control_context_state_port import InMemoryControlContextStatePort  # noqa: E402
from control_context_tools import (  # noqa: E402
    ControlContextMcpTools, ControlContextToolAuthorizationError,
    ControlContextToolError, HmacControlResolutionAttestor, McpToolCallContext,
    VerifiedMcpIdentity,
)


HEAD = "fc62b1336ef86459a87b96b88f11441a207284e7"
ATTEMPT = "CEREBRO-BOOT-20260930004220Z-2B852369"


def source(_head: str) -> dict[str, str | int]:
    if _head != HEAD:
        raise BootBirthSourceError("source-main-head-changed")
    return {
        "source_revision": HEAD,
        "method_ref": "CURRENT_CIVILIZATION_METHOD_PROFILE",
        "method_version": "1.0",
        "method_fingerprint": "a" * 64,
        "provider_frontier_ref": "GITHUB_SOURCE_MAIN_" + HEAD,
        "provider_revision": 1790668580,
    }


def context(scope: str = "project_state:transition", *, verified: bool = True) -> McpToolCallContext:
    return McpToolCallContext(
        identity=VerifiedMcpIdentity(
            tenant_ref="TENANT", workspace_ref="WORKSPACE", principal_ref="HUMAN",
            scopes=frozenset({scope}), token_verified=verified,
        ),
        request_meta={"openai/session": "BOOT-SESSION"},
    )


def denied(call, error: type[Exception]) -> bool:
    try:
        call()
    except error:
        return True
    return False


def main() -> int:
    attestor = HmacControlResolutionAttestor(key_id="TEST", secret=b"not-a-production-secret-at-least-32-bytes")
    state = InMemoryControlContextStatePort()
    tools = ControlContextMcpTools(
        state, attestor, boot_birth_source_verifier=source,
        boot_birth_attestation_issuer=attestor,
    )
    args = {"boot_attempt_id": ATTEMPT, "source_revision": HEAD}
    checks = {
        "missing-server-issuer-denies": denied(
            lambda: ControlContextMcpTools(state, attestor, boot_birth_source_verifier=source).dispatch(
                "create_ready_unbound_boot_generation", args, context()),
            ControlContextToolAuthorizationError,
        ),
        "read-only-token-denies": denied(
            lambda: tools.dispatch("create_ready_unbound_boot_generation", args, context("project_state:read")),
            ControlContextToolAuthorizationError,
        ),
        "unverified-token-denies": denied(
            lambda: tools.dispatch("create_ready_unbound_boot_generation", args, context(verified=False)),
            ControlContextToolAuthorizationError,
        ),
        "identity-injection-denies": denied(
            lambda: tools.dispatch("create_ready_unbound_boot_generation", {**args, "principal_ref": "AI"}, context()),
            ControlContextToolAuthorizationError,
        ),
        "extra-control-field-denies": denied(
            lambda: tools.dispatch("create_ready_unbound_boot_generation", {**args, "authority": "MCP"}, context()),
            ControlContextToolError,
        ),
        "stale-source-denies": denied(
            lambda: tools.dispatch("create_ready_unbound_boot_generation", {**args, "source_revision": "0" * 40}, context()),
            BootBirthSourceError,
        ),
    }
    def pinned_fetch(url: str) -> bytes:
        if url.endswith("/commits/main"):
            return json.dumps({"sha": HEAD, "commit": {"committer": {"date": "2026-09-29T00:00:00Z"}}}).encode()
        prefix = f"https://raw.githubusercontent.com/morgul-tech/Cerebro-Source-1.0/{HEAD}/"
        if not url.startswith(prefix):
            raise AssertionError("unexpected-provider-url")
        return (ROOT / url[len(prefix):]).read_bytes().replace(bytes([13, 10]), bytes([10]))
    verified = verify_current_method(HEAD, fetch=pinned_fetch)
    checks["pinned-method-fingerprint-matches-source-contract"] = (
        verified["method_fingerprint"] == "ed4407a55c0c680f80b405c821e109439f6d3d0ebbec37a5eac1fba5ab55b04d"
    )
    principal_projection = verify_role_method_projection(HEAD, "PRINCIPAL", fetch=pinned_fetch)
    checks["principal-role-projection-binds-stambok-source-ref"] = (
        principal_projection["method_profile_fingerprint"] == verified["method_fingerprint"]
        and principal_projection["contract_refs"] == ["standards/principalstambok.yaml", "standards/development/implementation-learning-loop.yaml"]
        and principal_projection["authority"] == "NONE"
    )
    worker_fetches: list[str] = []
    def tracked_worker_fetch(url: str) -> bytes:
        worker_fetches.append(url)
        return pinned_fetch(url)
    worker_projection = verify_role_method_projection(HEAD, "WORKER", fetch=tracked_worker_fetch)
    checks["worker-role-projection-does-not-inherit-principal-contract"] = (
        worker_projection["contract_refs"] == []
        and worker_projection["method_profile_fingerprint"] == verified["method_fingerprint"]
    )
    checks["worker-role-projection-performs-zero-principalstambok-reads"] = (
        not any(url.endswith("/standards/principalstambok.yaml") for url in worker_fetches)
    )
    def missing_principal_contract_fetch(url: str) -> bytes:
        if url.endswith("/standards/principalstambok.yaml"):
            raise FileNotFoundError("principalstambok-unavailable")
        return pinned_fetch(url)
    checks["principal-role-projection-rejects-unavailable-principalstambok"] = denied(
        lambda: verify_role_method_projection(HEAD, "PRINCIPAL", fetch=missing_principal_contract_fetch),
        BootBirthSourceError,
    )
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<id>tag:github.com,2008:/morgul-tech/Cerebro-Source-1.0/commits/main</id>'
        f'<entry><id>tag:github.com,2008:Grit::Commit/{HEAD}</id>'
        '<updated>2026-09-29T00:00:00Z</updated></entry></feed>'
    ).encode()
    def rate_limited_fetch(url: str) -> bytes:
        if url.endswith("/commits/main"):
            raise HTTPError(url, 403, "rate limit exceeded", {}, None)
        if url.endswith("/commits/main.atom"):
            return atom
        return pinned_fetch(url)
    checks["github-api-rate-limit-uses-current-atom-head"] = (
        verify_current_method(HEAD, fetch=rate_limited_fetch) == verified
    )
    first = tools.dispatch("create_ready_unbound_boot_generation", args, context())["structuredContent"]
    second = tools.dispatch("create_ready_unbound_boot_generation", args, context())["structuredContent"]
    generation = first["pre_role_generation"]
    checks["durable-ready-unbound-no-role-or-authority"] = (
        generation["lifecycle"] == "READY_UNBOUND"
        and generation["authority_envelope"]["role"] is None
        and generation["authority_envelope"]["grants_live_authority"] is False
        and generation["ready_unbound_receipt"]["post_state_readback_verified"] is True
    )
    checks["same-attempt-idempotent"] = (
        first["ready_unbound_receipt"] == second["ready_unbound_receipt"]
        and second["pre_role_generation"]["revision"] == 2
    )
    checks["no-secret-or-attestation-in-output"] = (
        "control_resolution_attestation" not in str(first)
        and "not-a-production-secret" not in str(first)
    )
    for name, passed in checks.items():
        print(("PASS" if passed else "FAIL") + " " + name)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
