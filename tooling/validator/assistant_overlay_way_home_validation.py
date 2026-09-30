#!/usr/bin/env python3
"""Verify that an existing Boot generation can attach an Assistant role without a client seal."""

from __future__ import annotations

import sys
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "mcp"), str(ROOT / "tooling" / "context")]

from control_context_state_port import InMemoryControlContextStatePort, StateBindingError  # noqa: E402
from boot_birth_source import (  # noqa: E402
    BootBirthSourceError, HEAD_URL, COMPARE_URL, REPOSITORY,
    verify_current_method, verify_existing_birth_method_continuity,
)
from control_context_tools import (  # noqa: E402
    ControlContextMcpTools, ControlContextToolAuthorizationError,
    ControlContextToolError, HmacControlResolutionAttestor, McpToolCallContext,
    VerifiedMcpIdentity,
)


HEAD = "f" * 40
ADVANCED_HEAD = "e" * 40
ATTEMPT = "CEREBRO-BOOT-ASS-20260930T2206-GJENKLANG"
TOOL = "resume_ready_unbound_assistant_overlay"


def source(head: str) -> dict[str, str | int]:
    if head != HEAD:
        raise ValueError("source-main-head-changed")
    fingerprint = verify_current_method(HEAD, fetch=provider(HEAD))["method_fingerprint"]
    return {
        "source_revision": HEAD,
        "method_ref": "CURRENT_CIVILIZATION_METHOD_PROFILE",
        "method_version": "1.0",
        "method_fingerprint": fingerprint,
        "provider_frontier_ref": "GITHUB_SOURCE_MAIN_" + HEAD,
        "provider_revision": 1,
    }


def continuity(head: str, method_fingerprint: str) -> dict[str, str | int | bool]:
    return verify_existing_birth_method_continuity(
        head, method_fingerprint, fetch=provider(ADVANCED_HEAD),
    )


def provider(current_head: str, *, changed_method: bool = False, ancestor: bool = True):
    raw_prefix = f"https://raw.githubusercontent.com/{REPOSITORY}/"

    def fetch(url: str) -> bytes:
        if url == HEAD_URL:
            return json.dumps({
                "sha": current_head,
                "commit": {"committer": {"date": "2026-09-30T00:00:00Z"}},
            }).encode()
        if url == f"{COMPARE_URL}/{HEAD}...{ADVANCED_HEAD}":
            return json.dumps({
                "status": "ahead" if ancestor else "diverged",
                "base_commit": {"sha": HEAD},
                "merge_base_commit": {"sha": HEAD if ancestor else "0" * 40},
                "head_commit": {"sha": ADVANCED_HEAD},
            }).encode()
        if url.startswith(raw_prefix):
            revision, path = url[len(raw_prefix):].split("/", 1)
            if revision not in {HEAD, ADVANCED_HEAD}:
                raise AssertionError("unexpected-source-revision")
            data = (ROOT / path).read_bytes().replace(b"\r\n", b"\n")
            if changed_method and revision == ADVANCED_HEAD and path == "mcp/constitution.yaml":
                data += b"\n# changed method contract\n"
            return data
        raise AssertionError("unexpected-provider-url")

    return fetch


def context(scope: str = "project_state:transition") -> McpToolCallContext:
    return McpToolCallContext(
        identity=VerifiedMcpIdentity(
            tenant_ref="TENANT", workspace_ref="WORKSPACE", principal_ref="HUMAN",
            scopes=frozenset({scope}), token_verified=True,
        ),
        request_meta={"openai/session": "BOOT-SESSION"},
    )


class BoundAssistantResolver:
    calls = 0

    def resolve(self, *, pre_role_generation, verified_source, identity, session_ref):
        self.calls += 1
        assert identity.principal_ref == "HUMAN"
        assert session_ref == context().session_ref()
        assert verified_source["source_revision"] == HEAD
        assert verified_source["current_source_revision"] == ADVANCED_HEAD
        return {
            "result": "ALLOW",
            "decision_ref": "AD1-GJENKLANG-CONTROL-001",
            "pre_role_fingerprint": pre_role_generation["fingerprint"],
            "ready_unbound_receipt_fingerprint": pre_role_generation["ready_unbound_receipt"]["receipt_fingerprint"],
            "overlay_payload": {
                "generation_ref": pre_role_generation["generation_ref"],
                "expected_revision": pre_role_generation["revision"],
                "overlay_ref": "ASSISTANT-OVERLAY-076EAE0F",
                "actor_ref": "ASSISTANT_AD1",
                "role": "ASSISTANT",
                "actor_generation_ref": "ASSISTANT_AD1_076EAE0F",
                "source_revision": HEAD,
            },
        }


class BadResolver(BoundAssistantResolver):
    def __init__(self, field: str, value: str):
        self.field = field
        self.value = value

    def resolve(self, **kwargs):
        decision = super().resolve(**kwargs)
        if self.field == "role":
            decision["overlay_payload"]["role"] = self.value
        else:
            decision[self.field] = self.value
        return decision


def denied(call, error: type[Exception]) -> bool:
    try:
        call()
    except error:
        return True
    return False


