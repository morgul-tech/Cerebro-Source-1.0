"""Local managed-Drive placement plans and readback checks; no transport or credentials."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


class PlacementGap(ValueError):
    """Typed fail-closed result; carrying this exception does not authorize an effect."""


VALID_ID = re.compile(r"[A-Za-z0-9_-]{10,}\Z")
VALID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}\Z")
FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_MIME = "application/vnd.google-apps.drive-sdk"


def _required_text(value, status):
    if not isinstance(value, str) or not value.strip():
        raise PlacementGap(status)
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not VALID_ID.fullmatch(value) or value == "root":
        raise PlacementGap("HOLD_INVALID_ID")
    return value


def _url(value, status):
    if not isinstance(value, str):
        raise PlacementGap(status)
    try:
        parsed = urlparse(value)
    except ValueError:
        raise PlacementGap(status) from None
    if parsed.scheme != "https" or parsed.netloc not in {"drive.google.com", "docs.google.com"}:
        raise PlacementGap(status)
    return value


def _alias_value(record, keys, status, compare=lambda value: value):
    """Return one recognized spelling; reject disagreement instead of choosing precedence."""
    present = [record[key] for key in keys if key in record]
    if not present:
        return None
    try:
        first = compare(present[0])
        if any(compare(value) != first for value in present[1:]):
            raise PlacementGap(status)
    except PlacementGap:
        raise
    except (TypeError, ValueError):
        raise PlacementGap("HOLD_MALFORMED_METADATA") from None
    return present[0]


def _permission_email_aliases(permissions):
    if permissions is None:
        return None
    if not isinstance(permissions, list):
        raise PlacementGap("HOLD_MALFORMED_METADATA")
    normalized = []
    for permission in permissions:
        if not isinstance(permission, dict):
            raise PlacementGap("HOLD_MALFORMED_METADATA")
        email = _alias_value(
            permission,
            ("emailAddress", "email_address"),
            "HOLD_METADATA_ALIAS_CONFLICT",
            lambda value: value.casefold() if isinstance(value, str) else value,
        )
        item = dict(permission)
        if email is not None:
            item["emailAddress"] = email
        normalized.append(item)
    return normalized


def _contract(contract):
    if not isinstance(contract, dict):
        raise PlacementGap("HOLD_MALFORMED_CONTRACT")
    if contract.get("schema") != "cerebro-owner-placement/v0.2":
        raise PlacementGap("HOLD_UNSUPPORTED_CONTRACT")
    for key in ("contract_id", "owner", "case_id", "owner_lane", "scope"):
        _required_text(contract.get(key), "HOLD_MALFORMED_CONTRACT")
    if not isinstance(contract.get("revision"), int) or contract["revision"] < 1:
        raise PlacementGap("HOLD_MALFORMED_CONTRACT")
    if contract.get("authority") != "NONE":
        raise PlacementGap("HOLD_CONTRACT_AUTHORITY_UNQUALIFIED")
    try:
        identifier(contract.get("coordinator_parent_id"))
        _url(contract.get("owner_home", {}).get("url"), "HOLD_MALFORMED_CONTRACT")
        placements = contract.get("placements")
        if not isinstance(placements, dict) or set(placements) != {"HUMAN_HANDOFF"}:
            raise PlacementGap("HOLD_MALFORMED_CONTRACT")
        placement = placements["HUMAN_HANDOFF"]
        identifier(placement.get("parent_id"))
        if placement.get("mime_type") != "text/markdown" or placement.get("file_suffix") != ".md":
            raise PlacementGap("HOLD_MALFORMED_CONTRACT")
        owner_basis = contract.get("owner_basis")
        if (not isinstance(owner_basis, dict)
                or owner_basis.get("required_metadata_visibility") != "permission_metadata_available"
                or owner_basis.get("expected_owner_count") != 1):
            raise PlacementGap("HOLD_MALFORMED_CONTRACT")
        _required_text(owner_basis.get("expected_owner_email"), "HOLD_MALFORMED_CONTRACT")
    except (AttributeError, TypeError):
        raise PlacementGap("HOLD_MALFORMED_CONTRACT") from None
    return placement


def _gate(contract, owner, case, fileclass):
    placement = _contract(contract)
    if owner != contract["owner"] or case != contract["case_id"]:
        raise PlacementGap("HOLD_OWNER_CASE_MISMATCH")
    if fileclass != "HUMAN_HANDOFF" or fileclass not in contract["placements"]:
        raise PlacementGap("HOLD_MISSING_BINDING")
    return placement


def normalize_metadata(metadata):
    """Read only known normalized and provider-native managed metadata spellings."""
    if not isinstance(metadata, dict):
        raise PlacementGap("HOLD_MALFORMED_METADATA")
    file_id = metadata.get("id")
    mime = _alias_value(metadata, ("mime_type", "mimeType"), "HOLD_METADATA_ALIAS_CONFLICT")
    parents = _alias_value(
        metadata,
        ("parent_ids", "parents"),
        "HOLD_METADATA_ALIAS_CONFLICT",
        lambda value: tuple(sorted(value)) if isinstance(value, list) and all(isinstance(x, str) for x in value) else value,
    )
    url = _alias_value(metadata, ("url", "webViewLink"), "HOLD_METADATA_ALIAS_CONFLICT")
    permissions = _permission_email_aliases(metadata.get("permissions"))
    visibility = metadata.get("source_visibility_status")
    if not isinstance(file_id, str) or not isinstance(mime, str):
        raise PlacementGap("HOLD_MALFORMED_METADATA")
    if parents is not None:
        if not isinstance(parents, list) or any(not isinstance(parent, str) for parent in parents):
            raise PlacementGap("HOLD_MALFORMED_METADATA")
        for parent in parents:
            identifier(parent)
    return {
        "id": file_id,
        "mime_type": mime,
        "parent_ids": parents,
        "url": url,
        "permissions": permissions,
        "source_visibility_status": visibility,
    }


def _owner_basis(contract, metadata):
    normalized = normalize_metadata(metadata)
    expected = contract["owner_basis"]["expected_owner_email"].casefold()
    if normalized["source_visibility_status"] != "permission_metadata_available":
        raise PlacementGap("HOLD_OWNER_BASIS_UNKNOWN")
    permissions = normalized["permissions"]
    if not isinstance(permissions, list):
        raise PlacementGap("HOLD_OWNER_BASIS_UNKNOWN")
    owners = [p for p in permissions if isinstance(p, dict) and p.get("role") == "owner"]
    if len(owners) != contract["owner_basis"]["expected_owner_count"]:
        raise PlacementGap("HOLD_OWNER_BASIS_DENIED")
    emails = [p.get("emailAddress", p.get("email_address")) for p in owners]
    if any(not isinstance(email, str) for email in emails):
        raise PlacementGap("HOLD_OWNER_BASIS_UNKNOWN")
    if [email.casefold() for email in emails] != [expected]:
        raise PlacementGap("HOLD_OWNER_BASIS_DENIED")
    return normalized


def _parent(contract, placement, parent_metadata):
    normalized = _owner_basis(contract, parent_metadata)
    expected_parent = identifier(placement["parent_id"])
    if normalized["id"] != expected_parent or normalized["mime_type"] != FOLDER_MIME:
        raise PlacementGap("HOLD_PARENT_EVIDENCE_MISMATCH")
    expected_coordinator = identifier(contract["coordinator_parent_id"])
    if normalized["parent_ids"] != [expected_coordinator]:
        raise PlacementGap("HOLD_PARENT_EVIDENCE_MISMATCH")
    return normalized


def _filename(contract, fileclass, path):
    original = path.stem.lower()
    original = re.sub(r"[^a-z0-9_-]+", "-", original).strip("-_")[:48]
    if not original:
        raise PlacementGap("HOLD_INVALID_FILENAME")
    tokens = (contract["contract_id"], contract["case_id"], fileclass, original)
    if any(not VALID_TOKEN.fullmatch(token) for token in tokens):
        raise PlacementGap("HOLD_INVALID_FILENAME")
    return "-".join(tokens) + ".md"


def plan(contract, owner, case, fileclass, local_path, parent_metadata, original_home_pointer):
    placement = _gate(contract, owner, case, fileclass)
    _parent(contract, placement, parent_metadata)
    pointer = _url(original_home_pointer, "HOLD_MISSING_ORIGINAL_HOME_POINTER")
    if pointer != contract["owner_home"]["url"]:
        raise PlacementGap("HOLD_ORIGINAL_HOME_POINTER_MISMATCH")
    try:
        path = Path(local_path).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise PlacementGap("HOLD_MISSING_PAYLOAD") from None
    if not path.is_file() or path.suffix.lower() != placement["file_suffix"]:
        raise PlacementGap("HOLD_PAYLOAD_TYPE_MISMATCH")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise PlacementGap("HOLD_MALFORMED_PAYLOAD") from None
    if pointer not in content:
        raise PlacementGap("HOLD_ORIGINAL_HOME_POINTER_MISSING_FROM_PAYLOAD")
    name = _filename(contract, fileclass, path)
    parent = identifier(placement["parent_id"])
    return {
        "status": "PLAN_ONLY",
        "authority": "NONE",
        "tool": "google_drive_upload_file",
        "arguments": {
            "file_uri": str(path),
            "file_name": name,
            "mime_type": placement["mime_type"],
            "parent_folder_id": parent,
        },
        "owner": owner,
        "case_id": case,
        "fileclass": fileclass,
        "original_home_pointer": pointer,
        "link_release": "AFTER_EXACT_ID_MIME_AND_PARENT_READBACK_ONLY",
        "unknown_outcome": "HOLD_RECONCILE_SAME_LOGICAL_ARTIFACT_NO_RETRY",
    }


def verify(file_id, expected_parent, metadata, expected_mime_type=None):
    identifier(file_id)
    identifier(expected_parent)
    normalized = normalize_metadata(metadata)
    if normalized["id"] != file_id:
        raise PlacementGap("HOLD_FILE_ID_MISMATCH")
    parents = normalized["parent_ids"]
    if parents != [expected_parent]:
        raise PlacementGap("HOLD_PLACEMENT_MISMATCH")
    if expected_mime_type is not None and normalized["mime_type"] != expected_mime_type:
        raise PlacementGap("HOLD_MIME_TYPE_MISMATCH")
    url = _url(normalized["url"], "HOLD_MISSING_RECIPIENT_LINK")
    return {
        "status": "PARENT_READBACK_VERIFIED",
        "authority": "NONE",
        "file_id": file_id,
        "parent_id": expected_parent,
        "mime_type": normalized["mime_type"],
        "recipient_link": url,
        "content_readback": "SEPARATE_REQUIRED_FOR_CONTENT_CLAIM",
    }


def repair(file_id, expected_parent, metadata, contract, owner, case, fileclass,
           parent_metadata, repair_basis=None):
    placement = _gate(contract, owner, case, fileclass)
    expected_parent = identifier(expected_parent)
    if expected_parent != identifier(placement["parent_id"]):
        raise PlacementGap("HOLD_PARENT_EVIDENCE_MISMATCH")
    normalized = normalize_metadata(metadata)
    identifier(file_id)
    if normalized["id"] != file_id:
        raise PlacementGap("HOLD_FILE_ID_MISMATCH")
    if normalized["mime_type"] != placement["mime_type"]:
        raise PlacementGap("HOLD_MIME_TYPE_MISMATCH")
    parents = normalized["parent_ids"]
    if not isinstance(parents, list) or not parents:
        raise PlacementGap("HOLD_MISSING_OBSERVED_PARENTS")
    if parents == [expected_parent]:
        return {"status": "NO_REPAIR_NEEDED", "file_id": file_id, "effect": "NONE"}
    _parent(contract, placement, parent_metadata)
    remove = [parent for parent in parents if parent != expected_parent]
    add = expected_parent not in parents
    if not isinstance(repair_basis, dict):
        raise PlacementGap("HOLD_REPAIR_OWNER_BASIS_DENIED")
    if (repair_basis.get("status") != "OWNER_APPROVED_CURRENT_PLACEMENT"
            or repair_basis.get("owner") != owner
            or repair_basis.get("case_id") != case
            or repair_basis.get("file_id") != file_id
            or repair_basis.get("add_parent_id") != (expected_parent if add else None)
            or repair_basis.get("remove_parent_ids") != remove):
        raise PlacementGap("HOLD_REPAIR_OWNER_BASIS_DENIED")
    args = {"fileId": file_id}
    if add:
        args["addParents"] = expected_parent
    if remove:
        args["removeParents"] = ",".join(remove)
    return {
        "status": "PLAN_ONLY",
        "authority": "NONE",
        "tool": "google_drive_update_file",
        "arguments": args,
        "same_file_id_required": True,
        "requires": "OWNER_APPROVED_CURRENT_PLACEMENT_AND_AFTER_READBACK",
        "unknown_outcome": "HOLD_RECONCILE_SAME_FILE_ID_NO_RETRY",
    }


def _read_input(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PlacementGap("HOLD_MALFORMED_INPUT") from None
    if not isinstance(value, dict):
        raise PlacementGap("HOLD_MALFORMED_INPUT")
    return value


def _read_contract(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise PlacementGap("HOLD_MALFORMED_CONTRACT") from None
    if not isinstance(value, dict):
        raise PlacementGap("HOLD_MALFORMED_CONTRACT")
    return value


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("operation", choices=["plan", "verify", "repair"])
    ap.add_argument("--input", required=True)
    ap.add_argument("--contract", help="Public placement contract JSON; use instead of embedding it in input.")
    ns = ap.parse_args(argv)
    try:
        args = _read_input(ns.input)
        if ns.contract is not None:
            if "contract" in args:
                raise PlacementGap("HOLD_MALFORMED_INPUT")
            args["contract"] = _read_contract(ns.contract)
        result = {"plan": plan, "verify": verify, "repair": repair}[ns.operation](**args)
    except PlacementGap as exc:
        print(json.dumps({"status": str(exc), "effect": "NONE"}, ensure_ascii=True))
        return 2
    except (TypeError, KeyError):
        print(json.dumps({"status": "HOLD_MALFORMED_INPUT", "effect": "NONE"}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
