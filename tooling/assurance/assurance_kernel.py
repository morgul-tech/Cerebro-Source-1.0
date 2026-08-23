#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

PERMIT_SCHEMA = "cerebro-assurance-kernel-permit/v1"
RECEIPT_SCHEMA = "cerebro-assurance-kernel-receipt/v1"
STATE_SCHEMA = "cerebro-assurance-kernel-state/v1"
TRUST_OBJECT_SCHEMA = "cerebro-doctor-trust-object/v1"
TRUST_ATTESTATION_SCHEMA = "cerebro-doctor-trust-attestation/v1"

IMMUNE_PERMIT_SCHEMA = "cerebro-immune-material-permit/v1"
IMMUNE_RECEIPT_SCHEMA = "cerebro-immune-material-receipt/v1"
IMMUNE_ATTESTATION_SCHEMA = "cerebro-immune-attestation/v1"
IMMUNE_MIGRATION_SCHEMA = "cerebro-immune-migration/v1"
IMMUNE_RECOVERY_RECORD_SCHEMA = "cerebro-immune-migration-recovery-record/v1"
IMMUNE_INSTALLATION_PLAN_SCHEMA = "cerebro-immune-installation-plan/v1"
IMMUNE_INSTALLATION_OBSERVATION_SCHEMA = "cerebro-immune-installation-observation/v1"
ONE_TIME_HUMAN_ADMIN_FIRST_ACTIVATION_SCHEMA = (
    "cerebro-one-time-human-admin-first-activation/v1"
)
ONE_TIME_HUMAN_ADMIN_FIRST_ACTIVATION = (
    "ONE_TIME_HUMAN_ADMIN_FIRST_ACTIVATION"
)
FIRST_ACTIVATION_DIRECTIVE = (
    "AN-ADMIN-IMMUNFORSVAR-FIRST-ACTIVATION-HUMAN-BOOTSTRAP-AUTHORITY-001"
)
FIRST_ACTIVATION_BASE_COMMIT = "4469eddccc8db213bb923c403c26c36e30309575"
FIRST_ACTIVATION_PARENT_CANDIDATE_FINGERPRINT = (
    "17ba9aad66c2d5c9e98d42091762b90be8e720c03ccc0856c8fb16872b8dfce0"
)
IMMUNE_RECOVERY_ACTIONS = {
    "RESUME_EXACT",
    "ROLLBACK_EXACT_PREPUBLICATION",
    "QUARANTINE",
}

STATES = {
    "UNINITIALIZED",
    "BOOTSTRAP_ONLY",
    "DOCTOR_ENFORCED",
    "FAILED_RECOVERY",
    "IMMUNE_MIGRATING",
    "IMMUNE_ENFORCED",
    "IMMUNE_QUARANTINED",
}
BOOTSTRAP_PACKAGE_CLASS = "DOCTOR_BOOTSTRAP_PACKAGE"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class AssuranceDenied(RuntimeError):
    pass


def canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path) -> str:
    return sha256_hex(Path(path).read_bytes())


def path_fingerprint(path: str | Path) -> str:
    return sha256_hex(str(Path(path).resolve()).encode("utf-8"))


def ledger_fingerprint(consumed: list[Any]) -> str:
    return sha256_hex(canonical(list(consumed)))


def touched_paths_fingerprint(paths: list[str]) -> str:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        value = str(raw).replace("\\", "/").strip("/")
        if not value:
            raise AssuranceDenied("TOUCHED_PATH_EMPTY")
        if value in seen:
            raise AssuranceDenied("TOUCHED_PATH_DUPLICATE:" + value)
        seen.add(value)
        normalized.append(value)
    normalized.sort()
    return sha256_hex(canonical(normalized))


def candidate_identity_from_manifest(path: str | Path) -> str:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise AssuranceDenied("FIRST_ACTIVATION_MANIFEST_FILES_INVALID")
    rows: list[str] = []
    seen: set[str] = set()
    for entry in sorted(files, key=lambda value: str(value.get("path") or "")):
        if not isinstance(entry, dict):
            raise AssuranceDenied("FIRST_ACTIVATION_MANIFEST_ENTRY_INVALID")
        item_path = str(entry.get("path") or "")
        if not item_path or item_path in seen:
            raise AssuranceDenied("FIRST_ACTIVATION_MANIFEST_PATH_INVALID")
        seen.add(item_path)
        rows.append(
            "|".join(
                str(entry.get(field) or "")
                for field in (
                    "path",
                    "operation",
                    "expected_git_blob_sha",
                    "final_git_blob_sha",
                    "sha256",
                )
            )
        )
    return sha256_hex("\n".join(rows).encode("utf-8"))


def current_host_fingerprint(working_source_path: str | Path) -> str:
    if os.name != "nt":
        raise AssuranceDenied("FIRST_ACTIVATION_WINDOWS_HOST_REQUIRED")
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            machine_guid = str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except Exception as exc:
        raise AssuranceDenied("FIRST_ACTIVATION_MACHINE_GUID_UNAVAILABLE") from exc
    computer_name = str(os.environ.get("COMPUTERNAME") or "").strip()
    if not machine_guid.strip() or not computer_name:
        raise AssuranceDenied("FIRST_ACTIVATION_HOST_IDENTITY_INCOMPLETE")
    return sha256_hex(
        canonical(
            {
                "machine_guid": machine_guid.strip().lower(),
                "computer_name": computer_name.lower(),
                "working_source": str(Path(working_source_path).resolve()).lower(),
            }
        )
    )