def main() -> int:
    state = InMemoryControlContextStatePort()
    attestor = HmacControlResolutionAttestor(
        key_id="TEST", secret=b"not-a-production-secret-at-least-32-bytes",
    )
    resolver = BoundAssistantResolver()
    tools = ControlContextMcpTools(
        state, attestor, boot_birth_source_verifier=source,
        boot_birth_attestation_issuer=attestor,
        assistant_overlay_control_resolver=resolver,
        assistant_overlay_attestation_issuer=attestor,
        assistant_overlay_source_continuity_verifier=continuity,
    )
    birth = tools.dispatch(
        "create_ready_unbound_boot_generation",
        {"boot_attempt_id": ATTEMPT, "source_revision": HEAD}, context(),
    )["structuredContent"]["pre_role_generation"]
    before_read = tools.dispatch(
        "read_boot_generation_state", {"generation_ref": ATTEMPT},
        context("project_state:read"),
    )["structuredContent"]
    before = before_read["pre_role_generation"]
    args = {
        "generation_ref": ATTEMPT,
        "expected_revision": birth["revision"],
        "source_revision": HEAD,
    }
    checks = {
        "existing-ready-unbound-before-resume": birth["lifecycle"] == "READY_UNBOUND",
        "read-only-provider-state-before-resume": before == birth,
        "readback-carries-verified-session-binding": (
            before_read["authenticated_binding"] == {
                "tenant_ref": "TENANT", "workspace_ref": "WORKSPACE",
                "principal_ref": "HUMAN", "consumer_ref": "CHATGPT_REMOTE_MCP",
                "session_ref": context().session_ref(),
            }
        ),
        "read-tool-rejects-non-boot-ref": denied(
            lambda: tools.dispatch(
                "read_boot_generation_state", {"generation_ref": "OTHER"},
                context("project_state:read"),
            ), ControlContextToolError,
        ),
        "main-advance-after-birth-keeps-method-continuity": False,
        "missing-resolver-denies": denied(
            lambda: ControlContextMcpTools(
                state, attestor, boot_birth_source_verifier=source,
                assistant_overlay_attestation_issuer=attestor,
            ).dispatch(TOOL, args, context()), ControlContextToolAuthorizationError,
        ),
        "missing-issuer-denies": denied(
            lambda: ControlContextMcpTools(
                state, attestor, boot_birth_source_verifier=source,
                assistant_overlay_control_resolver=resolver,
            ).dispatch(TOOL, args, context()), ControlContextToolAuthorizationError,
        ),
        "read-only-scope-denies": denied(
            lambda: tools.dispatch(TOOL, args, context("project_state:read")),
            ControlContextToolAuthorizationError,
        ),
        "client-attestation-injection-denies": denied(
            lambda: tools.dispatch(TOOL, {**args, "control_resolution_attestation": {}}, context()),
            ControlContextToolError,
        ),
        "stale-revision-denies": denied(
            lambda: tools.dispatch(TOOL, {**args, "expected_revision": 1}, context()),
            ControlContextToolError,
        ),
        "missing-generation-denies-without-birth": denied(
            lambda: tools.dispatch(TOOL, {**args, "generation_ref": ATTEMPT + "-MISSING"}, context()),
            StateBindingError,
        ),
    }
    birth_method = verify_current_method(HEAD, fetch=provider(HEAD))
    continued = verify_existing_birth_method_continuity(
        HEAD, birth_method["method_fingerprint"], fetch=provider(ADVANCED_HEAD),
    )
    checks["main-advance-after-birth-keeps-method-continuity"] = (
        continued["source_revision"] == HEAD
        and continued["current_source_revision"] == ADVANCED_HEAD
        and continued["method_unchanged"] is True
        and continued["ancestry_verified"] is True
    )
    checks["diverged-source-denies"] = denied(
        lambda: verify_existing_birth_method_continuity(
            HEAD, birth_method["method_fingerprint"],
            fetch=provider(ADVANCED_HEAD, ancestor=False),
        ), BootBirthSourceError,
    )
    checks["changed-method-denies"] = denied(
        lambda: verify_existing_birth_method_continuity(
            HEAD, birth_method["method_fingerprint"],
            fetch=provider(ADVANCED_HEAD, changed_method=True),
        ), BootBirthSourceError,
    )
    for label, field, value in (
        ("non-assistant-role-denies", "role", "PRINCIPAL"),
        ("stale-control-decision-denies", "pre_role_fingerprint", "0" * 64),
    ):
        untrusted = ControlContextMcpTools(
            state, attestor, boot_birth_source_verifier=source,
            assistant_overlay_control_resolver=BadResolver(field, value),
            assistant_overlay_attestation_issuer=attestor,
            assistant_overlay_source_continuity_verifier=continuity,
        )
        checks[label] = denied(
            lambda owner=untrusted: owner.dispatch(TOOL, args, context()),
            ControlContextToolAuthorizationError,
        )
    result = tools.dispatch(TOOL, args, context())["structuredContent"]
    after = tools.dispatch(
        "read_boot_generation_state", {"generation_ref": ATTEMPT},
        context("project_state:read"),
    )["structuredContent"]["pre_role_generation"]
    checks["same-generation-role-attached"] = (
        result["pre_role_generation"]["generation_ref"] == ATTEMPT
        and result["pre_role_generation"]["lifecycle"] == "ROLE_ATTACHED"
        and result["actor_generation_shadow"]["generation_ref"] == "ASSISTANT_AD1_076EAE0F"
        and result["actor_generation_shadow"]["role"] == "ASSISTANT"
        and result["control_decision_ref"] == "AD1-GJENKLANG-CONTROL-001"
    )
    checks["no-signature-or-secret-returned"] = (
        "control_resolution_attestation" not in str(result)
        and "not-a-production-secret" not in str(result)
    )
    checks["second-attach-denied-no-replay"] = denied(
        lambda: tools.dispatch(TOOL, args, context()), ControlContextToolError,
    )
    checks["birth-receipt-still-same"] = (
        result["pre_role_generation"]["ready_unbound_receipt"] == birth["ready_unbound_receipt"]
    )
    checks["read-only-provider-state-after-resume"] = after == result["pre_role_generation"]
    for name, passed in checks.items():
        print(("PASS" if passed else "FAIL") + " " + name)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
