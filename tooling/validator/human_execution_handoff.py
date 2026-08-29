#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "cerebro-human-execution-handoff/v1"
EXECUTION_UNIT_SCHEMA = "cerebro-human-execution-unit/v1"
VALIDATION_SCHEMA = "cerebro-human-execution-handoff-validation/v1"
ACTIVATION_SCHEMA = "cerebro-human-execution-handoff-activation-proof/v1"
PROFILE = "HASH_BOUND_POWERSHELL"
BINDING_ID = "HUMAN_EXECUTION_HANDOFF_TRANSPORT"
FINGERPRINT_PARAMETER = "HumanExecutionHandoffFingerprint"
BUILDER_ID = "tooling.builder"
IMPLEMENTER_ROLE = "IMPLEMENTER"
HUMAN_ROLE = "HUMAN"
EVIDENCE_BASIS_FILES = (
    "standards/human-continuation-surface.yaml",
    "standards/continuation-surface-system-policy.yaml",
    "standards/change-delivery.yaml",
    "standards/development/delivery-failure-regression.yaml",
    "mcp/manifest.yaml",
    "engines/presentation/rules.yaml",
    "tooling/builder/builder.yaml",
    "tooling/delivery/Cerebro.StandardDeliveryLauncher.ps1",
    "tooling/validator/human_execution_handoff.py",
    "tooling/validator/checks.yaml",
    "tooling/validator/contract-activation-bindings.json",
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SOURCE_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class HumanExecutionHandoffError(ValueError):
    pass


def _sha(value: Any, label: str) -> str:
    normalized = str(value).strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise HumanExecutionHandoffError(f"{label}-sha256-invalid")
    return normalized


def _nonempty(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HumanExecutionHandoffError(f"{label}-required")
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


def generate_execution_unit(
    launcher_sha256: str,
    bundle_sha256: str,
    producer_id: str,
    source_revision: str,
    work_mode_capability_proven: bool,
    local_receipt_observable: bool,
) -> dict[str, Any]:
    producer = _nonempty(producer_id, "producer-id")
    if not producer.startswith("IMPLEMENTER_"):
        raise HumanExecutionHandoffError("bound-implementer-id-required")
    revision = str(source_revision or "").strip().lower()
    if not SOURCE_REVISION_PATTERN.fullmatch(revision):
        raise HumanExecutionHandoffError("source-revision-invalid")
    if work_mode_capability_proven is not True or local_receipt_observable is not True:
        raise HumanExecutionHandoffError("proven-work-mode-local-receipt-capability-required")
    handoff = generate_envelope(launcher_sha256, bundle_sha256)
    return {
        "schema": EXECUTION_UNIT_SCHEMA,
        "builder": {
            "id": BUILDER_ID,
            "generated": True,
            "artifact_set_complete": True,
        },
        "producer": {"role": IMPLEMENTER_ROLE, "actor_id": producer},
        "qualifier": {"role": IMPLEMENTER_ROLE, "actor_id": producer},
        "bound_implementer_id": producer,
        "delivery_actor": {"role": IMPLEMENTER_ROLE, "actor_id": producer},
        "responder": {"role": IMPLEMENTER_ROLE, "actor_id": producer},
        "work_mode": {
            "enabled": True,
            "capability_proven": work_mode_capability_proven,
            "local_receipt_observable": local_receipt_observable,
        },
        "artifacts": {
            "launcher": {"sha256": handoff["launcher_sha256"]},
            "bundle": {"sha256": handoff["bundle_sha256"]},
        },
        "handoff": handoff,
        "generated_powershell_command": handoff["command"],
        "human_execution": {
            "actor_role": HUMAN_ROLE,
            "authorized": True,
            "one_shot": True,
            "terminal_outcome_route": "PRINCIPAL",
        },
        "post_run_receipt": {
            "owner_actor_id": producer,
            "self_consume_when_observable": True,
            "human_transport_requested": False,
        },
        "source_revision": revision,
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


def _validate_implementer_actor(value: Any, label: str, expected_id: str) -> None:
    if not isinstance(value, dict):
        raise HumanExecutionHandoffError(f"{label}-required")
    if value.get("role") != IMPLEMENTER_ROLE:
        raise HumanExecutionHandoffError(f"{label}-role-must-be-implementer")
    actor_id = _nonempty(value.get("actor_id"), f"{label}-actor-id")
    if actor_id != expected_id:
        raise HumanExecutionHandoffError(f"{label}-must-match-bound-implementer")


def validate_execution_unit(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema") != EXECUTION_UNIT_SCHEMA:
        raise HumanExecutionHandoffError("execution-unit-schema-mismatch")
    builder = value.get("builder")
    if not isinstance(builder, dict):
        raise HumanExecutionHandoffError("builder-binding-required")
    if (
        builder.get("id") != BUILDER_ID
        or builder.get("generated") is not True
        or builder.get("artifact_set_complete") is not True
    ):
        raise HumanExecutionHandoffError("builder-generated-complete-unit-required")

    bound_id = _nonempty(value.get("bound_implementer_id"), "bound-implementer-id")
    if not bound_id.startswith("IMPLEMENTER_"):
        raise HumanExecutionHandoffError("bound-implementer-id-required")
    _validate_implementer_actor(value.get("producer"), "producer", bound_id)
    _validate_implementer_actor(value.get("qualifier"), "qualifier", bound_id)
    _validate_implementer_actor(value.get("delivery_actor"), "delivery-actor", bound_id)
    _validate_implementer_actor(value.get("responder"), "responder", bound_id)

    work_mode = value.get("work_mode")
    if not isinstance(work_mode, dict):
        raise HumanExecutionHandoffError("work-mode-binding-required")
    if (
        work_mode.get("enabled") is not True
        or work_mode.get("capability_proven") is not True
        or work_mode.get("local_receipt_observable") is not True
    ):
        raise HumanExecutionHandoffError("proven-work-mode-local-receipt-capability-required")

    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"launcher", "bundle"}:
        raise HumanExecutionHandoffError("complete-launcher-and-bundle-artifact-set-required")
    launcher_artifact = artifacts.get("launcher")
    bundle_artifact = artifacts.get("bundle")
    if not isinstance(launcher_artifact, dict) or not isinstance(bundle_artifact, dict):
        raise HumanExecutionHandoffError("artifact-identities-required")

    handoff = value.get("handoff")
    if not isinstance(handoff, dict):
        raise HumanExecutionHandoffError("generated-handoff-required")
    handoff_result = validate_envelope(handoff)
    if _sha(launcher_artifact.get("sha256"), "launcher-artifact") != handoff["launcher_sha256"]:
        raise HumanExecutionHandoffError("launcher-artifact-handoff-mismatch")
    if _sha(bundle_artifact.get("sha256"), "bundle-artifact") != handoff["bundle_sha256"]:
        raise HumanExecutionHandoffError("bundle-artifact-handoff-mismatch")
    if value.get("generated_powershell_command") != handoff["command"]:
        raise HumanExecutionHandoffError("generated-powershell-command-required")

    human_execution = value.get("human_execution")
    if not isinstance(human_execution, dict):
        raise HumanExecutionHandoffError("human-execution-boundary-required")
    if (
        human_execution.get("actor_role") != HUMAN_ROLE
        or human_execution.get("authorized") is not True
        or human_execution.get("one_shot") is not True
        or human_execution.get("terminal_outcome_route") != "PRINCIPAL"
    ):
        raise HumanExecutionHandoffError("genuine-authorized-human-one-shot-boundary-required")

    post_run = value.get("post_run_receipt")
    if not isinstance(post_run, dict):
        raise HumanExecutionHandoffError("post-run-receipt-ownership-required")
    if post_run.get("owner_actor_id") != bound_id:
        raise HumanExecutionHandoffError("post-run-receipt-owner-must-be-bound-implementer")
    if post_run.get("self_consume_when_observable") is not True:
        raise HumanExecutionHandoffError("work-mode-receipt-self-consumption-required")
    if post_run.get("human_transport_requested") is not False:
        raise HumanExecutionHandoffError("human-receipt-transport-prohibited-when-observable")

    revision = str(value.get("source_revision") or "").strip().lower()
    if not SOURCE_REVISION_PATTERN.fullmatch(revision):
        raise HumanExecutionHandoffError("source-revision-invalid")
    return {
        "schema": "cerebro-human-execution-unit-validation/v1",
        "result": "PASS",
        "builder_id": BUILDER_ID,
        "producer_id": bound_id,
        "qualifier_id": bound_id,
        "delivery_actor_id": bound_id,
        "responder_id": bound_id,
        "complete_artifact_set_verified": True,
        "generated_command_verified": True,
        "work_mode_receipt_owner_verified": True,
        "human_one_shot_boundary_preserved": True,
        "principal_outcome_route_preserved": True,
        "handoff_fingerprint": handoff_result["handoff_fingerprint"],
        "source_revision": revision,
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


def validate_execution_response(value: dict[str, Any]) -> dict[str, Any]:
    execution_unit = value.get("execution_unit")
    if not isinstance(execution_unit, dict):
        raise HumanExecutionHandoffError("execution-unit-required")
    result = validate_execution_unit(execution_unit)
    validate_response(
        {
            "handoff": execution_unit["handoff"],
            "response_text": value.get("response_text"),
        }
    )
    result["response_terminal_surface_verified"] = True
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

    unit = generate_execution_unit(launcher, bundle, "IMPLEMENTER_GENERIC_TEST", "a" * 40, True, True)
    execution_response = {
        "execution_unit": unit,
        "response_text": "Kjor denne:\n\n```powershell\n" + unit["generated_powershell_command"] + "\n```",
    }
    validate_execution_response(execution_response)
    pm_producer = copy.deepcopy(unit)
    pm_producer["producer"] = {"role": "PROJECT_MANAGER", "actor_id": "PROJECT_MANAGER_TEST"}
    pm_responder = copy.deepcopy(unit)
    pm_responder["responder"] = {"role": "PROJECT_MANAGER", "actor_id": "PROJECT_MANAGER_TEST"}
    pm_qualifier = copy.deepcopy(unit)
    pm_qualifier["qualifier"] = {"role": "PROJECT_MANAGER", "actor_id": "PROJECT_MANAGER_TEST"}
    pm_delivery_actor = copy.deepcopy(unit)
    pm_delivery_actor["delivery_actor"] = {"role": "PROJECT_MANAGER", "actor_id": "PROJECT_MANAGER_TEST"}
    missing_launcher = copy.deepcopy(unit)
    missing_launcher["artifacts"].pop("launcher")
    missing_command = copy.deepcopy(unit)
    missing_command.pop("generated_powershell_command")
    human_receipt_owner = copy.deepcopy(unit)
    human_receipt_owner["post_run_receipt"]["owner_actor_id"] = "HUMAN_ADMIN"
    disabled_work_mode = copy.deepcopy(unit)
    disabled_work_mode["work_mode"]["capability_proven"] = False
    non_principal_outcome_route = copy.deepcopy(unit)
    non_principal_outcome_route["human_execution"]["terminal_outcome_route"] = "PROJECT_MANAGER"

    return {
        "result": "PASS",
        "generated_envelope_accepted": True,
        "terminal_rendered_surface_accepted": True,
        "artifact_hash_change_rejected": _must_reject("artifact-hash-change", validate_envelope, changed_hash),
        "markdown_escape_injection_rejected": _must_reject("markdown-escape", validate_envelope, escaped),
        "nonterminal_surface_rejected": _must_reject("nonterminal", validate_response, nonterminal),
        "literal_filename_surface_rejected": _must_reject("literal-filename", validate_envelope, literal_filename),
        "builder_generated_implementer_execution_unit_accepted": True,
        "pm_producer_rejected": _must_reject("pm-producer", validate_execution_unit, pm_producer),
        "pm_responder_rejected": _must_reject("pm-responder", validate_execution_unit, pm_responder),
        "pm_qualifier_rejected": _must_reject("pm-qualifier", validate_execution_unit, pm_qualifier),
        "pm_delivery_actor_rejected": _must_reject("pm-delivery-actor", validate_execution_unit, pm_delivery_actor),
        "missing_launcher_rejected": _must_reject("missing-launcher", validate_execution_unit, missing_launcher),
        "missing_generated_command_rejected": _must_reject("missing-generated-command", validate_execution_unit, missing_command),
        "human_receipt_owner_rejected": _must_reject("human-receipt-owner", validate_execution_unit, human_receipt_owner),
        "unproven_work_mode_rejected": _must_reject("unproven-work-mode", validate_execution_unit, disabled_work_mode),
        "non_principal_outcome_route_rejected": _must_reject(
            "non-principal-outcome-route", validate_execution_unit, non_principal_outcome_route
        ),
        "human_one_shot_boundary_accepted": validate_execution_unit(unit)["human_one_shot_boundary_preserved"],
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
        "builder_generated_execution_unit_enforced": checks.get("builder_generated_implementer_execution_unit_accepted") is True,
        "pm_producer_qualifier_delivery_and_responder_blocked": all(
            checks.get(field) is True
            for field in (
                "pm_producer_rejected",
                "pm_qualifier_rejected",
                "pm_delivery_actor_rejected",
                "pm_responder_rejected",
            )
        ),
        "complete_artifact_set_enforced": checks.get("missing_launcher_rejected") is True,
        "generated_command_required": checks.get("missing_generated_command_rejected") is True,
        "work_mode_receipt_self_consumption_enforced": checks.get("human_receipt_owner_rejected") is True and checks.get("unproven_work_mode_rejected") is True,
        "human_one_shot_boundary_preserved": checks.get("human_one_shot_boundary_accepted") is True,
        "principal_outcome_route_preserved": (
            checks.get("human_one_shot_boundary_accepted") is True
            and checks.get("non_principal_outcome_route_rejected") is True
        ),
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
    p_generate_unit = sub.add_parser("generate-execution-unit")
    p_generate_unit.add_argument("--launcher-sha256", required=True)
    p_generate_unit.add_argument("--bundle-sha256", required=True)
    p_generate_unit.add_argument("--producer-id", required=True)
    p_generate_unit.add_argument("--source-revision", required=True)
    p_generate_unit.add_argument("--work-mode-capability-proven", action="store_true")
    p_generate_unit.add_argument("--local-receipt-observable", action="store_true")
    p_generate_unit.add_argument("--output")
    p_envelope = sub.add_parser("validate-envelope")
    p_envelope.add_argument("--input", required=True)
    p_envelope.add_argument("--output")
    p_response = sub.add_parser("validate-response")
    p_response.add_argument("--input", required=True)
    p_response.add_argument("--output")
    p_unit = sub.add_parser("validate-execution-unit")
    p_unit.add_argument("--input", required=True)
    p_unit.add_argument("--output")
    p_execution_response = sub.add_parser("validate-execution-response")
    p_execution_response.add_argument("--input", required=True)
    p_execution_response.add_argument("--output")
    p_probe = sub.add_parser("activation-probe")
    p_probe.add_argument("--source-root", required=True)
    p_probe.add_argument("--output", required=True)
    p_selftest = sub.add_parser("selftest")
    p_selftest.add_argument("--output")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            result = generate_envelope(args.launcher_sha256, args.bundle_sha256)
        elif args.command == "generate-execution-unit":
            result = generate_execution_unit(
                args.launcher_sha256,
                args.bundle_sha256,
                args.producer_id,
                args.source_revision,
                args.work_mode_capability_proven,
                args.local_receipt_observable,
            )
        elif args.command == "validate-envelope":
            result = validate_envelope(_read_json(Path(args.input)))
        elif args.command == "validate-response":
            result = validate_response(_read_json(Path(args.input)))
        elif args.command == "validate-execution-unit":
            result = validate_execution_unit(_read_json(Path(args.input)))
        elif args.command == "validate-execution-response":
            result = validate_execution_response(_read_json(Path(args.input)))
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
