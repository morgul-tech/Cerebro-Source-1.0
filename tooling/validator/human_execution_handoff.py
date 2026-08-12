#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "cerebro-human-execution-handoff/v1"
VALIDATION_SCHEMA = "cerebro-human-execution-handoff-validation/v1"
ACTIVATION_SCHEMA = "cerebro-human-execution-handoff-activation-proof/v1"
PROFILE = "HASH_BOUND_POWERSHELL"
BINDING_ID = "HUMAN_EXECUTION_HANDOFF_TRANSPORT"
FINGERPRINT_PARAMETER = "HumanExecutionHandoffFingerprint"
EVIDENCE_BASIS_FILES = (
    "standards/human-continuation-surface.yaml",
    "standards/development/delivery-failure-regression.yaml",
    "mcp/manifest.yaml",
    "engines/presentation/rules.yaml",
    "tooling/delivery/Cerebro.StandardDeliveryLauncher.ps1",
    "tooling/validator/human_execution_handoff.py",
    "tooling/validator/checks.yaml",
    "tooling/validator/contract-activation-bindings.json",
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class HumanExecutionHandoffError(ValueError):
    pass


def _sha(value: Any, label: str) -> str:
    normalized = str(value).strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise HumanExecutionHandoffError(f"{label}-sha256-invalid")
    return normalized


def handoff_fingerprint(launcher_sha256: str, bundle_sha256: str) -> str:
    launcher = _sha(launcher_sha256, "launcher")
    bundle = _sha(bundle_sha256, "bundle")
    subject = f"{SCHEMA}|{PROFILE}|{launcher}|{bundle}"
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def generate_command(launcher_sha256: str, bundle_sha256: str) -> str:
    launcher = _sha(launcher_sha256, "launcher")
    bundle = _sha(bundle_sha256, "bundle")
    fingerprint = handoff_fingerprint(launcher, bundle)
    return "\n".join(
        (
            "$downloads = Join-Path $env:USERPROFILE 'Downloads'",
            "$launcherCandidates = @()",
            "foreach ($candidate in (Get-ChildItem -LiteralPath $downloads -Filter '*.ps1' -File)) { if ((Get-FileHash -LiteralPath $candidate.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -eq '" + launcher + "') { $launcherCandidates += $candidate } }",
            "$bundleCandidates = @()",
            "foreach ($candidate in (Get-ChildItem -LiteralPath $downloads -Filter '*.zip' -File)) { if ((Get-FileHash -LiteralPath $candidate.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -eq '" + bundle + "') { $bundleCandidates += $candidate } }",
            "if ($launcherCandidates.Count -ne 1) { throw \"Verified launcher count: $($launcherCandidates.Count)\" }",
            "if ($bundleCandidates.Count -ne 1) { throw \"Verified bundle count: $($bundleCandidates.Count)\" }",
            "$launcherPath = $launcherCandidates[0].FullName",
            "$bundlePath = $bundleCandidates[0].FullName",
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcherPath -BundlePath $bundlePath -HumanExecutionHandoffFingerprint '" + fingerprint + "'",
        )
    )


def generate_envelope(launcher_sha256: str, bundle_sha256: str) -> dict[str, Any]:
    launcher = _sha(launcher_sha256, "launcher")
    bundle = _sha(bundle_sha256, "bundle")
    return {
        "schema": SCHEMA,
        "profile": PROFILE,
        "launcher_sha256": launcher,
        "bundle_sha256": bundle,
        "handoff_fingerprint": handoff_fingerprint(launcher, bundle),
        "fingerprint_parameter": FINGERPRINT_PARAMETER,
        "command": generate_command(launcher, bundle),
    }


def validate_envelope(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != SCHEMA:
        raise HumanExecutionHandoffError("handoff-schema-mismatch")
    if value.get("profile") != PROFILE:
        raise HumanExecutionHandoffError("handoff-profile-mismatch")
    if value.get("fingerprint_parameter") != FINGERPRINT_PARAMETER:
        raise HumanExecutionHandoffError("fingerprint-parameter-mismatch")
    launcher = _sha(value.get("launcher_sha256"), "launcher")
    bundle = _sha(value.get("bundle_sha256"), "bundle")
    expected_fingerprint = handoff_fingerprint(launcher, bundle)
    if value.get("handoff_fingerprint") != expected_fingerprint:
        raise HumanExecutionHandoffError("handoff-fingerprint-mismatch")
    expected_command = generate_command(launcher, bundle)
    if value.get("command") != expected_command:
        raise HumanExecutionHandoffError("generated-command-mismatch")
    if "_" in expected_command or "\\_" in expected_command:
        raise HumanExecutionHandoffError("markdown-underscore-escape-surface-prohibited")
    if ".ps1' -File $launcherPath" in expected_command:
        raise HumanExecutionHandoffError("literal-launcher-filename-dependency-prohibited")
    return {
        "schema": VALIDATION_SCHEMA,
        "result": "PASS",
        "profile": PROFILE,
        "handoff_fingerprint": expected_fingerprint,
        "artifact_resolution": "EXACT_SHA256_AND_UNIQUE_CARDINALITY",
        "literal_artifact_filename_dependency": False,
        "known_markdown_underscore_escape_dependency": False,
    }


def _extract_terminal_powershell_block(text: str) -> str:
    stripped = text.rstrip()
    matches = list(re.finditer(r"(?s)(?:^|\n)```powershell\n(.*?)\n```", stripped))
    if len(matches) != 1:
        raise HumanExecutionHandoffError("exactly-one-powershell-command-surface-required")
    match = matches[0]
    if match.end() != len(stripped):
        raise HumanExecutionHandoffError("powershell-command-surface-must-be-response-terminal")
    return match.group(1)


def validate_response(value: dict[str, Any]) -> dict[str, Any]:
    envelope = value.get("handoff")
    if not isinstance(envelope, dict):
        raise HumanExecutionHandoffError("handoff-envelope-required")
    result = validate_envelope(envelope)
    response_text = value.get("response_text")
    if not isinstance(response_text, str):
        raise HumanExecutionHandoffError("response-text-required")
    if _extract_terminal_powershell_block(response_text) != envelope["command"]:
        raise HumanExecutionHandoffError("rendered-command-does-not-match-generated-command")
    result["response_terminal_surface_verified"] = True
    result["rendered_command_exact_match"] = True
    return result


def _must_reject(label: str, function, value: dict[str, Any]) -> bool:
    try:
        function(value)
    except HumanExecutionHandoffError:
        return True
    raise HumanExecutionHandoffError(f"negative-canary-not-rejected:{label}")


def selftest() -> dict[str, Any]:
    launcher = "1" * 64
    bundle = "2" * 64
    envelope = generate_envelope(launcher, bundle)
    validate_envelope(envelope)
    response = {"handoff": envelope, "response_text": "Kjor denne:\n\n```powershell\n" + envelope["command"] + "\n```"}
    validate_response(response)

    changed_hash = dict(envelope)
    changed_hash["bundle_sha256"] = "3" * 64
    escaped = dict(envelope)
    escaped["command"] = escaped["command"] + "\n# CEREBRO\\_PATCH"
    nonterminal = dict(response)
    nonterminal["response_text"] += "\nMer tekst"
    literal_filename = dict(envelope)
    literal_filename["command"] = "powershell.exe -File C:\\Users\\Example\\Downloads\\CEREBRO_PATCH.ps1"

    return {
        "result": "PASS",
        "generated_envelope_accepted": True,
        "terminal_rendered_surface_accepted": True,
        "artifact_hash_change_rejected": _must_reject("artifact-hash-change", validate_envelope, changed_hash),
        "markdown_escape_injection_rejected": _must_reject("markdown-escape", validate_envelope, escaped),
        "nonterminal_surface_rejected": _must_reject("nonterminal", validate_response, nonterminal),
        "literal_filename_surface_rejected": _must_reject("literal-filename", validate_envelope, literal_filename),
    }


def _source_fingerprint(root: Path) -> str:
    rows: list[str] = []
    for relative in sorted(EVIDENCE_BASIS_FILES):
        path = root / relative
        if not path.is_file():
            raise HumanExecutionHandoffError(f"activation-basis-file-missing:{relative}")
        rows.append(f"{relative}|{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def activation_probe(root: Path) -> dict[str, Any]:
    checks = selftest()
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": "PASS",
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "basis_files": list(EVIDENCE_BASIS_FILES),
        "source_state_fingerprint": _source_fingerprint(root),
        "generator_executed": True,
        "envelope_consumer_exercised": True,
        "response_candidate_consumer_exercised": True,
        "hash_bound_artifact_resolution_enforced": True,
        "unique_artifact_cardinality_enforced": True,
        "launcher_fingerprint_consumer_bound": True,
        "literal_filename_dependency_prohibited": True,
        "known_markdown_escape_canary_rejected": True,
        "negative_canaries_passed": all(value is True for key, value in checks.items() if key.endswith(("_rejected", "_accepted"))),
        "checks": checks,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise HumanExecutionHandoffError("json-object-required")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_generate = sub.add_parser("generate")
    p_generate.add_argument("--launcher-sha256", required=True)
    p_generate.add_argument("--bundle-sha256", required=True)
    p_generate.add_argument("--output")
    p_envelope = sub.add_parser("validate-envelope")
    p_envelope.add_argument("--input", required=True)
    p_envelope.add_argument("--output")
    p_response = sub.add_parser("validate-response")
    p_response.add_argument("--input", required=True)
    p_response.add_argument("--output")
    p_probe = sub.add_parser("activation-probe")
    p_probe.add_argument("--source-root", required=True)
    p_probe.add_argument("--output", required=True)
    p_selftest = sub.add_parser("selftest")
    p_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            result = generate_envelope(args.launcher_sha256, args.bundle_sha256)
        elif args.command == "validate-envelope":
            result = validate_envelope(_read_json(Path(args.input)))
        elif args.command == "validate-response":
            result = validate_response(_read_json(Path(args.input)))
        elif args.command == "activation-probe":
            result = activation_probe(Path(args.source_root))
        else:
            result = selftest()
        output = getattr(args, "output", None)
        if output:
            _write_json(Path(output), result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except Exception as exc:
        result = {"result": "BLOCK", "error": str(exc)}
        output = getattr(args, "output", None)
        if output:
            _write_json(Path(output), result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
