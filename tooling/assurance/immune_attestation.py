#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any

SCHEMA = "cerebro-immune-attestation/v1"
ALGORITHM = "HMAC-SHA256"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

MATERIAL_SUBJECT_FIELDS = (
    "permit_id",
    "campaign_id",
    "source_pre_head",
    "package_sha256",
    "touched_paths_sha256",
    "quarantine_scope_sha256",
    "risk_profile",
    "intended_consequence_class",
    "authority_epoch",
    "producer_identity",
)

RECOVERY_SUBJECT_FIELDS = (
    "subject_type",
    "migration_id",
    "migration_subject_sha256",
    "entry_authorization_fingerprint",
    "requested_recovery_action",
    "authority_epoch",
    "mcp_recovery_decision_sha256",
    "current_host_proof_sha256",
    "installation_plan_sha256",
    "installation_observation_sha256",
    "publication_state",
    "quarantine_scope_sha256",
    "producer_identity",
    "recovery_nonce",
)


class ImmuneAttestationError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: str | Path) -> str:
    return sha256_hex(Path(path).read_bytes())


def key_fingerprint(key: bytes) -> str:
    if len(key) < 32:
        raise ImmuneAttestationError("ATTESTOR_KEY_TOO_SHORT")
    return sha256_hex(key)


def _validate_subject(subject: Any) -> dict[str, Any]:
    if not isinstance(subject, dict):
        raise ImmuneAttestationError("ATTESTATION_SUBJECT_OBJECT_REQUIRED")

    is_recovery = subject.get("subject_type") == "MIGRATION_RECOVERY"
    required = RECOVERY_SUBJECT_FIELDS if is_recovery else MATERIAL_SUBJECT_FIELDS
    for field in required:
        if subject.get(field) in (None, ""):
            raise ImmuneAttestationError(f"ATTESTATION_SUBJECT_FIELD_MISSING:{field}")

    if is_recovery:
        for field in (
            "migration_subject_sha256",
            "entry_authorization_fingerprint",
            "mcp_recovery_decision_sha256",
            "current_host_proof_sha256",
            "installation_plan_sha256",
            "installation_observation_sha256",
            "quarantine_scope_sha256",
        ):
            if not HEX64_RE.fullmatch(str(subject[field])):
                raise ImmuneAttestationError(f"ATTESTATION_SHA256_INVALID:{field}")
        if subject["requested_recovery_action"] not in {
            "RESUME_EXACT",
            "ROLLBACK_EXACT_PREPUBLICATION",
            "QUARANTINE",
        }:
            raise ImmuneAttestationError("ATTESTATION_RECOVERY_ACTION_INVALID")
        if subject["publication_state"] not in {"NOT_PUBLISHED", "PUBLISHED", "UNKNOWN"}:
            raise ImmuneAttestationError("ATTESTATION_PUBLICATION_STATE_INVALID")
        nonce = str(subject["recovery_nonce"])
        if not nonce.startswith("RECOVERY:") or len(nonce) < 25:
            raise ImmuneAttestationError("ATTESTATION_RECOVERY_NONCE_INVALID")
    else:
        if not HEX40_RE.fullmatch(str(subject["source_pre_head"])):
            raise ImmuneAttestationError("ATTESTATION_SOURCE_HEAD_INVALID")
        for field in (
            "package_sha256",
            "touched_paths_sha256",
            "quarantine_scope_sha256",
        ):
            if not HEX64_RE.fullmatch(str(subject[field])):
                raise ImmuneAttestationError(f"ATTESTATION_SHA256_INVALID:{field}")
        if subject["intended_consequence_class"] not in {
            "SOURCE_EFFECT",
            "RUNTIME_EFFECT",
            "SHARED_EFFECT",
            "KNOWLEDGE_ONLY",
        }:
            raise ImmuneAttestationError("ATTESTATION_CONSEQUENCE_CLASS_INVALID")

    if not isinstance(subject["authority_epoch"], int) or subject["authority_epoch"] < 1:
        raise ImmuneAttestationError("ATTESTATION_AUTHORITY_EPOCH_INVALID")
    return dict(subject)


def _unsigned(attestation: dict[str, Any]) -> dict[str, Any]:
    unsigned = dict(attestation)
    unsigned.pop("signature", None)
    unsigned.pop("attestation_fingerprint", None)
    return unsigned


