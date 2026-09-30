"""Independent Source-currentness check for a role-neutral Boot birth.

This module only verifies public Source bytes. It never issues an attestation or
grants a role. The Context State service uses its result before creating a
READY_UNBOUND generation.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Callable
from urllib.request import Request, urlopen


REPOSITORY = "morgul-tech/Cerebro-Source-1.0"
OWNER = "standards/boot-critical-path-architecture.yaml"
METHODS = (
    "CURRENTNESS_FIRST", "METHOD_NE_AUTHORITY", "NO_LIVE_STATE_INHERITANCE",
    "VERIFY_LOAD_CONSUME_READBACK", "TASK_SEMANTICS_ON_DEMAND",
)
REFS = (
    "mcp/constitution.yaml", "mcp/boot-architecture-control.yaml",
    "engines/presentation/component.yaml", "engines/presentation/rules.yaml",
)
HEX40 = re.compile(r"[0-9a-f]{40}\Z")


class BootBirthSourceError(ValueError):
    pass


def _http_get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "cerebro-context-boot-birth/1"})
    with urlopen(request, timeout=10) as response:
        return response.read(2_000_001)


def verify_current_method(
    requested_source_head: str,
    *,
    fetch: Callable[[str], bytes] = _http_get,
) -> dict[str, str | int]:
    """Verify current main and reproduce Resolve-CerebroCivilizationMethod's hash."""
    if not isinstance(requested_source_head, str) or not HEX40.fullmatch(requested_source_head):
        raise BootBirthSourceError("source-head-invalid")
    head_url = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
    try:
        head_data = json.loads(fetch(
            head_url
        ))
        actual_head = head_data["sha"]
        committed_at = head_data["commit"]["committer"]["date"]
    except Exception as exc:
        raise BootBirthSourceError("source-head-provider-response-invalid") from exc
    if actual_head != requested_source_head:
        raise BootBirthSourceError("source-main-head-changed")
    if not HEX40.fullmatch(actual_head):
        raise BootBirthSourceError("source-head-provider-response-invalid")

    def pinned(path: str) -> bytes:
        try:
            data = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{actual_head}/{path}")
        except Exception as exc:
            raise BootBirthSourceError(f"source-blob-unavailable:{path}") from exc
        if not data or len(data) > 2_000_000:
            raise BootBirthSourceError(f"source-blob-size-invalid:{path}")
        return data

    owner_bytes = pinned(OWNER)
    try:
        owner_text = owner_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootBirthSourceError("method-owner-utf8-invalid") from exc
    matches = re.findall(r"(?m)^  current_civilization_method_profile:\s*([^\r\n]+)$", owner_text)
    if len(matches) != 1:
        raise BootBirthSourceError("method-single-owner-required")
    for key in ("id", "version", "authority", "durable_method", "contract_refs"):
        if len(re.findall(r'"' + key + r'"\s*:', matches[0])) != 1:
            raise BootBirthSourceError("method-exact-fields-required")
    try:
        profile = json.loads(matches[0])
    except ValueError as exc:
        raise BootBirthSourceError("method-profile-json-invalid") from exc
    if (
        not isinstance(profile, dict)
        or set(profile) != {"id", "version", "authority", "durable_method", "contract_refs"}
        or profile["id"] != "CURRENT_CIVILIZATION_METHOD_PROFILE"
        or profile["version"] != "1.0"
        or profile["authority"] != "NONE"
        or profile["durable_method"] != list(METHODS)
        or profile["contract_refs"] != list(REFS)
    ):
        raise BootBirthSourceError("method-profile-contract-mismatch")
    material = [f"{OWNER}|{hashlib.sha256(owner_bytes).hexdigest()}"]
    for ref in REFS:
        material.append(f"{ref}|{hashlib.sha256(pinned(ref)).hexdigest()}")
    try:
        if json.loads(fetch(head_url))["sha"] != actual_head:
            raise BootBirthSourceError("source-main-head-changed-during-verification")
    except BootBirthSourceError:
        raise
    except Exception as exc:
        raise BootBirthSourceError("source-head-readback-invalid") from exc
    subject = "|".join((
        profile["id"], profile["version"], actual_head,
        "|".join(METHODS), "|".join(material),
    ))
    fingerprint = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    try:
        revision = int(datetime.fromisoformat(committed_at.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError) as exc:
        raise BootBirthSourceError("source-commit-time-invalid") from exc
    if revision <= 0:
        raise BootBirthSourceError("source-commit-time-invalid")
    return {
        "source_revision": actual_head,
        "method_ref": profile["id"],
        "method_version": profile["version"],
        "method_fingerprint": fingerprint,
        "provider_frontier_ref": f"GITHUB_SOURCE_MAIN_{actual_head}",
        "provider_revision": revision,
    }
