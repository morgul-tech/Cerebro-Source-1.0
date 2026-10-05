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
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from xml.etree import ElementTree


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
ROLE_NAMES = ("PRINCIPAL", "ASSISTANT", "PROJECT_MANAGER", "IMPLEMENTER", "WORKER", "RESEARCHER")
ROLE_PROJECTION_SCHEMA = "cerebro-role-method-projection/v1"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
ATOM_NS = "{http://www.w3.org/2005/Atom}"
HEAD_URL = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
ATOM_URL = f"https://github.com/{REPOSITORY}/commits/main.atom"
COMPARE_URL = f"https://api.github.com/repos/{REPOSITORY}/compare"


class BootBirthSourceError(ValueError):
    pass


def _http_get(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "cerebro-context-boot-birth/1"})
    with urlopen(request, timeout=10) as response:
        return response.read(2_000_001)


def _source_head(fetch: Callable[[str], bytes], *, atom_only: bool = False) -> tuple[str, str, bool]:
    """Read GitHub's current main; use its public Atom feed on API rate limiting."""
    if not atom_only:
        try:
            data = json.loads(fetch(HEAD_URL))
            return data["sha"], data["commit"]["committer"]["date"], False
        except HTTPError as exc:
            if exc.code != 403:
                raise
    feed = ElementTree.fromstring(fetch(ATOM_URL))
    if feed.findtext(f"{ATOM_NS}id") != f"tag:github.com,2008:/{REPOSITORY}/commits/main":
        raise BootBirthSourceError("source-atom-feed-mismatch")
    entry = feed.find(f"{ATOM_NS}entry")
    if entry is None:
        raise BootBirthSourceError("source-atom-entry-missing")
    entry_id = entry.findtext(f"{ATOM_NS}id", "")
    match = re.fullmatch(r"tag:github\.com,2008:Grit::Commit/([0-9a-f]{40})", entry_id)
    if match is None:
        raise BootBirthSourceError("source-atom-commit-invalid")
    committed_at = entry.findtext(f"{ATOM_NS}updated", "")
    return match.group(1), committed_at, True


def _method_contract_at_head(
    head: str, fetch: Callable[[str], bytes],
) -> tuple[dict[str, str | list[str]], tuple[str, ...], str]:
    """Read pinned method bytes without asserting that this commit is current main."""
    def pinned(path: str) -> bytes:
        try:
            data = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{head}/{path}")
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
    subject = "|".join((
        profile["id"], profile["version"], head,
        "|".join(METHODS), "|".join(material),
    ))
    fingerprint = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    return profile, tuple(material), fingerprint


def _role_contract_profile_at_head(
    head: str, fetch: Callable[[str], bytes],
) -> tuple[dict[str, object], dict[str, tuple[str, ...]]]:
    """Resolve role-scoped Source refs without granting role or authority."""

    def pinned(path: str) -> bytes:
        try:
            data = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{head}/{path}")
        except Exception as exc:
            raise BootBirthSourceError(f"source-blob-unavailable:{path}") from exc
        if not data or len(data) > 2_000_000:
            raise BootBirthSourceError(f"source-blob-size-invalid:{path}")
        return data

    try:
        owner_text = pinned(OWNER).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootBirthSourceError("method-owner-utf8-invalid") from exc
    matches = re.findall(r"(?m)^  role_method_contract_profile:\s*([^\r\n]+)$", owner_text)
    if len(matches) != 1:
        raise BootBirthSourceError("role-method-contract-profile-single-owner-required")
    try:
        profile = json.loads(matches[0])
    except ValueError as exc:
        raise BootBirthSourceError("role-method-contract-profile-json-invalid") from exc
    if (
        not isinstance(profile, dict)
        or set(profile) != {"version", "authority", "roles"}
        or profile["version"] != "1.0"
        or profile["authority"] != "NONE"
        or not isinstance(profile["roles"], dict)
        or tuple(profile["roles"]) != ROLE_NAMES
    ):
        raise BootBirthSourceError("role-method-contract-profile-invalid")
    normalized: dict[str, tuple[str, ...]] = {}
    for role in ROLE_NAMES:
        refs = profile["roles"].get(role)
        if (
            not isinstance(refs, list)
            or any(
                not isinstance(ref, str)
                or not ref
                or ref.startswith("/")
                or "\\" in ref
                or ".." in ref.split("/")
                for ref in refs
            )
            or len(refs) != len(set(refs))
        ):
            raise BootBirthSourceError("role-method-contract-refs-invalid")
        normalized[role] = tuple(refs)
    return profile, normalized


def verify_role_method_projection(
    source_revision: str,
    role: str,
    *,
    fetch: Callable[[str], bytes] = _http_get,
) -> dict[str, object]:
    """Verify one role projection against exact Source bytes at the birth revision.

    Currentness/authorization belong to the already-attested pre-role generation and
    role-attach owner. This function adds no role or authority; it only proves that
    the projected contract refs are exact bytes under that Source commit.
    """
    if not isinstance(source_revision, str) or not HEX40.fullmatch(source_revision):
        raise BootBirthSourceError("role-method-projection-source-invalid")
    if role not in ROLE_NAMES:
        raise BootBirthSourceError("role-method-projection-role-invalid")
    _, _, method_fingerprint = _method_contract_at_head(source_revision, fetch)
    _, role_map = _role_contract_profile_at_head(source_revision, fetch)
    refs = role_map[role]
    material: list[str] = []
    for ref in refs:
        try:
            data = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{source_revision}/{ref}")
        except Exception as exc:
            raise BootBirthSourceError(f"source-blob-unavailable:{ref}") from exc
        if not data or len(data) > 2_000_000:
            raise BootBirthSourceError(f"source-blob-size-invalid:{ref}")
        material.append(f"{ref}|{hashlib.sha256(data).hexdigest()}")
    contract_fingerprint = hashlib.sha256(
        "|".join((
            ROLE_PROJECTION_SCHEMA, source_revision, method_fingerprint, role,
            "|".join(material),
        )).encode("utf-8")
    ).hexdigest()
    return {
        "schema": ROLE_PROJECTION_SCHEMA,
        "role": role,
        "source_revision": source_revision,
        "method_profile_fingerprint": method_fingerprint,
        "contract_refs": list(refs),
        "contract_fingerprint": contract_fingerprint,
        "authority": "NONE",
    }