def sign_attestation(
    *,
    subject: dict[str, Any],
    key: bytes,
    attestor_identity: str,
    implementation_path: str | Path,
) -> dict[str, Any]:
    normalized = _validate_subject(subject)
    attestor = str(attestor_identity).strip()
    if not attestor:
        raise ImmuneAttestationError("ATTESTOR_IDENTITY_REQUIRED")
    if attestor == str(normalized["producer_identity"]):
        raise ImmuneAttestationError("PRODUCER_SELF_ATTESTATION_PROHIBITED")
    implementation = Path(implementation_path)
    if not implementation.is_file():
        raise ImmuneAttestationError("ATTESTOR_IMPLEMENTATION_MISSING")

    result: dict[str, Any] = {
        "schema": SCHEMA,
        "algorithm": ALGORITHM,
        "validation_result": "PASS",
        "attestor_identity": attestor,
        "implementation_sha256": file_sha256(implementation),
        "key_fingerprint": key_fingerprint(key),
        "subject": normalized,
    }
    result["signature"] = hmac.new(key, canonical(_unsigned(result)), hashlib.sha256).hexdigest()
    result["attestation_fingerprint"] = sha256_hex(canonical(_unsigned(result)))
    return result


def verify_attestation(
    attestation: dict[str, Any],
    *,
    key: bytes,
    implementation_path: str | Path,
    expected_subject: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(attestation, dict):
        raise ImmuneAttestationError("ATTESTATION_OBJECT_REQUIRED")
    if attestation.get("schema") != SCHEMA:
        raise ImmuneAttestationError("ATTESTATION_SCHEMA_INVALID")
    if attestation.get("algorithm") != ALGORITHM:
        raise ImmuneAttestationError("ATTESTATION_ALGORITHM_INVALID")
    if attestation.get("validation_result") != "PASS":
        raise ImmuneAttestationError("ATTESTATION_RESULT_NOT_PASS")
    subject = _validate_subject(attestation.get("subject"))
    attestor = str(attestation.get("attestor_identity") or "")
    if not attestor:
        raise ImmuneAttestationError("ATTESTOR_IDENTITY_REQUIRED")
    if attestor == str(subject["producer_identity"]):
        raise ImmuneAttestationError("PRODUCER_SELF_ATTESTATION_PROHIBITED")

    implementation = Path(implementation_path)
    if not implementation.is_file():
        raise ImmuneAttestationError("ATTESTOR_IMPLEMENTATION_MISSING")
    if file_sha256(implementation) != str(attestation.get("implementation_sha256") or ""):
        raise ImmuneAttestationError("ATTESTOR_IMPLEMENTATION_DRIFT")
    if key_fingerprint(key) != str(attestation.get("key_fingerprint") or ""):
        raise ImmuneAttestationError("ATTESTOR_KEY_DRIFT")

    signature = str(attestation.get("signature") or "")
    if not HEX64_RE.fullmatch(signature):
        raise ImmuneAttestationError("ATTESTATION_SIGNATURE_INVALID")
    expected_signature = hmac.new(
        key, canonical(_unsigned(attestation)), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ImmuneAttestationError("ATTESTATION_SIGNATURE_INVALID")

    fingerprint = str(attestation.get("attestation_fingerprint") or "")
    expected_fingerprint = sha256_hex(canonical(_unsigned(attestation)))
    if not HEX64_RE.fullmatch(fingerprint) or fingerprint != expected_fingerprint:
        raise ImmuneAttestationError("ATTESTATION_FINGERPRINT_INVALID")

    if expected_subject is not None:
        expected = _validate_subject(expected_subject)
        if canonical(expected) != canonical(subject):
            raise ImmuneAttestationError("ATTESTATION_SUBJECT_MISMATCH")

    return {
        "result": "PASS",
        "attestation_fingerprint": fingerprint,
        "attestor_identity": attestor,
        "subject": subject,
    }


def _read_json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ImmuneAttestationError("JSON_OBJECT_REQUIRED")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sign = sub.add_parser("sign")
    sign.add_argument("--subject", required=True)
    sign.add_argument("--key", required=True)
    sign.add_argument("--attestor-identity", required=True)
    sign.add_argument("--implementation", required=True)
    sign.add_argument("--output", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--attestation", required=True)
    verify.add_argument("--key", required=True)
    verify.add_argument("--implementation", required=True)
    verify.add_argument("--expected-subject")

    args = parser.parse_args()
    try:
        if args.command == "sign":
            output = sign_attestation(
                subject=_read_json(args.subject),
                key=Path(args.key).read_bytes(),
                attestor_identity=args.attestor_identity,
                implementation_path=args.implementation,
            )
            Path(args.output).write_text(
                json.dumps(output, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        else:
            output = verify_attestation(
                _read_json(args.attestation),
                key=Path(args.key).read_bytes(),
                implementation_path=args.implementation,
                expected_subject=(
                    _read_json(args.expected_subject)
                    if args.expected_subject
                    else None
                ),
            )
        print(json.dumps(output, sort_keys=True))
        return 0
    except (ImmuneAttestationError, OSError, ValueError) as exc:
        print(json.dumps({"result": "DENY", "reason": str(exc)}, sort_keys=True))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
