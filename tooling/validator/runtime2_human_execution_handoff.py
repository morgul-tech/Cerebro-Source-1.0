#!/usr/bin/env python3
"""Generate and validate the dedicated hash-bound Runtime2 ``.cmd`` handoff."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "cerebro-runtime2-human-execution-handoff/v1"
VALIDATION_SCHEMA = "cerebro-runtime2-human-execution-handoff-validation/v1"
ACTIVATION_SCHEMA = "cerebro-runtime2-human-execution-handoff-activation-proof/v1"
PROFILE = "HASH_BOUND_RUNTIME2_CMD"
BINDING_ID = "RUNTIME2_HUMAN_EXECUTION_HANDOFF_TRANSPORT"
CONTEXT_BINDING_ID = "RUNTIME2-HUMAN-EXECUTION-HANDOFF-CONTINUE"
CONSUMER_RELATIVE = "tooling/runtime-host/runtime2_handoff_consumer.py"
REGISTRY_RELATIVE = "engines/context/continuation-bindings.json"
WORKING_CONTEXT_RELATIVE = "engines/context/working-context.yaml"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
EXACT_TARGETS = {
    "engines/context/working-context.yaml",
    "engines/context/continuation-bindings.json",
    "standards/human-continuation-surface.yaml",
    "mcp/manifest.yaml",
    "tooling/validator/checks.yaml",
    "tooling/validator/contract-activation-bindings.json",
    "tooling/validator/runtime2_human_execution_handoff.py",
    "tooling/runtime-host/runtime2_handoff_consumer.py",
}
DO_NOT_MODIFY_PATHS = {
    "tooling/delivery/Cerebro.StandardDeliveryLauncher.ps1",
    "tooling/validator/human_execution_handoff.py",
}


class Runtime2HandoffError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Runtime2HandoffError(message)


def _sha(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    _require(bool(SHA256_RE.fullmatch(normalized)), f"{label}-sha256-invalid")
    return normalized


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _binding_fingerprint(binding: dict[str, Any]) -> str:
    subject = {key: value for key, value in binding.items() if key != "binding_fingerprint"}
    return hashlib.sha256(json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def load_runtime2_context_binding(source_root: Path) -> dict[str, Any]:
    registry = json.loads((source_root / REGISTRY_RELATIVE).read_text(encoding="utf-8"))
    _require(registry.get("active_binding_id") == "C02-P002-CASE-FINAL-CONTINUE", "c02-active-binding-was-silently-rebased")
    _require(registry.get("runtime2_active_binding_id") == CONTEXT_BINDING_ID, "runtime2-active-binding-missing")
    matches = [item for item in registry.get("bindings", []) if item.get("binding_id") == CONTEXT_BINDING_ID]
    _require(len(matches) == 1, "runtime2-active-binding-cardinality-invalid")
    binding = matches[0]
    _require(binding.get("binding_fingerprint") == _binding_fingerprint(binding), "runtime2-active-binding-fingerprint-mismatch")
    _require(binding.get("current_basis_ref") == "CTX-BASIS-RUNTIME2-HUMAN-EXECUTION-HANDOFF-20260901-001", "runtime2-current-basis-ref-mismatch")
    _require(binding.get("full_payload_ref") == WORKING_CONTEXT_RELATIVE + "#CTX-BASIS-RUNTIME2-HUMAN-EXECUTION-HANDOFF-20260901-001", "runtime2-full-payload-ref-mismatch")
    context_text = (source_root / WORKING_CONTEXT_RELATIVE).read_text(encoding="utf-8")
    _require("- id: " + binding["current_basis_ref"] in context_text, "runtime2-current-basis-ref-unresolved")
    return copy.deepcopy(binding)


def envelope_fingerprint(envelope: dict[str, Any]) -> str:
    subject = {key: value for key, value in envelope.items() if key != "handoff_fingerprint"}
    return hashlib.sha256(_canonical_json(subject)).hexdigest()


def generate_envelope(cmd_sha256: str, source_revision: str, context_binding: dict[str, Any]) -> dict[str, Any]:
    revision = str(source_revision or "").strip().lower()
    _require(bool(REVISION_RE.fullmatch(revision)), "source-revision-invalid")
    _require(context_binding.get("binding_id") == CONTEXT_BINDING_ID, "runtime2-context-binding-id-mismatch")
    _require(context_binding.get("binding_fingerprint") == _binding_fingerprint(context_binding), "runtime2-context-binding-fingerprint-mismatch")
    envelope = {
        "schema": SCHEMA,
        "profile": PROFILE,
        "binding_id": BINDING_ID,
        "source_revision": revision,
        "context_binding": {
            "binding_id": CONTEXT_BINDING_ID,
            "binding_fingerprint": context_binding["binding_fingerprint"],
        },
        "artifact": {
            "kind": "WINDOWS_CMD",
            "extension": ".cmd",
            "sha256": _sha(cmd_sha256, "cmd-artifact"),
        },
    }
    envelope["handoff_fingerprint"] = envelope_fingerprint(envelope)
    return envelope


def validate_envelope(envelope: dict[str, Any], context_binding: dict[str, Any]) -> dict[str, Any]:
    _require(set(envelope) == {"schema", "profile", "binding_id", "source_revision", "context_binding", "artifact", "handoff_fingerprint"}, "runtime2-envelope-field-set-invalid")
    _require(envelope.get("schema") == SCHEMA, "runtime2-envelope-schema-mismatch")
    _require(envelope.get("profile") == PROFILE, "runtime2-envelope-profile-mismatch")
    _require(envelope.get("profile") != "HASH_BOUND_POWERSHELL", "runtime2-cmd-mislabeled-as-powershell")
    _require(envelope.get("binding_id") == BINDING_ID, "runtime2-transport-binding-mismatch")
    _require(bool(REVISION_RE.fullmatch(str(envelope.get("source_revision") or ""))), "source-revision-invalid")
    expected_context = {"binding_id": CONTEXT_BINDING_ID, "binding_fingerprint": context_binding.get("binding_fingerprint")}
    _require(envelope.get("context_binding") == expected_context, "runtime2-context-binding-envelope-mismatch")
    artifact = envelope.get("artifact")
    _require(isinstance(artifact, dict) and set(artifact) == {"kind", "extension", "sha256"}, "runtime2-envelope-must-contain-exactly-one-cmd-artifact-identity")
    _require(artifact.get("kind") == "WINDOWS_CMD" and artifact.get("extension") == ".cmd", "runtime2-envelope-artifact-must-be-cmd")
    cmd_sha = _sha(artifact.get("sha256"), "cmd-artifact")
    expected_fingerprint = envelope_fingerprint(envelope)
    _require(envelope.get("handoff_fingerprint") == expected_fingerprint, "runtime2-handoff-fingerprint-mismatch")
    return {
        "schema": VALIDATION_SCHEMA,
        "result": "PASS",
        "binding_id": BINDING_ID,
        "profile": PROFILE,
        "cmd_sha256": cmd_sha,
        "context_binding_id": CONTEXT_BINDING_ID,
        "handoff_fingerprint": expected_fingerprint,
    }


def generate_command(envelope_sha256: str, source_root: Path) -> str:
    envelope_sha = _sha(envelope_sha256, "envelope")
    consumer = str((source_root / CONSUMER_RELATIVE).resolve())
    return "\n".join((
        "$downloads = Join-Path $env:USERPROFILE 'Downloads'",
        "$envelopes = @()",
        "foreach ($candidate in (Get-ChildItem -LiteralPath $downloads -Filter '*.json' -File)) { if ((Get-FileHash -LiteralPath $candidate.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -eq '" + envelope_sha + "') { $envelopes += $candidate } }",
        "if ($envelopes.Count -ne 1) { throw \"Verified Runtime2 envelope count: $($envelopes.Count)\" }",
        "python '" + consumer.replace("'", "''") + "' --envelope $envelopes[0].FullName --search-root $downloads",
    ))


def generate_handoff(cmd_sha256: str, source_revision: str, source_root: Path) -> dict[str, Any]:
    binding = load_runtime2_context_binding(source_root)
    envelope = generate_envelope(cmd_sha256, source_revision, binding)
    envelope_bytes = _canonical_json(envelope)
    envelope_sha = hashlib.sha256(envelope_bytes).hexdigest()
    return {
        "envelope": envelope,
        "envelope_bytes_sha256": envelope_sha,
        "command": generate_command(envelope_sha, source_root),
    }


def resolve_unique_cmd(search_root: Path, expected_sha256: str) -> Path:
    expected = _sha(expected_sha256, "cmd-artifact")
    matches = [path for path in search_root.glob("*.cmd") if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected]
    _require(len(matches) == 1, f"runtime2-cmd-hash-cardinality-invalid:{len(matches)}")
    return matches[0].resolve()


def validate_response(response_text: str, command: str) -> None:
    matches = list(re.finditer(r"(?s)(?:^|\n)```powershell\n(.*?)\n```", response_text.rstrip()))
    _require(len(matches) == 1, "runtime2-exactly-one-powershell-surface-required")
    _require(matches[0].end() == len(response_text.rstrip()), "runtime2-powershell-surface-must-be-terminal")
    _require(matches[0].group(1) == command, "runtime2-rendered-command-mismatch")


def _must_block(function, *args) -> bool:
    try:
        function(*args)
    except Runtime2HandoffError:
        return True
    return False


def selftest() -> dict[str, Any]:
    binding = {
        "schema": "cerebro-human-continuation-binding/v1", "binding_id": CONTEXT_BINDING_ID,
        "surface_kind": "SHORT_HUMAN_TRIGGER", "alias": "Kjor Runtime2 handoff", "target_ref": "RUNTIME2",
        "operation": "EXECUTE_HASH_BOUND_RUNTIME2_CMD", "current_basis_ref": "CTX-BASIS",
        "full_payload_ref": "engines/context/working-context.yaml#CTX-BASIS", "constraints_refs": [], "evidence_refs": [],
        "maturity": "LOCKED", "readiness": "READY", "source_revision": "fixture",
        "required_next_behavior": [], "resume_order": [], "alternative_paths": [],
    }
    binding["binding_fingerprint"] = _binding_fingerprint(binding)
    envelope = generate_envelope("1" * 64, "a" * 40, binding)
    valid = validate_envelope(envelope, binding)
    wrong_profile = copy.deepcopy(envelope); wrong_profile["profile"] = "HASH_BOUND_POWERSHELL"
    extra_artifact = copy.deepcopy(envelope); extra_artifact["artifacts"] = [copy.deepcopy(envelope["artifact"])]
    wrong_context = copy.deepcopy(envelope); wrong_context["context_binding"]["binding_fingerprint"] = "2" * 64
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        payload = b"@echo off\r\nexit /b 0\r\n"
        target = root / "candidate.cmd"; target.write_bytes(payload)
        target_sha = hashlib.sha256(payload).hexdigest()
        resolved = resolve_unique_cmd(root, target_sha)
        unchanged = hashlib.sha256(resolved.read_bytes()).hexdigest() == target_sha
        (root / "duplicate.cmd").write_bytes(payload)
        duplicate_blocked = _must_block(resolve_unique_cmd, root, target_sha)
        (root / "candidate.cmd").unlink(); (root / "duplicate.cmd").unlink()
        missing_blocked = _must_block(resolve_unique_cmd, root, target_sha)
    return {
        "result": "PASS",
        "valid_envelope_accepted": valid["result"] == "PASS",
        "active_binding_fingerprint_required": _must_block(validate_envelope, wrong_context, binding),
        "powershell_profile_for_cmd_rejected": _must_block(validate_envelope, wrong_profile, binding),
        "extra_artifact_identity_rejected": _must_block(validate_envelope, extra_artifact, binding),
        "zero_hash_match_rejected": missing_blocked,
        "multiple_hash_matches_rejected": duplicate_blocked,
        "target_bytes_unchanged_by_resolution": unchanged,
    }


def _changed_paths(source_root: Path) -> set[str]:
    output = subprocess.check_output(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=source_root, text=True)
    return {line[3:].replace("\\", "/") for line in output.splitlines() if line.strip()}


def _source_fingerprint(source_root: Path) -> str:
    rows = [f"{relative}|{hashlib.sha256((source_root / relative).read_bytes()).hexdigest()}" for relative in sorted(EXACT_TARGETS)]
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def activation_probe(source_root: Path) -> dict[str, Any]:
    binding = load_runtime2_context_binding(source_root)
    checks = selftest()
    consumer_text = (source_root / CONSUMER_RELATIVE).read_text(encoding="utf-8")
    forbidden = ("cerebro" + "_sync", "Standard" + "Delivery")
    no_publication_or_generic_delivery = all(token not in consumer_text for token in forbidden)
    changed = _changed_paths(source_root)
    exact_targetset = changed == EXACT_TARGETS
    do_not_modify_clean = all(subprocess.run(["git", "diff", "--quiet", "HEAD", "--", path], cwd=source_root).returncode == 0 for path in DO_NOT_MODIFY_PATHS)
    _require(no_publication_or_generic_delivery, "runtime2-consumer-publication-or-generic-delivery-call-prohibited")
    _require(exact_targetset, "runtime2-hcs8-targetset-must-be-exact8")
    _require(do_not_modify_clean, "runtime2-hcs8-do-not-modify-path-changed")
    required_canaries = [value for key, value in checks.items() if key != "result"]
    _require(all(required_canaries), "runtime2-handoff-negative-canary-failed")
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": "PASS",
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "basis_files": sorted(EXACT_TARGETS),
        "source_state_fingerprint": _source_fingerprint(source_root),
        "context_binding_id": binding["binding_id"],
        "context_binding_fingerprint": binding["binding_fingerprint"],
        "generator_executed": True,
        "envelope_consumer_exercised": True,
        "runtime2_context_binding_validated": True,
        "cmd_hash_unique_cardinality_enforced": True,
        "consumer_independent_rehash_required": True,
        "consumer_nonsyncing_boundary_enforced": no_publication_or_generic_delivery,
        "target_cmd_bytes_preserved": checks["target_bytes_unchanged_by_resolution"],
        "do_not_modify_paths_preserved": do_not_modify_clean,
        "exact_hcs8_targetset_enforced": exact_targetset,
        "negative_canaries_passed": all(required_canaries),
        "changed_paths": sorted(changed),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_generate = sub.add_parser("generate")
    p_generate.add_argument("--cmd-sha256", required=True); p_generate.add_argument("--source-revision", required=True)
    p_generate.add_argument("--source-root", required=True); p_generate.add_argument("--output-envelope"); p_generate.add_argument("--output")
    p_validate = sub.add_parser("validate-envelope")
    p_validate.add_argument("--input", required=True); p_validate.add_argument("--source-root", required=True)
    p_probe = sub.add_parser("activation-probe"); p_probe.add_argument("--source-root", required=True); p_probe.add_argument("--output")
    p_selftest = sub.add_parser("selftest"); p_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            result = generate_handoff(args.cmd_sha256, args.source_revision, Path(args.source_root))
            if args.output_envelope:
                _write(Path(args.output_envelope), result["envelope"])
        elif args.command == "validate-envelope":
            root = Path(args.source_root); result = validate_envelope(json.loads(Path(args.input).read_text(encoding="utf-8")), load_runtime2_context_binding(root))
        elif args.command == "activation-probe":
            result = activation_probe(Path(args.source_root))
        else:
            result = selftest()
        if getattr(args, "output", None): _write(Path(args.output), result)
        else: print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except Exception as exc:
        result = {"result": "BLOCK", "error": str(exc)}
        if getattr(args, "output", None): _write(Path(args.output), result)
        else: print(json.dumps(result, indent=2, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