def _git_value(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssuranceDenied(
            "FIRST_ACTIVATION_GIT_FAILED:"
            + " ".join(args)
            + ":"
            + completed.stderr.strip()
        )
    return completed.stdout.strip()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".assurance-kernel-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline="\n"
        ) as stream:
            json.dump(
                value,
                stream,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def _exclusive_lock(
    lock_path: Path, *, retries: int = 200, delay: float = 0.025
) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(lock_path, "a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
        os.fsync(stream.fileno())

    acquired = False
    try:
        for _ in range(retries):
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                acquired = True
                break
            except (OSError, BlockingIOError):
                time.sleep(delay)
        if not acquired:
            raise AssuranceDenied("KERNEL_STATE_LOCK_TIMEOUT")
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        stream.close()


@dataclass(frozen=True)
class MaterialIntent:
    source_head: str
    package_sha256: str
    touched_paths_sha256: str
    package_class: str
    campaign_id: str
    authority_epoch: int


def _load_module(name: str, path: Path) -> Any:
    if not path.is_file():
        raise AssuranceDenied("VERIFIER_IMPLEMENTATION_MISSING:" + str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssuranceDenied("VERIFIER_IMPORT_SPEC_INVALID:" + str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AssuranceKernel:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.lock_path = state_path.with_name(state_path.name + ".lock")

    def _read(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema": STATE_SCHEMA,
                "state": "UNINITIALIZED",
                "authority_epoch": 1,
                "consumed": [],
            }
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("schema") != STATE_SCHEMA:
            raise AssuranceDenied("KERNEL_STATE_SCHEMA_INVALID")
        if data.get("state") not in STATES:
            raise AssuranceDenied("KERNEL_STATE_INVALID")
        if (
            not isinstance(data.get("authority_epoch"), int)
            or data["authority_epoch"] < 1
        ):
            raise AssuranceDenied("KERNEL_EPOCH_INVALID")
        consumed = data.get("consumed")
        if not isinstance(consumed, list) or len(consumed) != len(
            set(map(str, consumed))
        ):
            raise AssuranceDenied("KERNEL_LEDGER_INVALID")

        if data["state"] == "DOCTOR_ENFORCED":
            required = (
                "doctor_active_path_proof_sha256",
                "doctor_trust_key_path",
                "doctor_trust_key_sha256",
                "doctor_verifier_path",
                "doctor_verifier_sha256",
            )
            for field in required:
                if data.get(field) in (None, ""):
                    raise AssuranceDenied(
                        "DOCTOR_BINDING_STATE_MISSING:" + field
                    )
            for field in (
                "doctor_active_path_proof_sha256",
                "doctor_trust_key_sha256",
                "doctor_verifier_sha256",
            ):
                if not HEX64_RE.fullmatch(str(data[field])):
                    raise AssuranceDenied(
                        "DOCTOR_BINDING_STATE_INVALID:" + field
                    )

        if data["state"] in {
            "IMMUNE_MIGRATING",
            "IMMUNE_ENFORCED",
            "IMMUNE_QUARANTINED",
        }:
            required = (
                "migration_id",
                "migration_source_state",
                "migration_source_head",
                "migration_source_tree",
                "migration_consumed_ledger_sha256",
                "external_anchor_id",
                "external_anchor_fingerprint",
                "external_anchor_verifier_path",
                "external_anchor_verifier_sha256",
                "immune_attestor_path",
                "immune_attestor_sha256",
                "immune_attestor_key_path",
                "immune_attestor_key_fingerprint",
            )
            for field in required:
                if data.get(field) in (None, ""):
                    raise AssuranceDenied(
                        "IMMUNE_BINDING_STATE_MISSING:" + field
                    )
            for field in (
                "migration_consumed_ledger_sha256",
                "external_anchor_fingerprint",
                "external_anchor_verifier_sha256",
                "immune_attestor_sha256",
                "immune_attestor_key_fingerprint",
            ):
                if not HEX64_RE.fullmatch(str(data[field])):
                    raise AssuranceDenied(
                        "IMMUNE_BINDING_STATE_INVALID:" + field
                    )
            recovery_record = data.get("migration_recovery_record")
            if not isinstance(recovery_record, dict):
                raise AssuranceDenied("IMMUNE_RECOVERY_RECORD_MISSING")
            if recovery_record.get("schema") != IMMUNE_RECOVERY_RECORD_SCHEMA:
                raise AssuranceDenied("IMMUNE_RECOVERY_RECORD_SCHEMA_INVALID")
            if recovery_record.get("migration_id") != data.get("migration_id"):
                raise AssuranceDenied("IMMUNE_RECOVERY_RECORD_MIGRATION_MISMATCH")
            for field in (
                "migration_subject_sha256",
                "entry_authorization_fingerprint",
                "prestate_fingerprint",
                "consumed_ledger_sha256",
                "installation_plan_sha256",
                "entry_nonce_sha256",
            ):
                if not HEX64_RE.fullmatch(str(recovery_record.get(field) or "")):
                    raise AssuranceDenied("IMMUNE_RECOVERY_RECORD_INVALID:" + field)
            if recovery_record["consumed_ledger_sha256"] != data["migration_consumed_ledger_sha256"]:
                raise AssuranceDenied("IMMUNE_RECOVERY_RECORD_LEDGER_MISMATCH")
            if (
                not isinstance(recovery_record.get("post_entry_authority_epoch"), int)
                or recovery_record["post_entry_authority_epoch"] < 1
                or recovery_record["post_entry_authority_epoch"] > data["authority_epoch"]
            ):
                raise AssuranceDenied("IMMUNE_RECOVERY_RECORD_EPOCH_INVALID")
            consumptions = recovery_record.get("recovery_consumptions")
            if not isinstance(consumptions, list):
                raise AssuranceDenied("IMMUNE_RECOVERY_CONSUMPTIONS_INVALID")
            ids = []
            nonces = []
            for item in consumptions:
                if not isinstance(item, dict):
                    raise AssuranceDenied("IMMUNE_RECOVERY_CONSUMPTION_INVALID")
                cid = str(item.get("recovery_consumption_id") or "")
                nh = str(item.get("recovery_nonce_sha256") or "")
                if not HEX64_RE.fullmatch(cid) or not HEX64_RE.fullmatch(nh):
                    raise AssuranceDenied("IMMUNE_RECOVERY_CONSUMPTION_ID_INVALID")
                ids.append(cid); nonces.append(nh)
            if len(ids) != len(set(ids)) or len(nonces) != len(set(nonces)):
                raise AssuranceDenied("IMMUNE_RECOVERY_CONSUMPTION_REPLAY_STATE")

        if data["state"] == "IMMUNE_ENFORCED":
            proof = data.get("immune_activation_proof_sha256")
            if not HEX64_RE.fullmatch(str(proof or "")):
                raise AssuranceDenied(
                    "IMMUNE_BINDING_STATE_INVALID:immune_activation_proof_sha256"
                )

        if data["state"] == "IMMUNE_QUARANTINED":
            reason = data.get("quarantine_reason_sha256")
            scope = data.get("quarantine_scope_sha256")
            if not HEX64_RE.fullmatch(str(reason or "")):
                raise AssuranceDenied(
                    "IMMUNE_QUARANTINE_REASON_INVALID"
                )
            if not HEX64_RE.fullmatch(str(scope or "")):
                raise AssuranceDenied(
                    "IMMUNE_QUARANTINE_SCOPE_INVALID"
                )
        return data

    def initialize_bootstrap(
        self, *, external_anchor_proof: str, authority_epoch: int = 1
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            if state["state"] != "UNINITIALIZED":
                raise AssuranceDenied("INITIALIZE_FROM_NONINITIAL_STATE")
            if len(external_anchor_proof) < 32:
                raise AssuranceDenied("EXTERNAL_ANCHOR_PROOF_INVALID")
            if authority_epoch < 1:
                raise AssuranceDenied("AUTHORITY_EPOCH_INVALID")
            new = {
                "schema": STATE_SCHEMA,
                "state": "BOOTSTRAP_ONLY",
                "authority_epoch": authority_epoch,
                "consumed": [],
                "anchor_proof_sha256": sha256_hex(
                    external_anchor_proof.encode("utf-8")
                ),
            }
            _atomic_json(self.state_path, new)
            return new

    @staticmethod
    def _validate_legacy_permit(permit: dict[str, Any]) -> None:
        required = (
            "permit_id",
            "campaign_id",
            "package_class",
            "source_pre_head",
            "package_sha256",
            "touched_paths_sha256",
            "nonce",
            "authority_epoch",
        )
        if permit.get("schema") != PERMIT_SCHEMA:
            raise AssuranceDenied("PERMIT_SCHEMA_INVALID")
        for field in required:
            if permit.get(field) in (None, ""):
                raise AssuranceDenied("PERMIT_FIELD_MISSING:" + field)
        AssuranceKernel._validate_common_permit_fields(permit)
        doctor_hash = permit.get("doctor_receipt_sha256")
        if doctor_hash not in (None, "") and not HEX64_RE.fullmatch(
            str(doctor_hash)
        ):
            raise AssuranceDenied("DOCTOR_RECEIPT_SHA256_INVALID")
        trust_path = permit.get("doctor_trust_object_path")
        if trust_path not in (None, "") and not isinstance(trust_path, str):
            raise AssuranceDenied("DOCTOR_TRUST_OBJECT_PATH_INVALID")

    @staticmethod
    def _validate_immune_permit(permit: dict[str, Any]) -> None:
        required = (
            "permit_id",
            "campaign_id",
            "package_class",
            "source_pre_head",
            "package_sha256",
            "touched_paths_sha256",
            "quarantine_scope_sha256",
            "risk_profile",
            "intended_consequence_class",
            "nonce",
            "authority_epoch",
            "producer_identity",
            "attestation_path",
        )
        if permit.get("schema") != IMMUNE_PERMIT_SCHEMA:
            raise AssuranceDenied("IMMUNE_PERMIT_SCHEMA_INVALID")
        for field in required:
            if permit.get(field) in (None, ""):
                raise AssuranceDenied(
                    "IMMUNE_PERMIT_FIELD_MISSING:" + field
                )
        AssuranceKernel._validate_common_permit_fields(permit)
        if not HEX64_RE.fullmatch(
            str(permit["quarantine_scope_sha256"])
        ):
            raise AssuranceDenied(
                "IMMUNE_PERMIT_QUARANTINE_SCOPE_INVALID"
            )
        if permit["intended_consequence_class"] not in {
            "SOURCE_EFFECT",
            "RUNTIME_EFFECT",
            "SHARED_EFFECT",
            "KNOWLEDGE_ONLY",
        }:
            raise AssuranceDenied(
                "IMMUNE_PERMIT_CONSEQUENCE_CLASS_INVALID"
            )
        if (
            permit.get("material_consumer_identity")
            not in (None, "STANDARD_DELIVERY")
        ):
            raise AssuranceDenied(
                "IMMUNE_MATERIAL_CONSUMER_IDENTITY_INVALID"
            )

    @staticmethod
    def _validate_common_permit_fields(permit: dict[str, Any]) -> None:
        if not HEX40_RE.fullmatch(str(permit["source_pre_head"])):
            raise AssuranceDenied("SOURCE_HEAD_FORMAT_INVALID")
        for field in ("package_sha256", "touched_paths_sha256"):
            if not HEX64_RE.fullmatch(str(permit[field])):
                raise AssuranceDenied("SHA256_FORMAT_INVALID:" + field)
        if len(str(permit["nonce"])) < 16:
            raise AssuranceDenied("NONCE_TOO_SHORT")
        if (
            not isinstance(permit["authority_epoch"], int)
            or permit["authority_epoch"] < 1
        ):
            raise AssuranceDenied("AUTHORITY_EPOCH_INVALID")

    @staticmethod
    def _validate_intent(intent: MaterialIntent) -> None:
        if not HEX40_RE.fullmatch(str(intent.source_head)):
            raise AssuranceDenied("INTENT_SOURCE_HEAD_INVALID")
        if not HEX64_RE.fullmatch(str(intent.package_sha256)):
            raise AssuranceDenied("INTENT_PACKAGE_SHA256_INVALID")
        if not HEX64_RE.fullmatch(str(intent.touched_paths_sha256)):
            raise AssuranceDenied("INTENT_PATHS_SHA256_INVALID")
        if not intent.package_class or not intent.campaign_id:
            raise AssuranceDenied("INTENT_IDENTITY_MISSING")
        if (
            not isinstance(intent.authority_epoch, int)
            or intent.authority_epoch < 1
        ):
            raise AssuranceDenied("INTENT_AUTHORITY_EPOCH_INVALID")

    @staticmethod
    def _trust_fingerprint_payload(
        trust: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(trust)
        result.pop("trust_object_fingerprint", None)
        return result

    @staticmethod
    def _trust_semantic_core(
        trust: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            key: value
            for key, value in trust.items()
            if key not in {"attestation", "trust_object_fingerprint"}
        }

    @staticmethod
    def _attestation_unsigned(
        attestation: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(attestation)
        result.pop("signature", None)
        return result

    def _verify_doctor_trust(
        self,
        state: dict[str, Any],
        permit: dict[str, Any],
        intent: MaterialIntent,
    ) -> dict[str, Any]:
        receipt_fingerprint = str(
            permit.get("doctor_receipt_sha256") or ""
        )
        if not HEX64_RE.fullmatch(receipt_fingerprint):
            raise AssuranceDenied("DOCTOR_RECEIPT_REQUIRED")
        trust_path = str(permit.get("doctor_trust_object_path") or "")
        if not trust_path:
            raise AssuranceDenied("DOCTOR_TRUST_OBJECT_REQUIRED")

        key_path = Path(str(state["doctor_trust_key_path"]))
        verifier_path = Path(str(state["doctor_verifier_path"]))
        trust_file = Path(trust_path)
        if not key_path.is_file():
            raise AssuranceDenied("DOCTOR_TRUST_KEY_MISSING")
        if not verifier_path.is_file():
            raise AssuranceDenied("DOCTOR_VERIFIER_MISSING")
        if file_sha256(key_path) != str(
            state["doctor_trust_key_sha256"]
        ):
            raise AssuranceDenied("DOCTOR_TRUST_KEY_DRIFT")
        if file_sha256(verifier_path) != str(
            state["doctor_verifier_sha256"]
        ):
            raise AssuranceDenied("DOCTOR_VERIFIER_DRIFT")
        if not trust_file.is_file():
            raise AssuranceDenied("DOCTOR_TRUST_OBJECT_MISSING")
        try:
            trust = json.loads(
                trust_file.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise AssuranceDenied(
                "DOCTOR_TRUST_OBJECT_INVALID_JSON"
            ) from exc
        if (
            trust.get("schema") != TRUST_OBJECT_SCHEMA
            or trust.get("state") != "CERTIFIED_CURRENT"
        ):
            raise AssuranceDenied(
                "DOCTOR_TRUST_OBJECT_STATE_INVALID"
            )
        attestation = trust.get("attestation") or {}
        if (
            attestation.get("schema")
            != TRUST_ATTESTATION_SCHEMA
            or attestation.get("algorithm") != "HMAC-SHA256"
            or attestation.get("validation_result") != "PASS"
        ):
            raise AssuranceDenied(
                "DOCTOR_ATTESTATION_CONTRACT_INVALID"
            )
        if attestation.get("implementation_sha256") != str(
            state["doctor_verifier_sha256"]
        ):
            raise AssuranceDenied(
                "DOCTOR_ATTESTATION_VERIFIER_MISMATCH"
            )
        if attestation.get("key_sha256") != str(
            state["doctor_trust_key_sha256"]
        ):
            raise AssuranceDenied(
                "DOCTOR_ATTESTATION_KEY_MISMATCH"
            )
        if attestation.get("receipt_fingerprint") != trust.get(
            "receipt_fingerprint"
        ):
            raise AssuranceDenied(
                "DOCTOR_ATTESTATION_RECEIPT_MISMATCH"
            )
        if int(trust.get("authority_epoch") or 0) != int(
            state["authority_epoch"]
        ):
            raise AssuranceDenied("DOCTOR_TRUST_EPOCH_MISMATCH")
        if trust.get("receipt_fingerprint") != receipt_fingerprint:
            raise AssuranceDenied("DOCTOR_TRUST_RECEIPT_MISMATCH")

        subject = trust.get("subject") or {}
        for field, expected in (
            ("source_pre_head", intent.source_head),
            ("package_sha256", intent.package_sha256),
            ("touched_paths_sha256", intent.touched_paths_sha256),
        ):
            if str(subject.get(field) or "") != str(expected):
                raise AssuranceDenied(
                    "DOCTOR_TRUST_SUBJECT_MISMATCH:" + field
                )
        if "SOURCE_PROMOTION" not in list(
            trust.get("claim_scope") or []
        ):
            raise AssuranceDenied(
                "DOCTOR_TRUST_CLAIM_SCOPE_DENIED"
            )

        signature = str(attestation.get("signature") or "")
        if not HEX64_RE.fullmatch(signature):
            raise AssuranceDenied(
                "DOCTOR_TRUST_SIGNATURE_INVALID"
            )
        expected_signature = hmac.new(
            key_path.read_bytes(),
            canonical(
                {
                    "trust": self._trust_semantic_core(trust),
                    "attestation": self._attestation_unsigned(
                        attestation
                    ),
                }
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise AssuranceDenied(
                "DOCTOR_TRUST_SIGNATURE_INVALID"
            )

        fingerprint = str(
            trust.get("trust_object_fingerprint") or ""
        )
        expected_fingerprint = sha256_hex(
            canonical(self._trust_fingerprint_payload(trust))
        )
        if (
            not HEX64_RE.fullmatch(fingerprint)
            or fingerprint != expected_fingerprint
        ):
            raise AssuranceDenied(
                "DOCTOR_TRUST_OBJECT_FINGERPRINT_INVALID"
            )
        return trust

    @staticmethod
    def _immune_subject(
        permit: dict[str, Any], intent: MaterialIntent
    ) -> dict[str, Any]:
        return {
            "permit_id": str(permit["permit_id"]),
            "campaign_id": intent.campaign_id,
            "source_pre_head": intent.source_head,
            "package_sha256": intent.package_sha256,
            "touched_paths_sha256": intent.touched_paths_sha256,
            "quarantine_scope_sha256": str(
                permit["quarantine_scope_sha256"]
            ),
            "risk_profile": str(permit["risk_profile"]),
            "intended_consequence_class": str(
                permit["intended_consequence_class"]
            ),
            "authority_epoch": intent.authority_epoch,
            "producer_identity": str(permit["producer_identity"]),
        }

    def _verify_immune_attestation(
        self,
        state: dict[str, Any],
        permit: dict[str, Any],
        intent: MaterialIntent,
    ) -> dict[str, Any]:
        key_path = Path(str(state["immune_attestor_key_path"]))
        implementation_path = Path(
            str(state["immune_attestor_path"])
        )
        attestation_path = Path(str(permit["attestation_path"]))
        if not key_path.is_file():
            raise AssuranceDenied("IMMUNE_ATTESTOR_KEY_MISSING")
        if not implementation_path.is_file():
            raise AssuranceDenied(
                "IMMUNE_ATTESTOR_IMPLEMENTATION_MISSING"
            )
        if not attestation_path.is_file():
            raise AssuranceDenied(
                "IMMUNE_ATTESTATION_MISSING"
            )
        if file_sha256(implementation_path) != str(
            state["immune_attestor_sha256"]
        ):
            raise AssuranceDenied(
                "IMMUNE_ATTESTOR_IMPLEMENTATION_DRIFT"
            )
        if sha256_hex(key_path.read_bytes()) != str(
            state["immune_attestor_key_fingerprint"]
        ):
            raise AssuranceDenied("IMMUNE_ATTESTOR_KEY_DRIFT")
        if len(key_path.read_bytes()) < 32:
            raise AssuranceDenied("IMMUNE_ATTESTOR_KEY_INVALID")

        module = _load_module(
            "cerebro_immune_attestation_runtime",
            implementation_path,
        )
        if not hasattr(module, "verify_attestation"):
            raise AssuranceDenied(
                "IMMUNE_ATTESTOR_INTERFACE_INVALID"
            )
        try:
            attestation = json.loads(
                attestation_path.read_text(encoding="utf-8")
            )
            result = module.verify_attestation(
                attestation,
                key=key_path.read_bytes(),
                implementation_path=implementation_path,
                expected_subject=self._immune_subject(
                    permit, intent
                ),
            )
        except Exception as exc:
            raise AssuranceDenied(
                "IMMUNE_ATTESTATION_INVALID:" + str(exc)
            ) from exc
        if result.get("result") != "PASS":
            raise AssuranceDenied("IMMUNE_ATTESTATION_NOT_PASS")
        fingerprint = str(
            result.get("attestation_fingerprint") or ""
        )
        if not HEX64_RE.fullmatch(fingerprint):
            raise AssuranceDenied(
                "IMMUNE_ATTESTATION_FINGERPRINT_INVALID"
            )
        return result

    def _check_against_state(
        self,
        state: dict[str, Any],
        permit: dict[str, Any],
        intent: MaterialIntent,
    ) -> dict[str, Any]:
        self._validate_intent(intent)
        if state["state"] in {
            "UNINITIALIZED",
            "FAILED_RECOVERY",
            "IMMUNE_QUARANTINED",
        }:
            raise AssuranceDenied("KERNEL_NOT_ENFORCING")
        if state["state"] == "IMMUNE_MIGRATING":
            raise AssuranceDenied(
                "IMMUNE_MIGRATION_NOT_FINALIZED"
            )
        if int(permit.get("authority_epoch") or 0) != int(
            state["authority_epoch"]
        ) or intent.authority_epoch != int(
            state["authority_epoch"]
        ):
            raise AssuranceDenied("STALE_AUTHORITY_EPOCH")

        pairs = {
            "campaign_id": intent.campaign_id,
            "package_class": intent.package_class,
            "source_pre_head": intent.source_head,
            "package_sha256": intent.package_sha256,
            "touched_paths_sha256": intent.touched_paths_sha256,
        }
        for key, actual in pairs.items():
            if str(permit.get(key) or "") != str(actual):
                raise AssuranceDenied(
                    "BINDING_MISMATCH:" + key
                )

        consumption_id = sha256_hex(
            canonical(
                {
                    "permit_id": permit.get("permit_id"),
                    "nonce": permit.get("nonce"),
                    "intent": pairs,
                    "epoch": intent.authority_epoch,
                }
            )
        )
        if consumption_id in state["consumed"]:
            raise AssuranceDenied("PERMIT_REPLAY")

        if state["state"] == "BOOTSTRAP_ONLY":
            self._validate_legacy_permit(permit)
            if intent.package_class != BOOTSTRAP_PACKAGE_CLASS:
                raise AssuranceDenied(
                    "BOOTSTRAP_PACKAGE_CLASS_DENIED"
                )
            if len(state["consumed"]) >= 1:
                raise AssuranceDenied(
                    "BOOTSTRAP_CONSUMPTION_EXHAUSTED"
                )
            return {
                "schema": RECEIPT_SCHEMA,
                "result": "ALLOW",
                "reason": "CURRENT_EXACT_PERMIT",
                "permit_id": str(permit["permit_id"]),
                "campaign_id": intent.campaign_id,
                "package_sha256": intent.package_sha256,
                "authority_epoch": intent.authority_epoch,
                "consumption_id": consumption_id,
                "kernel_state": state["state"],
            }

        if state["state"] == "DOCTOR_ENFORCED":
            self._validate_legacy_permit(permit)
            trust = self._verify_doctor_trust(
                state, permit, intent
            )
            return {
                "schema": RECEIPT_SCHEMA,
                "result": "ALLOW",
                "reason": "CURRENT_EXACT_PERMIT",
                "permit_id": str(permit["permit_id"]),
                "campaign_id": intent.campaign_id,
                "package_sha256": intent.package_sha256,
                "authority_epoch": intent.authority_epoch,
                "consumption_id": consumption_id,
                "kernel_state": state["state"],
                "doctor_trust_object_fingerprint": trust[
                    "trust_object_fingerprint"
                ],
            }

        if state["state"] != "IMMUNE_ENFORCED":
            raise AssuranceDenied("KERNEL_NOT_ENFORCING")

        self._validate_immune_permit(permit)
        attestation = self._verify_immune_attestation(
            state, permit, intent
        )
        return {
            "schema": IMMUNE_RECEIPT_SCHEMA,
            "result": "ALLOW",
            "reason": "CURRENT_EXACT_IMMUNE_PERMIT",
            "permit_id": str(permit["permit_id"]),
            "campaign_id": intent.campaign_id,
            "package_sha256": intent.package_sha256,
            "authority_epoch": intent.authority_epoch,
            "consumption_id": consumption_id,
            "kernel_state": state["state"],
            "assurance_profile": "IMMUNE",
            "material_consumer_identity": "STANDARD_DELIVERY",
            "attestation_fingerprint": attestation[
                "attestation_fingerprint"
            ],
            "quarantine_scope_sha256": str(
                permit["quarantine_scope_sha256"]
            ),
            "risk_profile": str(permit["risk_profile"]),
            "intended_consequence_class": str(
                permit["intended_consequence_class"]
            ),
        }

    def check(
        self, permit: dict[str, Any], intent: MaterialIntent
    ) -> dict[str, Any]:
        return self._check_against_state(
            self._read(), permit, intent
        )

    def consume(
        self, permit: dict[str, Any], intent: MaterialIntent
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            current = self._read()
            receipt = self._check_against_state(
                current, permit, intent
            )
            current["consumed"] = list(
                current["consumed"]
            ) + [receipt["consumption_id"]]
            _atomic_json(self.state_path, current)
            return receipt

    def transition_doctor_enforced(
        self,
        *,
        active_path_proof_sha256: str,
        expected_epoch: int,
        trust_key_path: str,
        doctor_verifier_path: str,
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            if state["state"] != "BOOTSTRAP_ONLY":
                raise AssuranceDenied(
                    "DOCTOR_TRANSITION_FROM_INVALID_STATE"
                )
            if len(state["consumed"]) != 1:
                raise AssuranceDenied(
                    "DOCTOR_BOOTSTRAP_NOT_EXACTLY_ONCE"
                )
            if not HEX64_RE.fullmatch(
                active_path_proof_sha256
            ):
                raise AssuranceDenied(
                    "DOCTOR_ACTIVE_PATH_PROOF_INVALID"
                )
            if int(state["authority_epoch"]) != expected_epoch:
                raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
            key_path = Path(trust_key_path)
            verifier_path = Path(doctor_verifier_path)
            if (
                not key_path.is_file()
                or len(key_path.read_bytes()) < 32
            ):
                raise AssuranceDenied("DOCTOR_TRUST_KEY_INVALID")
            if not verifier_path.is_file():
                raise AssuranceDenied("DOCTOR_VERIFIER_INVALID")
            state["state"] = "DOCTOR_ENFORCED"
            state["authority_epoch"] = expected_epoch + 1
            state[
                "doctor_active_path_proof_sha256"
            ] = active_path_proof_sha256
            state["doctor_trust_key_path"] = str(
                key_path.resolve()
            )
            state["doctor_trust_key_sha256"] = file_sha256(
                key_path
            )
            state["doctor_verifier_path"] = str(
                verifier_path.resolve()
            )
            state["doctor_verifier_sha256"] = file_sha256(
                verifier_path
            )
            _atomic_json(self.state_path, state)
            return state

    def transition_failed_recovery(
        self, *, reason_sha256: str, expected_epoch: int
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            if state["state"] not in {
                "BOOTSTRAP_ONLY",
                "DOCTOR_ENFORCED",
            }:
                raise AssuranceDenied(
                    "FAILED_RECOVERY_FROM_INVALID_STATE"
                )
            if not HEX64_RE.fullmatch(reason_sha256):
                raise AssuranceDenied(
                    "RECOVERY_REASON_SHA256_INVALID"
                )
            if int(state["authority_epoch"]) != expected_epoch:
                raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
            state["state"] = "FAILED_RECOVERY"
            state["authority_epoch"] = expected_epoch + 1
            state["recovery_reason_sha256"] = reason_sha256
            _atomic_json(self.state_path, state)
            return state

    def transition_one_time_human_admin_first_activation(
        self,
        authorization: dict[str, Any],
        *,
        expected_epoch: int,
        manifest_path: str,
        working_source_path: str,
        immune_attestor_path: str,
    ) -> dict[str, Any]:
        """Consume the ingress785 Human Admin bootstrap authority exactly once."""
        with _exclusive_lock(self.lock_path):
            state = self._read()
            if state["state"] != "DOCTOR_ENFORCED":
                raise AssuranceDenied("FIRST_ACTIVATION_FROM_INVALID_STATE")
            if not state["consumed"]:
                raise AssuranceDenied("FIRST_ACTIVATION_DELIVERY_NOT_CONSUMED")
            if int(state["authority_epoch"]) != expected_epoch:
                raise AssuranceDenied("STALE_AUTHORITY_EPOCH")

            required = (
                "authorization_id", "authorization_type", "directive",
                "candidate_parent_fingerprint", "candidate_identity",
                "source_base_commit", "source_current_commit", "source_current_tree",
                "working_source_path", "host_fingerprint", "migration_id", "nonce",
                "authority_epoch", "delivery_consumption_id", "immune_attestor_path",
                "immune_attestor_sha256", "immune_attestor_key_path",
                "immune_attestor_key_fingerprint", "authoritative_source", "branch",
                "remote_equality_verified",
            )
            if authorization.get("schema") != (
                ONE_TIME_HUMAN_ADMIN_FIRST_ACTIVATION_SCHEMA
            ):
                raise AssuranceDenied(
                    "FIRST_ACTIVATION_AUTHORIZATION_SCHEMA_INVALID"
                )
            for field in required:
                if authorization.get(field) in (None, ""):
                    raise AssuranceDenied(
                        "FIRST_ACTIVATION_AUTHORIZATION_FIELD_MISSING:" + field
                    )
            exact = {
                "authorization_type": ONE_TIME_HUMAN_ADMIN_FIRST_ACTIVATION,
                "directive": FIRST_ACTIVATION_DIRECTIVE,
                "candidate_parent_fingerprint": (
                    FIRST_ACTIVATION_PARENT_CANDIDATE_FINGERPRINT
                ),
                "source_base_commit": FIRST_ACTIVATION_BASE_COMMIT,
                "authoritative_source": "origin/main",
                "branch": "main",
                "remote_equality_verified": True,
                "authority_epoch": expected_epoch,
                "delivery_consumption_id": state["consumed"][-1],
            }
            for field, value in exact.items():
                if authorization.get(field) != value:
                    raise AssuranceDenied(
                        "FIRST_ACTIVATION_AUTHORIZATION_MISMATCH:" + field
                    )
            for field in (
                "candidate_parent_fingerprint", "candidate_identity", "host_fingerprint",
                "immune_attestor_sha256", "immune_attestor_key_fingerprint",
            ):
                if not HEX64_RE.fullmatch(str(authorization[field])):
                    raise AssuranceDenied("FIRST_ACTIVATION_SHA256_INVALID:" + field)
            for field in ("source_base_commit", "source_current_commit", "source_current_tree"):
                if not HEX40_RE.fullmatch(str(authorization[field])):
                    raise AssuranceDenied(
                        "FIRST_ACTIVATION_GIT_IDENTITY_INVALID:" + field
                    )
            if len(str(authorization["nonce"])) < 32:
                raise AssuranceDenied("FIRST_ACTIVATION_NONCE_INVALID")
            if not str(authorization["migration_id"]).startswith(
                "IMMUNE-FIRST-ACTIVATION-"
            ):
                raise AssuranceDenied("FIRST_ACTIVATION_MIGRATION_ID_INVALID")

            source_root = Path(working_source_path).resolve()
            if str(source_root).lower() != str(
                Path(str(authorization["working_source_path"])).resolve()
            ).lower():
                raise AssuranceDenied("FIRST_ACTIVATION_WORKING_SOURCE_MISMATCH")
            if current_host_fingerprint(source_root) != str(
                authorization["host_fingerprint"]
            ):
                raise AssuranceDenied("FIRST_ACTIVATION_HOST_MISMATCH")

            manifest_identity = candidate_identity_from_manifest(manifest_path)
            if manifest_identity != str(authorization["candidate_identity"]):
                raise AssuranceDenied("FIRST_ACTIVATION_CANDIDATE_IDENTITY_MISMATCH")
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            if str(manifest.get("expected_base_commit") or "") != (
                FIRST_ACTIVATION_BASE_COMMIT
            ):
                raise AssuranceDenied("FIRST_ACTIVATION_BASE_COMMIT_MISMATCH")

            if _git_value(source_root, "branch", "--show-current") != "main":
                raise AssuranceDenied("FIRST_ACTIVATION_BRANCH_MISMATCH")
            if _git_value(source_root, "status", "--porcelain", "--untracked-files=all"):
                raise AssuranceDenied("FIRST_ACTIVATION_WORKTREE_NOT_CLEAN")
            remote_url = _git_value(source_root, "remote", "get-url", "origin")
            if "morgul-tech/Cerebro-Source-1.0" not in remote_url:
                raise AssuranceDenied("FIRST_ACTIVATION_REMOTE_MISMATCH")
            _git_value(source_root, "fetch", "--no-tags", "origin", "main")
            local_head = _git_value(source_root, "rev-parse", "HEAD")
            remote_head = _git_value(source_root, "rev-parse", "refs/remotes/origin/main")
            source_tree = _git_value(source_root, "rev-parse", "HEAD^{tree}")
            if local_head != remote_head:
                raise AssuranceDenied("FIRST_ACTIVATION_REMOTE_EQUALITY_FAILED")
            if local_head != str(authorization["source_current_commit"]):
                raise AssuranceDenied("FIRST_ACTIVATION_SOURCE_HEAD_MISMATCH")
            if source_tree != str(authorization["source_current_tree"]):
                raise AssuranceDenied("FIRST_ACTIVATION_SOURCE_TREE_MISMATCH")

            attestor = Path(immune_attestor_path).resolve()
            attestor_key = Path(str(state.get("doctor_trust_key_path") or "")).resolve()
            if str(attestor).lower() != str(
                Path(str(authorization["immune_attestor_path"])).resolve()
            ).lower():
                raise AssuranceDenied("FIRST_ACTIVATION_ATTESTOR_PATH_MISMATCH")
            if str(attestor_key).lower() != str(
                Path(str(authorization["immune_attestor_key_path"])).resolve()
            ).lower():
                raise AssuranceDenied("FIRST_ACTIVATION_ATTESTOR_KEY_PATH_MISMATCH")
            if not attestor.is_file() or file_sha256(attestor) != str(
                authorization["immune_attestor_sha256"]
            ):
                raise AssuranceDenied("FIRST_ACTIVATION_ATTESTOR_BINDING_INVALID")
            if not attestor_key.is_file() or len(attestor_key.read_bytes()) < 32:
                raise AssuranceDenied("FIRST_ACTIVATION_ATTESTOR_KEY_INVALID")
            if sha256_hex(attestor_key.read_bytes()) != str(
                authorization["immune_attestor_key_fingerprint"]
            ):
                raise AssuranceDenied("FIRST_ACTIVATION_ATTESTOR_KEY_DRIFT")
            if file_sha256(attestor_key) != str(
                state.get("doctor_trust_key_sha256") or ""
            ):
                raise AssuranceDenied("FIRST_ACTIVATION_DOCTOR_KEY_BINDING_DRIFT")
            doctor_verifier = Path(str(state.get("doctor_verifier_path") or ""))
            if not doctor_verifier.is_file() or file_sha256(doctor_verifier) != str(
                state.get("doctor_verifier_sha256") or ""
            ):
                raise AssuranceDenied("FIRST_ACTIVATION_DOCTOR_VERIFIER_BINDING_DRIFT")

            authorization_fingerprint = sha256_hex(canonical(authorization))
            prestate_fingerprint = sha256_hex(canonical(state))
            consumed_fingerprint = ledger_fingerprint(state["consumed"])
            activation_subject = {
                "authorization_fingerprint": authorization_fingerprint,
                "candidate_identity": manifest_identity,
                "source_current_commit": local_head,
                "source_current_tree": source_tree,
                "host_fingerprint": authorization["host_fingerprint"],
                "migration_id": authorization["migration_id"],
                "nonce_sha256": sha256_hex(str(authorization["nonce"]).encode("utf-8")),
                "attestor_sha256": authorization["immune_attestor_sha256"],
                "attestor_key_fingerprint": authorization[
                    "immune_attestor_key_fingerprint"
                ],
                "delivery_consumption_id": state["consumed"][-1],
            }
            activation_proof = sha256_hex(canonical(activation_subject))
            verifier_path = Path(__file__).resolve()
            migration_id = str(authorization["migration_id"])
            state.update(
                {
                    "state": "IMMUNE_ENFORCED",
                    "authority_epoch": expected_epoch + 1,
                    "migration_id": migration_id,
                    "migration_source_state": "DOCTOR_ENFORCED",
                    "migration_source_head": local_head,
                    "migration_source_tree": source_tree,
                    "migration_consumed_ledger_sha256": consumed_fingerprint,
                    "migration_receipt_sha256": authorization_fingerprint,
                    "migration_campaign_id": "P0_CEREBRO_IMMUNFORSVAR",
                    "migration_package_sha256": file_sha256(manifest_path),
                    "migration_touched_paths_sha256": touched_paths_fingerprint(
                        [str(item["path"]) for item in manifest["files"]]
                    ),
                    "migration_current_host_proof_sha256": str(
                        authorization["host_fingerprint"]
                    ),
                    "migration_installation_plan_sha256": manifest_identity,
                    "migration_producer_identity": "IMPLEMENTER-L3-574DAB6D",
                    "migration_recovery_record": {
                        "schema": IMMUNE_RECOVERY_RECORD_SCHEMA,
                        "migration_id": migration_id,
                        "migration_subject_sha256": activation_proof,
                        "entry_authorization_fingerprint": authorization_fingerprint,
                        "prestate_fingerprint": prestate_fingerprint,
                        "post_entry_authority_epoch": expected_epoch + 1,
                        "consumed_ledger_sha256": consumed_fingerprint,
                        "installation_plan_sha256": manifest_identity,
                        "entry_nonce_sha256": sha256_hex(
                            str(authorization["nonce"]).encode("utf-8")
                        ),
                        "recovery_consumptions": [],
                    },
                    "external_anchor_id": FIRST_ACTIVATION_DIRECTIVE,
                    "external_anchor_fingerprint": authorization_fingerprint,
                    "external_anchor_verifier_path": str(verifier_path),
                    "external_anchor_verifier_sha256": file_sha256(verifier_path),
                    "immune_attestor_path": str(attestor),
                    "immune_attestor_sha256": file_sha256(attestor),
                    "immune_attestor_key_path": str(attestor_key),
                    "immune_attestor_key_fingerprint": sha256_hex(
                        attestor_key.read_bytes()
                    ),
                    "immune_activation_proof_sha256": activation_proof,
                    "one_time_human_admin_authorization_fingerprint": (
                        authorization_fingerprint
                    ),
                    "one_time_human_admin_first_activation_consumed": True,
                    "one_time_human_admin_directive": FIRST_ACTIVATION_DIRECTIVE,
                    "migrated_existing_doctor_key_without_new_secret": True,
                }
            )
            for legacy_field in (
                "doctor_active_path_proof_sha256",
                "doctor_trust_key_path",
                "doctor_trust_key_sha256",
                "doctor_verifier_path",
                "doctor_verifier_sha256",
            ):
                state.pop(legacy_field, None)
            _atomic_json(self.state_path, state)
            return state

    @staticmethod
    def _validate_migration(
        migration: dict[str, Any]
    ) -> None:
        required = (
            "migration_id", "operation", "source_state", "target_state",
            "source_repository", "source_branch", "source_head", "source_tree",
            "authority_epoch", "consumed_ledger_sha256", "campaign_id",
            "package_sha256", "touched_paths_sha256", "mcp_decision_fingerprint",
            "current_host_proof_sha256", "quarantine_scope_sha256", "risk_profile",
            "prestate_manifest_sha256", "rollback_plan_sha256",
            "installation_plan_path", "installation_plan_sha256", "producer_identity",
            "external_anchor_id", "external_anchor_fingerprint",
            "external_anchor_preexisting", "external_anchor_outside_targetset",
            "external_anchor_verifier_path", "external_anchor_verifier_sha256",
            "external_anchor_attestation_path", "immune_attestor_path",
            "immune_attestor_sha256", "immune_attestor_key_path",
            "immune_attestor_key_fingerprint", "claim_scope", "nonce",
        )
        if migration.get("schema") != IMMUNE_MIGRATION_SCHEMA:
            raise AssuranceDenied("IMMUNE_MIGRATION_SCHEMA_INVALID")
        for field in required:
            if migration.get(field) in (None, ""):
                raise AssuranceDenied("IMMUNE_MIGRATION_FIELD_MISSING:" + field)
        if migration["operation"] != "IMMUNE_MIGRATION":
            raise AssuranceDenied("IMMUNE_MIGRATION_OPERATION_INVALID")
        if migration["source_state"] not in {"DOCTOR_ENFORCED", "FAILED_RECOVERY"}:
            raise AssuranceDenied("IMMUNE_MIGRATION_SOURCE_STATE_INVALID")
        if migration["target_state"] != "IMMUNE_MIGRATING":
            raise AssuranceDenied("IMMUNE_MIGRATION_TARGET_STATE_INVALID")
        if (migration["source_repository"] != "morgul-tech/Cerebro-Source-1.0"
                or migration["source_branch"] != "main"):
            raise AssuranceDenied("IMMUNE_MIGRATION_SOURCE_IDENTITY_INVALID")
        for field in ("source_head", "source_tree"):
            if not HEX40_RE.fullmatch(str(migration[field])):
                raise AssuranceDenied("IMMUNE_MIGRATION_GIT_IDENTITY_INVALID:" + field)
        for field in (
            "consumed_ledger_sha256", "package_sha256", "touched_paths_sha256",
            "mcp_decision_fingerprint", "current_host_proof_sha256",
            "quarantine_scope_sha256", "prestate_manifest_sha256",
            "rollback_plan_sha256", "installation_plan_sha256",
            "external_anchor_fingerprint", "external_anchor_verifier_sha256",
            "immune_attestor_sha256", "immune_attestor_key_fingerprint",
        ):
            if not HEX64_RE.fullmatch(str(migration[field])):
                raise AssuranceDenied("IMMUNE_MIGRATION_SHA256_INVALID:" + field)
        if migration["external_anchor_preexisting"] is not True:
            raise AssuranceDenied("EXTERNAL_ANCHOR_PREEXISTENCE_REQUIRED")
        if migration["external_anchor_outside_targetset"] is not True:
            raise AssuranceDenied("EXTERNAL_ANCHOR_OUTSIDE_TARGETSET_REQUIRED")
        if "SOURCE_PROMOTION" not in list(migration["claim_scope"]):
            raise AssuranceDenied("IMMUNE_MIGRATION_CLAIM_SCOPE_DENIED")
        nonce = str(migration["nonce"]);
        if len(nonce) < 16:
            raise AssuranceDenied("IMMUNE_MIGRATION_NONCE_TOO_SHORT")
        if nonce.startswith("RECOVERY:"):
            raise AssuranceDenied("ENTRY_NONCE_RECOVERY_NAMESPACE_PROHIBITED")
        plan_path = Path(str(migration["installation_plan_path"]))
        AssuranceKernel._load_installation_plan(plan_path, str(migration["installation_plan_sha256"]))

    @staticmethod
    def _migration_subject(migration: dict[str, Any]) -> dict[str, Any]:
        return {
            "migration_id": str(migration["migration_id"]),
            "campaign_id": str(migration["campaign_id"]),
            "source_state": str(migration["source_state"]),
            "source_repository": str(migration["source_repository"]),
            "source_branch": str(migration["source_branch"]),
            "source_head": str(migration["source_head"]),
            "source_tree": str(migration["source_tree"]),
            "package_sha256": str(migration["package_sha256"]),
            "touched_paths_sha256": str(migration["touched_paths_sha256"]),
            "authority_epoch": int(migration["authority_epoch"]),
            "consumed_ledger_sha256": str(migration["consumed_ledger_sha256"]),
            "mcp_decision_fingerprint": str(migration["mcp_decision_fingerprint"]),
            "current_host_proof_sha256": str(migration["current_host_proof_sha256"]),
            "quarantine_scope_sha256": str(migration["quarantine_scope_sha256"]),
            "risk_profile": str(migration["risk_profile"]),
            "prestate_manifest_sha256": str(migration["prestate_manifest_sha256"]),
            "rollback_plan_sha256": str(migration["rollback_plan_sha256"]),
            "installation_plan_sha256": str(migration["installation_plan_sha256"]),
            "producer_identity": str(migration["producer_identity"]),
            "external_anchor_id": str(migration["external_anchor_id"]),
            "external_anchor_fingerprint": str(migration["external_anchor_fingerprint"]),
            "nonce": str(migration["nonce"]),
        }

    @staticmethod
    def _load_installation_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
        if not path.is_file():
            raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_MISSING")
        if file_sha256(path) != expected_sha256:
            raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_DRIFT")
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_INVALID") from exc
        if not isinstance(plan, dict) or plan.get("schema") != IMMUNE_INSTALLATION_PLAN_SCHEMA:
            raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_SCHEMA_INVALID")
        entries = plan.get("entries")
        if not isinstance(entries, list) or not entries:
            raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_ENTRIES_INVALID")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_ENTRY_INVALID")
            rel = str(entry.get("path") or "").replace("\\", "/").strip("/")
            if not rel or rel in seen or ".." in Path(rel).parts:
                raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_PATH_INVALID")
            seen.add(rel)
            for field in ("pre_sha256", "final_sha256"):
                value = str(entry.get(field) or "")
                if value != "ABSENT" and not HEX64_RE.fullmatch(value):
                    raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_HASH_INVALID:" + field)
            if entry["pre_sha256"] == entry["final_sha256"]:
                raise AssuranceDenied("IMMUNE_INSTALLATION_PLAN_NOOP_ENTRY")
        return plan

    @staticmethod
    def _classify_installation_progress(
        plan: dict[str, Any], observation_path: Path, expected_sha256: str
    ) -> tuple[str, int]:
        if not observation_path.is_file():
            raise AssuranceDenied("IMMUNE_INSTALLATION_OBSERVATION_MISSING")
        if file_sha256(observation_path) != expected_sha256:
            raise AssuranceDenied("IMMUNE_INSTALLATION_OBSERVATION_DRIFT")
        try:
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AssuranceDenied("IMMUNE_INSTALLATION_OBSERVATION_INVALID") from exc
        if (not isinstance(observation, dict)
                or observation.get("schema") != IMMUNE_INSTALLATION_OBSERVATION_SCHEMA):
            raise AssuranceDenied("IMMUNE_INSTALLATION_OBSERVATION_SCHEMA_INVALID")
        observed = observation.get("entries")
        if not isinstance(observed, list) or len(observed) != len(plan["entries"]):
            raise AssuranceDenied("IMMUNE_INSTALLATION_OBSERVATION_ENTRIES_INVALID")
        phase = "FINAL"
        final_count = 0
        for expected, actual in zip(plan["entries"], observed):
            if not isinstance(actual, dict) or str(actual.get("path") or "") != str(expected["path"]):
                return "DRIFT", final_count
            value = str(actual.get("sha256") or "")
            pre = str(expected["pre_sha256"]); final = str(expected["final_sha256"])
            if value == final:
                if phase == "PRE":
                    return "DRIFT", final_count
                final_count += 1
            elif value == pre:
                phase = "PRE"
            else:
                return "DRIFT", final_count
        if final_count == 0:
            return "PRESTATE_EXACT", 0
        if final_count == len(plan["entries"]):
            return "FULLY_INSTALLED", final_count
        return "DETERMINISTIC_PREFIX", final_count

    @staticmethod
    def _validate_recovery(recovery: dict[str, Any]) -> None:
        required = (
            "migration_id", "operation", "requested_recovery_action", "authority_epoch",
            "migration_subject_sha256", "entry_authorization_fingerprint",
            "mcp_recovery_decision_sha256", "current_host_proof_sha256",
            "external_anchor_recovery_attestation_path", "recovery_attestation_path",
            "recovery_nonce", "installation_plan_path", "installation_plan_sha256",
            "installation_observation_path", "installation_observation_sha256",
            "publication_state", "quarantine_scope_sha256", "producer_identity",
        )
        if recovery.get("schema") != IMMUNE_MIGRATION_SCHEMA:
            raise AssuranceDenied("IMMUNE_RECOVERY_SCHEMA_INVALID")
        for field in required:
            if recovery.get(field) in (None, ""):
                raise AssuranceDenied("IMMUNE_RECOVERY_FIELD_MISSING:" + field)
        if recovery["operation"] != "IMMUNE_MIGRATION_RECOVERY":
            raise AssuranceDenied("IMMUNE_RECOVERY_OPERATION_INVALID")
        if recovery["requested_recovery_action"] not in IMMUNE_RECOVERY_ACTIONS:
            raise AssuranceDenied("IMMUNE_RECOVERY_ACTION_INVALID")
        if recovery["publication_state"] not in {"NOT_PUBLISHED", "PUBLISHED", "UNKNOWN"}:
            raise AssuranceDenied("IMMUNE_RECOVERY_PUBLICATION_STATE_INVALID")
        for field in (
            "migration_subject_sha256", "entry_authorization_fingerprint",
            "mcp_recovery_decision_sha256", "current_host_proof_sha256",
            "installation_plan_sha256", "installation_observation_sha256",
            "quarantine_scope_sha256",
        ):
            if not HEX64_RE.fullmatch(str(recovery[field])):
                raise AssuranceDenied("IMMUNE_RECOVERY_SHA256_INVALID:" + field)
        nonce = str(recovery["recovery_nonce"]);
        if not nonce.startswith("RECOVERY:") or len(nonce) < 25:
            raise AssuranceDenied("IMMUNE_RECOVERY_NONCE_NAMESPACE_INVALID")
        if recovery["requested_recovery_action"] == "ROLLBACK_EXACT_PREPUBLICATION":
            for field in ("rollback_completion_proof_sha256", "prestate_manifest_sha256", "rollback_plan_sha256"):
                if not HEX64_RE.fullmatch(str(recovery.get(field) or "")):
                    raise AssuranceDenied("IMMUNE_RECOVERY_ROLLBACK_FIELD_INVALID:" + field)
        if recovery["requested_recovery_action"] == "QUARANTINE" and not HEX64_RE.fullmatch(str(recovery.get("reason_sha256") or "")):
            raise AssuranceDenied("IMMUNE_RECOVERY_QUARANTINE_REASON_INVALID")

    @staticmethod
    def _recovery_subject(recovery: dict[str, Any]) -> dict[str, Any]:
        return {
            "subject_type": "MIGRATION_RECOVERY",
            "migration_id": str(recovery["migration_id"]),
            "migration_subject_sha256": str(recovery["migration_subject_sha256"]),
            "entry_authorization_fingerprint": str(recovery["entry_authorization_fingerprint"]),
            "requested_recovery_action": str(recovery["requested_recovery_action"]),
            "authority_epoch": int(recovery["authority_epoch"]),
            "mcp_recovery_decision_sha256": str(recovery["mcp_recovery_decision_sha256"]),
            "current_host_proof_sha256": str(recovery["current_host_proof_sha256"]),
            "installation_plan_sha256": str(recovery["installation_plan_sha256"]),
            "installation_observation_sha256": str(recovery["installation_observation_sha256"]),
            "publication_state": str(recovery["publication_state"]),
            "quarantine_scope_sha256": str(recovery["quarantine_scope_sha256"]),
            "producer_identity": str(recovery["producer_identity"]),
            "recovery_nonce": str(recovery["recovery_nonce"]),
        }

    def _evaluate_recovery_locked(
        self, state: dict[str, Any], recovery: dict[str, Any], *, expected_epoch: int
    ) -> dict[str, Any]:
        self._validate_recovery(recovery)
        if state["state"] != "IMMUNE_MIGRATING":
            raise AssuranceDenied("IMMUNE_RECOVERY_FROM_INVALID_STATE")
        if int(state["authority_epoch"]) != expected_epoch or int(recovery["authority_epoch"]) != expected_epoch:
            raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
        record = state.get("migration_recovery_record")
        if not isinstance(record, dict):
            raise AssuranceDenied("IMMUNE_RECOVERY_RECORD_MISSING")
        exact = (
            ("migration_id", state["migration_id"]),
            ("migration_subject_sha256", record["migration_subject_sha256"]),
            ("entry_authorization_fingerprint", record["entry_authorization_fingerprint"]),
            ("installation_plan_sha256", record["installation_plan_sha256"]),
            ("quarantine_scope_sha256", state["migration_quarantine_scope_sha256"]),
        )
        for field, value in exact:
            if str(recovery[field]) != str(value):
                raise AssuranceDenied("IMMUNE_RECOVERY_SUBJECT_MISMATCH:" + field)
        current_ledger = ledger_fingerprint(state["consumed"])
        if current_ledger != record["consumed_ledger_sha256"]:
            raise AssuranceDenied("IMMUNE_RECOVERY_LEDGER_DRIFT")
        nonce_hash = sha256_hex(str(recovery["recovery_nonce"]).encode("utf-8"))
        if nonce_hash == record["entry_nonce_sha256"]:
            raise AssuranceDenied("IMMUNE_RECOVERY_ENTRY_NONCE_REPLAY")
        if nonce_hash in {str(x.get("recovery_nonce_sha256") or "") for x in record["recovery_consumptions"]}:
            raise AssuranceDenied("IMMUNE_RECOVERY_NONCE_REPLAY")

        plan_path = Path(str(recovery["installation_plan_path"]))
        plan = self._load_installation_plan(plan_path, str(recovery["installation_plan_sha256"]))
        progress, prefix_count = self._classify_installation_progress(
            plan, Path(str(recovery["installation_observation_path"])),
            str(recovery["installation_observation_sha256"]),
        )
        action = str(recovery["requested_recovery_action"]); publication = str(recovery["publication_state"])
        if action != "QUARANTINE" and (publication != "NOT_PUBLISHED" or progress == "DRIFT"):
            raise AssuranceDenied("RECOVERY_QUARANTINE_REQUIRED")
        if action == "RESUME_EXACT" and progress not in {"PRESTATE_EXACT", "DETERMINISTIC_PREFIX", "FULLY_INSTALLED"}:
            raise AssuranceDenied("IMMUNE_RECOVERY_RESUME_PREFIX_INVALID")
        if action == "ROLLBACK_EXACT_PREPUBLICATION":
            if publication != "NOT_PUBLISHED" or progress != "PRESTATE_EXACT":
                raise AssuranceDenied("IMMUNE_RECOVERY_ROLLBACK_PRESTATE_NOT_PROVEN")
            if str(recovery["prestate_manifest_sha256"]) != str(state["migration_prestate_manifest_sha256"]):
                raise AssuranceDenied("IMMUNE_RECOVERY_PRESTATE_IDENTITY_MISMATCH")
            if str(recovery["rollback_plan_sha256"]) != str(state["migration_rollback_plan_sha256"]):
                raise AssuranceDenied("IMMUNE_RECOVERY_ROLLBACK_PLAN_MISMATCH")

        verifier_path = Path(str(state["external_anchor_verifier_path"]))
        if not verifier_path.is_file() or file_sha256(verifier_path) != str(state["external_anchor_verifier_sha256"]):
            raise AssuranceDenied("EXTERNAL_ANCHOR_VERIFIER_DRIFT")
        external_attestation = Path(str(recovery["external_anchor_recovery_attestation_path"]))
        if not external_attestation.is_file():
            raise AssuranceDenied("EXTERNAL_ANCHOR_RECOVERY_ATTESTATION_MISSING")
        verifier = _load_module("cerebro_external_anchor_recovery_verifier", verifier_path)
        if not hasattr(verifier, "verify_recovery"):
            raise AssuranceDenied("EXTERNAL_ANCHOR_RECOVERY_INTERFACE_ABSENT")
        try:
            external = verifier.verify_recovery(
                recovery, recovery_record=dict(record), attestation_path=str(external_attestation)
            )
        except Exception as exc:
            raise AssuranceDenied("EXTERNAL_ANCHOR_RECOVERY_VERIFICATION_FAILED:" + str(exc)) from exc
        if not isinstance(external, dict) or external.get("result") != "PASS":
            raise AssuranceDenied("EXTERNAL_ANCHOR_RECOVERY_NONPASS")
        for field, value in (("external_anchor_id", state["external_anchor_id"]),
                             ("external_anchor_fingerprint", state["external_anchor_fingerprint"]),
                             ("currentness", "CURRENT")):
            if external.get(field) != value:
                raise AssuranceDenied("EXTERNAL_ANCHOR_RECOVERY_MISMATCH:" + field)
        recovery_auth = str(external.get("recovery_authorization_fingerprint") or "")
        if not HEX64_RE.fullmatch(recovery_auth):
            raise AssuranceDenied("EXTERNAL_ANCHOR_RECOVERY_AUTHORIZATION_INVALID")

        key_path = Path(str(state["immune_attestor_key_path"])); attestor_path = Path(str(state["immune_attestor_path"]))
        if (not key_path.is_file() or sha256_hex(key_path.read_bytes()) != str(state["immune_attestor_key_fingerprint"])):
            raise AssuranceDenied("IMMUNE_ATTESTOR_KEY_DRIFT")
        if (not attestor_path.is_file() or file_sha256(attestor_path) != str(state["immune_attestor_sha256"])):
            raise AssuranceDenied("IMMUNE_ATTESTOR_IMPLEMENTATION_DRIFT")
        recovery_attestation_path = Path(str(recovery["recovery_attestation_path"]))
        if not recovery_attestation_path.is_file():
            raise AssuranceDenied("IMMUNE_RECOVERY_ATTESTATION_MISSING")
        module = _load_module("cerebro_immune_recovery_attestation_runtime", attestor_path)
        try:
            attestation = json.loads(recovery_attestation_path.read_text(encoding="utf-8"))
            attestation_result = module.verify_attestation(
                attestation, key=key_path.read_bytes(), implementation_path=attestor_path,
                expected_subject=self._recovery_subject(recovery),
            )
        except Exception as exc:
            raise AssuranceDenied("IMMUNE_RECOVERY_ATTESTATION_INVALID:" + str(exc)) from exc
        if attestation_result.get("result") != "PASS":
            raise AssuranceDenied("IMMUNE_RECOVERY_ATTESTATION_NOT_PASS")
        attestation_fingerprint = str(attestation_result.get("attestation_fingerprint") or "")
        if not HEX64_RE.fullmatch(attestation_fingerprint):
            raise AssuranceDenied("IMMUNE_RECOVERY_ATTESTATION_FINGERPRINT_INVALID")

        consumption_payload = {
            "migration_subject_sha256": record["migration_subject_sha256"],
            "entry_authorization_fingerprint": record["entry_authorization_fingerprint"],
            "requested_recovery_action": action,
            "authority_epoch": expected_epoch,
            "mcp_recovery_decision_sha256": recovery["mcp_recovery_decision_sha256"],
            "current_host_proof_sha256": recovery["current_host_proof_sha256"],
            "recovery_authorization_fingerprint": recovery_auth,
            "recovery_attestation_fingerprint": attestation_fingerprint,
            "installation_plan_sha256": recovery["installation_plan_sha256"],
            "installation_observation_sha256": recovery["installation_observation_sha256"],
            "publication_state": publication,
            "recovery_nonce_sha256": nonce_hash,
        }
        consumption_id = sha256_hex(canonical(consumption_payload))
        if consumption_id in {str(x.get("recovery_consumption_id") or "") for x in record["recovery_consumptions"]}:
            raise AssuranceDenied("IMMUNE_RECOVERY_CONSUMPTION_REPLAY")
        return {
            "action": action, "publication_state": publication, "installation_progress": progress,
            "installation_prefix_count": prefix_count, "recovery_nonce_sha256": nonce_hash,
            "recovery_authorization_fingerprint": recovery_auth,
            "recovery_attestation_fingerprint": attestation_fingerprint,
            "recovery_consumption_id": consumption_id,
            "consumption_payload": consumption_payload,
        }

    def check_immune_migration_recovery(
        self, recovery: dict[str, Any], *, expected_epoch: int
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            evaluated = self._evaluate_recovery_locked(state, recovery, expected_epoch=expected_epoch)
            return {
                "result": "PASS",
                "reason": "AUTHENTICATED_RECOVERY_CHECK_NO_EFFECT",
                "recovery_action": evaluated["action"],
                "recovery_consumption_id": evaluated["recovery_consumption_id"],
                "installation_progress": evaluated["installation_progress"],
                "installation_prefix_count": evaluated["installation_prefix_count"],
                "authority_epoch": expected_epoch,
                "kernel_state": state["state"],
                "state_effect": "NONE",
            }

    def recover_immune_migration(
        self, recovery: dict[str, Any], *, expected_epoch: int
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            evaluated = self._evaluate_recovery_locked(state, recovery, expected_epoch=expected_epoch)
            record = dict(state["migration_recovery_record"]); consumptions = list(record["recovery_consumptions"])
            after_epoch = expected_epoch + 1
            consumptions.append({
                "recovery_consumption_id": evaluated["recovery_consumption_id"],
                "recovery_nonce_sha256": evaluated["recovery_nonce_sha256"],
                "recovery_action": evaluated["action"],
                "authority_epoch_before": expected_epoch,
                "authority_epoch_after": after_epoch,
                "installation_progress": evaluated["installation_progress"],
                "installation_observation_sha256": str(recovery["installation_observation_sha256"]),
                "publication_state": evaluated["publication_state"],
                "recovery_authorization_fingerprint": evaluated["recovery_authorization_fingerprint"],
                "recovery_attestation_fingerprint": evaluated["recovery_attestation_fingerprint"],
            })
            record["recovery_consumptions"] = consumptions
            state["migration_recovery_record"] = record
            state["authority_epoch"] = after_epoch
            action = evaluated["action"]
            if action == "RESUME_EXACT":
                state["state"] = "IMMUNE_MIGRATING"
                state["migration_resume_authorization_sha256"] = evaluated["recovery_consumption_id"]
            elif action == "ROLLBACK_EXACT_PREPUBLICATION":
                state["state"] = str(state["migration_source_state"])
                state["migration_rollback_completion_proof_sha256"] = str(recovery["rollback_completion_proof_sha256"])
            else:
                state["state"] = "IMMUNE_QUARANTINED"
                state["quarantine_reason_sha256"] = str(recovery["reason_sha256"])
                state["quarantine_scope_sha256"] = str(recovery["quarantine_scope_sha256"])
            _atomic_json(self.state_path, state)
            ledger_after = ledger_fingerprint(state["consumed"])
            return {
                "schema": IMMUNE_RECEIPT_SCHEMA,
                "result": "RECOVERY",
                "reason": "AUTHENTICATED_POSTCRASH_RETURN_CONSUMED",
                "migration_id": str(recovery["migration_id"]),
                "recovery_action": action,
                "recovery_consumption_id": evaluated["recovery_consumption_id"],
                "migration_subject_sha256": str(recovery["migration_subject_sha256"]),
                "entry_authorization_fingerprint": str(recovery["entry_authorization_fingerprint"]),
                "recovery_authorization_fingerprint": evaluated["recovery_authorization_fingerprint"],
                "recovery_attestation_fingerprint": evaluated["recovery_attestation_fingerprint"],
                "mcp_recovery_decision_sha256": str(recovery["mcp_recovery_decision_sha256"]),
                "current_host_proof_sha256": str(recovery["current_host_proof_sha256"]),
                "installation_plan_sha256": str(recovery["installation_plan_sha256"]),
                "installation_observation_sha256": str(recovery["installation_observation_sha256"]),
                "installation_progress": evaluated["installation_progress"],
                "publication_state": str(recovery["publication_state"]),
                "authority_epoch_before": expected_epoch,
                "authority_epoch_after": after_epoch,
                "consumed_ledger_sha256_before": str(record["consumed_ledger_sha256"]),
                "consumed_ledger_sha256_after": ledger_after,
                "consumed_ledger_preserved": ledger_after == str(record["consumed_ledger_sha256"]),
                "kernel_state": state["state"],
                "quarantine_scope_sha256": str(recovery["quarantine_scope_sha256"]),
            }

    def begin_immune_migration(
        self,
        migration: dict[str, Any],
        *,
        expected_epoch: int,
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            self._validate_migration(migration)
            if state["state"] not in {
                "DOCTOR_ENFORCED",
                "FAILED_RECOVERY",
            }:
                raise AssuranceDenied(
                    "IMMUNE_MIGRATION_FROM_INVALID_STATE"
                )
            if state["state"] != migration["source_state"]:
                raise AssuranceDenied(
                    "IMMUNE_MIGRATION_SOURCE_STATE_MISMATCH"
                )
            if int(state["authority_epoch"]) != expected_epoch:
                raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
            if int(migration["authority_epoch"]) != expected_epoch:
                raise AssuranceDenied(
                    "IMMUNE_MIGRATION_EPOCH_MISMATCH"
                )
            if migration["consumed_ledger_sha256"] != (
                ledger_fingerprint(state["consumed"])
            ):
                raise AssuranceDenied(
                    "IMMUNE_MIGRATION_LEDGER_MISMATCH"
                )

            verifier_path = Path(
                str(migration["external_anchor_verifier_path"])
            )
            attestation_path = Path(
                str(migration["external_anchor_attestation_path"])
            )
            immune_attestor_path = Path(
                str(migration["immune_attestor_path"])
            )
            immune_key_path = Path(
                str(migration["immune_attestor_key_path"])
            )
            if not verifier_path.is_file():
                raise AssuranceDenied(
                    "EXTERNAL_ANCHOR_VERIFIER_MISSING"
                )
            if file_sha256(verifier_path) != str(
                migration["external_anchor_verifier_sha256"]
            ):
                raise AssuranceDenied(
                    "EXTERNAL_ANCHOR_VERIFIER_DRIFT"
                )
            if not attestation_path.is_file():
                raise AssuranceDenied(
                    "EXTERNAL_ANCHOR_ATTESTATION_MISSING"
                )
            if (
                not immune_attestor_path.is_file()
                or file_sha256(immune_attestor_path)
                != str(migration["immune_attestor_sha256"])
            ):
                raise AssuranceDenied(
                    "IMMUNE_ATTESTOR_BINDING_INVALID"
                )
            if (
                not immune_key_path.is_file()
                or len(immune_key_path.read_bytes()) < 32
                or sha256_hex(immune_key_path.read_bytes())
                != str(
                    migration[
                        "immune_attestor_key_fingerprint"
                    ]
                )
            ):
                raise AssuranceDenied(
                    "IMMUNE_ATTESTOR_KEY_BINDING_INVALID"
                )

            verifier = _load_module(
                "cerebro_external_anchor_verifier",
                verifier_path,
            )
            if not hasattr(verifier, "verify_migration"):
                raise AssuranceDenied(
                    "EXTERNAL_ANCHOR_VERIFIER_INTERFACE_ABSENT"
                )
            try:
                result = verifier.verify_migration(
                    migration,
                    attestation_path=str(attestation_path),
                )
            except Exception as exc:
                raise AssuranceDenied(
                    "EXTERNAL_ANCHOR_VERIFICATION_FAILED:"
                    + str(exc)
                ) from exc
            if not isinstance(result, dict) or result.get(
                "result"
            ) != "PASS":
                raise AssuranceDenied(
                    "EXTERNAL_ANCHOR_VERIFICATION_NONPASS"
                )
            checks = (
                (
                    "external_anchor_id",
                    migration["external_anchor_id"],
                ),
                (
                    "external_anchor_fingerprint",
                    migration["external_anchor_fingerprint"],
                ),
                ("currentness", "CURRENT"),
                ("preexisting", True),
                ("outside_targetset", True),
            )
            for field, expected in checks:
                if result.get(field) != expected:
                    raise AssuranceDenied(
                        "EXTERNAL_ANCHOR_VERIFICATION_MISMATCH:"
                        + field
                    )
            receipt_fingerprint = str(
                result.get("receipt_sha256") or ""
            )
            if not HEX64_RE.fullmatch(receipt_fingerprint):
                raise AssuranceDenied("EXTERNAL_ANCHOR_RECEIPT_INVALID")
            entry_authorization_fingerprint = str(
                result.get("anchor_authorization_fingerprint") or ""
            )
            if not HEX64_RE.fullmatch(entry_authorization_fingerprint):
                raise AssuranceDenied("EXTERNAL_ANCHOR_AUTHORIZATION_FINGERPRINT_INVALID")
            prestate_fingerprint = sha256_hex(canonical(state))
            migration_subject_sha256 = sha256_hex(canonical(self._migration_subject(migration)))
            recovery_record = {
                "schema": IMMUNE_RECOVERY_RECORD_SCHEMA,
                "migration_id": str(migration["migration_id"]),
                "migration_subject_sha256": migration_subject_sha256,
                "entry_authorization_fingerprint": entry_authorization_fingerprint,
                "prestate_fingerprint": prestate_fingerprint,
                "post_entry_authority_epoch": expected_epoch + 1,
                "consumed_ledger_sha256": str(migration["consumed_ledger_sha256"]),
                "installation_plan_sha256": str(migration["installation_plan_sha256"]),
                "entry_nonce_sha256": sha256_hex(str(migration["nonce"]).encode("utf-8")),
                "recovery_consumptions": [],
            }

            state.update(
                {
                    "state": "IMMUNE_MIGRATING",
                    "authority_epoch": expected_epoch + 1,
                    "migration_id": str(
                        migration["migration_id"]
                    ),
                    "migration_source_state": str(
                        migration["source_state"]
                    ),
                    "migration_source_head": str(
                        migration["source_head"]
                    ),
                    "migration_source_tree": str(
                        migration["source_tree"]
                    ),
                    "migration_consumed_ledger_sha256": str(
                        migration["consumed_ledger_sha256"]
                    ),
                    "migration_receipt_sha256": receipt_fingerprint,
                    "migration_campaign_id": str(migration["campaign_id"]),
                    "migration_package_sha256": str(migration["package_sha256"]),
                    "migration_touched_paths_sha256": str(migration["touched_paths_sha256"]),
                    "migration_mcp_decision_fingerprint": str(migration["mcp_decision_fingerprint"]),
                    "migration_current_host_proof_sha256": str(migration["current_host_proof_sha256"]),
                    "migration_quarantine_scope_sha256": str(migration["quarantine_scope_sha256"]),
                    "migration_risk_profile": str(migration["risk_profile"]),
                    "migration_prestate_manifest_sha256": str(migration["prestate_manifest_sha256"]),
                    "migration_rollback_plan_sha256": str(migration["rollback_plan_sha256"]),
                    "migration_installation_plan_path": str(Path(str(migration["installation_plan_path"])).resolve()),
                    "migration_installation_plan_sha256": str(migration["installation_plan_sha256"]),
                    "migration_producer_identity": str(migration["producer_identity"]),
                    "migration_recovery_record": recovery_record,
                    "external_anchor_id": str(
                        migration["external_anchor_id"]
                    ),
                    "external_anchor_fingerprint": str(
                        migration[
                            "external_anchor_fingerprint"
                        ]
                    ),
                    "external_anchor_verifier_path": str(
                        verifier_path.resolve()
                    ),
                    "external_anchor_verifier_sha256": str(
                        migration[
                            "external_anchor_verifier_sha256"
                        ]
                    ),
                    "immune_attestor_path": str(
                        immune_attestor_path.resolve()
                    ),
                    "immune_attestor_sha256": str(
                        migration["immune_attestor_sha256"]
                    ),
                    "immune_attestor_key_path": str(
                        immune_key_path.resolve()
                    ),
                    "immune_attestor_key_fingerprint": str(
                        migration[
                            "immune_attestor_key_fingerprint"
                        ]
                    ),
                }
            )
            _atomic_json(self.state_path, state)
            return state

    def finalize_immune_enforced(
        self,
        *,
        expected_epoch: int,
        migration_receipt_sha256: str,
        immune_activation_proof_sha256: str,
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            if state["state"] != "IMMUNE_MIGRATING":
                raise AssuranceDenied(
                    "IMMUNE_FINALIZE_FROM_INVALID_STATE"
                )
            if int(state["authority_epoch"]) != expected_epoch:
                raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
            if (
                not HEX64_RE.fullmatch(
                    migration_receipt_sha256
                )
                or migration_receipt_sha256
                != state["migration_receipt_sha256"]
            ):
                raise AssuranceDenied(
                    "IMMUNE_MIGRATION_RECEIPT_MISMATCH"
                )
            if not HEX64_RE.fullmatch(
                immune_activation_proof_sha256
            ):
                raise AssuranceDenied(
                    "IMMUNE_ACTIVATION_PROOF_INVALID"
                )
            if ledger_fingerprint(state["consumed"]) != str(
                state["migration_consumed_ledger_sha256"]
            ):
                raise AssuranceDenied(
                    "IMMUNE_MIGRATION_LEDGER_DRIFT"
                )
            for path_field, hash_field in (
                (
                    "external_anchor_verifier_path",
                    "external_anchor_verifier_sha256",
                ),
                (
                    "immune_attestor_path",
                    "immune_attestor_sha256",
                ),
            ):
                path = Path(str(state[path_field]))
                if (
                    not path.is_file()
                    or file_sha256(path)
                    != str(state[hash_field])
                ):
                    raise AssuranceDenied(
                        "IMMUNE_BOUND_IMPLEMENTATION_DRIFT:"
                        + path_field
                    )
            key_path = Path(
                str(state["immune_attestor_key_path"])
            )
            if (
                not key_path.is_file()
                or sha256_hex(key_path.read_bytes())
                != str(
                    state["immune_attestor_key_fingerprint"]
                )
            ):
                raise AssuranceDenied(
                    "IMMUNE_ATTESTOR_KEY_DRIFT"
                )
            state["state"] = "IMMUNE_ENFORCED"
            state["authority_epoch"] = expected_epoch + 1
            state[
                "immune_activation_proof_sha256"
            ] = immune_activation_proof_sha256
            _atomic_json(self.state_path, state)
            return state

    def transition_immune_quarantined(
        self,
        *,
        expected_epoch: int,
        reason_sha256: str,
        quarantine_scope_sha256: str,
    ) -> dict[str, Any]:
        with _exclusive_lock(self.lock_path):
            state = self._read()
            if state["state"] not in {
                "IMMUNE_MIGRATING",
                "IMMUNE_ENFORCED",
            }:
                raise AssuranceDenied(
                    "IMMUNE_QUARANTINE_FROM_INVALID_STATE"
                )
            if int(state["authority_epoch"]) != expected_epoch:
                raise AssuranceDenied("STALE_AUTHORITY_EPOCH")
            if not HEX64_RE.fullmatch(reason_sha256):
                raise AssuranceDenied(
                    "IMMUNE_QUARANTINE_REASON_INVALID"
                )
            if not HEX64_RE.fullmatch(
                quarantine_scope_sha256
            ):
                raise AssuranceDenied(
                    "IMMUNE_QUARANTINE_SCOPE_INVALID"
                )
            state["state"] = "IMMUNE_QUARANTINED"
            state["authority_epoch"] = expected_epoch + 1
            state["quarantine_reason_sha256"] = reason_sha256
            state[
                "quarantine_scope_sha256"
            ] = quarantine_scope_sha256
            _atomic_json(self.state_path, state)
            return state


def deny_receipt(reason: str, *, immune: bool = False) -> dict[str, Any]:
    return {
        "schema": (
            IMMUNE_RECEIPT_SCHEMA if immune else RECEIPT_SCHEMA
        ),
        "result": "DENY",
        "reason": reason,
    }


def _load(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssuranceDenied("JSON_OBJECT_REQUIRED")
    return value


def intent_from_manifest(
    manifest_path: str, source_head: str
) -> MaterialIntent:
    path = Path(manifest_path)
    raw = path.read_bytes()
    manifest = json.loads(raw.decode("utf-8"))
    binding = manifest.get("assurance_kernel")
    if not isinstance(binding, dict):
        raise AssuranceDenied(
            "MANIFEST_ASSURANCE_BINDING_MISSING"
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise AssuranceDenied("MANIFEST_FILES_MISSING")
    paths: list[str] = []
    for entry in files:
        if (
            not isinstance(entry, dict)
            or not str(entry.get("path") or "").strip()
        ):
            raise AssuranceDenied(
                "MANIFEST_FILE_PATH_INVALID"
            )
        paths.append(str(entry["path"]))
    try:
        epoch = int(binding["authority_epoch"])
    except Exception as exc:
        raise AssuranceDenied(
            "MANIFEST_AUTHORITY_EPOCH_INVALID"
        ) from exc
    return MaterialIntent(
        source_head=source_head,
        package_sha256=sha256_hex(raw),
        touched_paths_sha256=touched_paths_fingerprint(paths),
        package_class=str(binding.get("package_class") or ""),
        campaign_id=str(binding.get("campaign_id") or ""),
        authority_epoch=epoch,
    )


def _intent(args: argparse.Namespace) -> MaterialIntent:
    return MaterialIntent(
        args.source_head,
        args.package_sha256,
        args.touched_paths_sha256,
        args.package_class,
        args.campaign_id,
        args.authority_epoch,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    initialize = sub.add_parser("initialize-bootstrap")
    initialize.add_argument("--anchor-proof", required=True)
    initialize.add_argument(
        "--authority-epoch", type=int, default=1
    )

    for name in ("check-permit", "consume-permit"):
        command = sub.add_parser(name)
        command.add_argument("--permit", required=True)
        command.add_argument("--source-head", required=True)
        command.add_argument("--package-sha256", required=True)
        command.add_argument(
            "--touched-paths-sha256", required=True
        )
        command.add_argument("--package-class", required=True)
        command.add_argument("--campaign-id", required=True)
        command.add_argument(
            "--authority-epoch", type=int, required=True
        )

    for name in (
        "check-manifest-permit",
        "consume-manifest-permit",
    ):
        command = sub.add_parser(name)
        command.add_argument("--permit", required=True)
        command.add_argument("--manifest", required=True)
        command.add_argument("--source-head", required=True)

    doctor = sub.add_parser("doctor-enforced")
    doctor.add_argument(
        "--active-path-proof-sha256", required=True
    )
    doctor.add_argument(
        "--expected-epoch", type=int, required=True
    )
    doctor.add_argument("--trust-key", required=True)
    doctor.add_argument("--doctor-verifier", required=True)

    failed = sub.add_parser("failed-recovery")
    failed.add_argument("--reason-sha256", required=True)
    failed.add_argument(
        "--expected-epoch", type=int, required=True
    )

    begin = sub.add_parser("begin-immune-migration")
    begin.add_argument("--migration", required=True)
    begin.add_argument(
        "--expected-epoch", type=int, required=True
    )

    first_activation = sub.add_parser(
        "one-time-human-admin-first-activation"
    )
    first_activation.add_argument("--authorization", required=True)
    first_activation.add_argument("--manifest", required=True)
    first_activation.add_argument("--working-source", required=True)
    first_activation.add_argument("--immune-attestor", required=True)
    first_activation.add_argument(
        "--expected-epoch", type=int, required=True
    )

    for name in ("check-immune-migration-recovery", "recover-immune-migration"):
        recovery = sub.add_parser(name)
        recovery.add_argument("--recovery", required=True)
        recovery.add_argument("--expected-epoch", type=int, required=True)

    finalize = sub.add_parser("finalize-immune-enforced")
    finalize.add_argument(
        "--expected-epoch", type=int, required=True
    )
    finalize.add_argument(
        "--migration-receipt-sha256", required=True
    )
    finalize.add_argument(
        "--immune-activation-proof-sha256", required=True
    )

    quarantine = sub.add_parser("immune-quarantine")
    quarantine.add_argument(
        "--expected-epoch", type=int, required=True
    )
    quarantine.add_argument("--reason-sha256", required=True)
    quarantine.add_argument(
        "--quarantine-scope-sha256", required=True
    )

    args = parser.parse_args()
    kernel = AssuranceKernel(Path(args.state))
    try:
        if args.cmd == "initialize-bootstrap":
            output = kernel.initialize_bootstrap(
                external_anchor_proof=args.anchor_proof,
                authority_epoch=args.authority_epoch,
            )
        elif args.cmd == "check-permit":
            output = kernel.check(
                _load(args.permit), _intent(args)
            )
        elif args.cmd == "consume-permit":
            output = kernel.consume(
                _load(args.permit), _intent(args)
            )
        elif args.cmd == "check-manifest-permit":
            output = kernel.check(
                _load(args.permit),
                intent_from_manifest(
                    args.manifest, args.source_head
                ),
            )
        elif args.cmd == "consume-manifest-permit":
            output = kernel.consume(
                _load(args.permit),
                intent_from_manifest(
                    args.manifest, args.source_head
                ),
            )
        elif args.cmd == "doctor-enforced":
            output = kernel.transition_doctor_enforced(
                active_path_proof_sha256=(
                    args.active_path_proof_sha256
                ),
                expected_epoch=args.expected_epoch,
                trust_key_path=args.trust_key,
                doctor_verifier_path=args.doctor_verifier,
            )
        elif args.cmd == "failed-recovery":
            output = kernel.transition_failed_recovery(
                reason_sha256=args.reason_sha256,
                expected_epoch=args.expected_epoch,
            )
        elif args.cmd == "begin-immune-migration":
            output = kernel.begin_immune_migration(
                _load(args.migration),
                expected_epoch=args.expected_epoch,
            )
        elif args.cmd == "one-time-human-admin-first-activation":
            output = kernel.transition_one_time_human_admin_first_activation(
                _load(args.authorization),
                expected_epoch=args.expected_epoch,
                manifest_path=args.manifest,
                working_source_path=args.working_source,
                immune_attestor_path=args.immune_attestor,
            )
        elif args.cmd == "check-immune-migration-recovery":
            output = kernel.check_immune_migration_recovery(
                _load(args.recovery), expected_epoch=args.expected_epoch
            )
        elif args.cmd == "recover-immune-migration":
            output = kernel.recover_immune_migration(
                _load(args.recovery), expected_epoch=args.expected_epoch
            )
        elif args.cmd == "finalize-immune-enforced":
            output = kernel.finalize_immune_enforced(
                expected_epoch=args.expected_epoch,
                migration_receipt_sha256=(
                    args.migration_receipt_sha256
                ),
                immune_activation_proof_sha256=(
                    args.immune_activation_proof_sha256
                ),
            )
        else:
            output = kernel.transition_immune_quarantined(
                expected_epoch=args.expected_epoch,
                reason_sha256=args.reason_sha256,
                quarantine_scope_sha256=(
                    args.quarantine_scope_sha256
                ),
            )
        print(json.dumps(output, sort_keys=True))
        return 0
    except (AssuranceDenied, OSError, ValueError) as exc:
        current_state = None
        try:
            current_state = kernel._read().get("state")
        except Exception:
            pass
        print(
            json.dumps(
                deny_receipt(
                    str(exc),
                    immune=current_state
                    in {
                        "IMMUNE_MIGRATING",
                        "IMMUNE_ENFORCED",
                        "IMMUNE_QUARANTINED",
                    },
                ),
                sort_keys=True,
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