def _verify_current_method_details(
    requested_source_head: str, fetch: Callable[[str], bytes],
) -> tuple[dict[str, str | int], tuple[str, ...]]:
    if not isinstance(requested_source_head, str) or not HEX40.fullmatch(requested_source_head):
        raise BootBirthSourceError("source-head-invalid")
    try:
        actual_head, committed_at, used_atom = _source_head(fetch)
    except Exception as exc:
        raise BootBirthSourceError("source-head-provider-response-invalid") from exc
    if actual_head != requested_source_head:
        raise BootBirthSourceError("source-main-head-changed")
    if not HEX40.fullmatch(actual_head):
        raise BootBirthSourceError("source-head-provider-response-invalid")
    profile, material, fingerprint = _method_contract_at_head(actual_head, fetch)
    try:
        if _source_head(fetch, atom_only=used_atom)[0] != actual_head:
            raise BootBirthSourceError("source-main-head-changed-during-verification")
    except BootBirthSourceError:
        raise
    except Exception as exc:
        raise BootBirthSourceError("source-head-readback-invalid") from exc
    try:
        revision = int(datetime.fromisoformat(committed_at.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError) as exc:
        raise BootBirthSourceError("source-commit-time-invalid") from exc
    if revision <= 0:
        raise BootBirthSourceError("source-commit-time-invalid")
    result = {
        "source_revision": actual_head,
        "method_ref": profile["id"],
        "method_version": profile["version"],
        "method_fingerprint": fingerprint,
        "provider_frontier_ref": f"GITHUB_SOURCE_MAIN_{actual_head}",
        "provider_revision": revision,
    }
    return result, material


def verify_current_method(
    requested_source_head: str,
    *,
    fetch: Callable[[str], bytes] = _http_get,
) -> dict[str, str | int]:
    """Verify current main and reproduce Resolve-CerebroCivilizationMethod's hash."""
    return _verify_current_method_details(requested_source_head, fetch)[0]


def verify_existing_birth_method_continuity(
    birth_source_head: str,
    birth_method_fingerprint: str,
    *,
    fetch: Callable[[str], bytes] = _http_get,
) -> dict[str, str | int | bool]:
    """Permit an already durable birth across a code-only main advance.

    A former main commit is acceptable only when GitHub proves its ancestry and
    the pinned civilization-method bytes are unchanged. This never creates a
    birth, changes its source revision, or treats an arbitrary stale ref as live.
    """
    if not isinstance(birth_source_head, str) or not HEX40.fullmatch(birth_source_head):
        raise BootBirthSourceError("birth-source-head-invalid")
    if not isinstance(birth_method_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", birth_method_fingerprint):
        raise BootBirthSourceError("birth-method-fingerprint-invalid")
    try:
        current_head = _source_head(fetch)[0]
    except Exception as exc:
        raise BootBirthSourceError("source-head-provider-response-invalid") from exc
    current, current_material = _verify_current_method_details(current_head, fetch)
    if current_head == birth_source_head:
        if current["method_fingerprint"] != birth_method_fingerprint:
            raise BootBirthSourceError("birth-method-fingerprint-mismatch")
    else:
        try:
            comparison = json.loads(fetch(f"{COMPARE_URL}/{birth_source_head}...{current_head}"))
        except Exception as exc:
            raise BootBirthSourceError("birth-main-ancestry-unavailable") from exc

        if not isinstance(comparison, dict):
            raise BootBirthSourceError("birth-not-ancestor-of-current-main")

        def compared_sha(field: str) -> str | None:
            value = comparison.get(field)
            return value.get("sha") if isinstance(value, dict) else None

        if (
            comparison.get("status") != "ahead"
            or compared_sha("base_commit") != birth_source_head
            or compared_sha("merge_base_commit") != birth_source_head
        ):
            raise BootBirthSourceError("birth-not-ancestor-of-current-main")
        birth_profile, birth_material, computed_birth_fingerprint = _method_contract_at_head(
            birth_source_head, fetch,
        )
        if computed_birth_fingerprint != birth_method_fingerprint:
            raise BootBirthSourceError("birth-method-fingerprint-mismatch")
        if (
            birth_profile["id"] != current["method_ref"]
            or birth_profile["version"] != current["method_version"]
            or birth_material != current_material
        ):
            raise BootBirthSourceError("civilization-method-changed-since-birth")
    try:
        if _source_head(fetch)[0] != current_head:
            raise BootBirthSourceError("source-main-head-changed-during-verification")
    except BootBirthSourceError:
        raise
    except Exception as exc:
        raise BootBirthSourceError("source-head-readback-invalid") from exc
    return {
        "source_revision": birth_source_head,
        "current_source_revision": current_head,
        "method_ref": current["method_ref"],
        "method_version": current["method_version"],
        "method_fingerprint": birth_method_fingerprint,
        "method_unchanged": True,
        "ancestry_verified": True,
        "provider_frontier_ref": current["provider_frontier_ref"],
        "provider_revision": current["provider_revision"],
    }
