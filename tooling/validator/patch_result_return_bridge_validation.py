#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SCHEMA = "cerebro-patch-result-return-bridge-validation/v1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def powershell() -> str:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        value = shutil.which(name)
        if value:
            return value
    raise RuntimeError("POWERSHELL_NOT_FOUND")


def run(command: list[str], *, expected: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode not in expected:
        raise RuntimeError(
            f"NATIVE_EXIT_NOT_ALLOWED:{result.returncode}:"
            f"{result.stdout[-3000:]}:{result.stderr[-3000:]}"
        )
    return result


def parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def invoke_pump(
    pump: Path,
    mode: str,
    *,
    outbox: Path | None = None,
    drive: Path | None = None,
    artifact: Path | None = None,
    attempt: str = "",
    result: str = "PASS",
    failure_family: str = "",
    package: Path | None = None,
    expected: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    command = [
        powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(pump),
        "-Mode",
        mode,
    ]
    if outbox is not None:
        command += ["-OutboxRoot", str(outbox)]
    if drive is not None:
        command += ["-DriveReturnRoot", str(drive)]
    if package is not None:
        command += ["-PackagePath", str(package)]
    if artifact is not None:
        command += [
            "-AttemptId",
            attempt,
            "-PatchId",
            "PATCH527-SELFTEST",
            "-ClaimId",
            "CLAIM764-SELFTEST",
            "-Result",
            result,
            "-SourceBefore",
            "0" * 40,
            "-SourceAfter",
            "1" * 40,
            "-ProductSha256",
            sha256(artifact),
            "-ReachedStage",
            "VALIDATION",
            "-SourceMutationAssessment",
            "NO_UNCOMMITTED_SOURCE_MUTATION_PRESENT",
            "-ArtifactPaths",
            str(artifact),
        ]
        if failure_family:
            command += ["-FailureFamily", failure_family]
        if result == "PASS":
            command += ["-CerebroSyncVerified"]
    return run(command, expected=expected)


def validate(root: Path) -> dict[str, Any]:
    pump = root / "tooling/return-bridge/Cerebro.ReturnBridgePump.ps1"
    launcher = root / "tooling/delivery/Cerebro.StandardDeliveryLauncher.ps1"
    diagnostic = root / "tooling/host/diagnostic_capsule.py"
    standard = root / "standards/patch-result-return-bridge.yaml"
    delivery = root / "standards/delivery-kernel.yaml"
    checks = root / "tooling/validator/checks.yaml"
    required = (pump, launcher, diagnostic, standard, delivery, checks)
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    tests: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str = "") -> None:
        tests.append({"name": name, "result": "PASS" if passed else "FAIL", "detail": detail})

    record("required-files", not missing, ",".join(missing))
    if missing:
        return {"schema": SCHEMA, "result": "FAIL", "tests": tests}

    standard_text = standard.read_text(encoding="utf-8-sig")
    delivery_text = delivery.read_text(encoding="utf-8-sig")
    launcher_text = launcher.read_text(encoding="utf-8-sig")
    diagnostic_text = diagnostic.read_text(encoding="utf-8-sig")
    checks_text = checks.read_text(encoding="utf-8-sig")
    record(
        "contract-invariants",
        all(
            token in standard_text
            for token in (
                "ONE_WAY_RESULT_RETURN",
                "PENDING_NO_PATCH_RERUN",
                "REJECT_ID_HASH_COLLISION",
                "ZERO_HUMAN_FILE_TOUCH",
                "Human file courier on the normal path",
            )
        ),
    )
    record(
        "delivery-kernel-network-free-composition",
        "patch_result_return_bridge:" in delivery_text
        and "network_semantics_in_delivery_kernel: PROHIBITED" in delivery_text,
    )
    record(
        "launcher-wiring",
        all(
            token in launcher_text
            for token in (
                "Publish-ReturnBridgeResult",
                "Cerebro.ReturnBridgePump.ps1",
                "RETURN_BRIDGE_STATE",
                "return_bridge_ref",
            )
        ),
    )
    record(
        "diagnostic-sufficiency",
        all(
            token in diagnostic_text
            for token in (
                '"raw_error_bounded"',
                '"transcript_excerpt"',
                '"diagnostic_classification"',
                "PARTIAL_DIAGNOSTIC",
                "CLASSIFICATION_SUFFICIENT",
            )
        ),
    )
    record(
        "checks-registration",
        "patch_result_return_bridge_validation:" in checks_text
        and "tooling/validator/patch_result_return_bridge_validation.py" in checks_text,
    )

    with tempfile.TemporaryDirectory(prefix="cerebro-return-bridge-validation-") as temp:
        work = Path(temp)
        outbox = work / "outbox"
        drive = work / "drive"
        drive.mkdir()
        pass_artifact = work / "pass.json"
        pass_artifact.write_text('{"result":"PASS"}\n', encoding="utf-8")
        fail_artifact = work / "diagnostic.json"
        write_json(
            fail_artifact,
            {
                "capsule_id": "DCAP-SELFTEST",
                "failure": {
                    "stage": "APPLY",
                    "message": "bounded useful diagnostic",
                    "exit_code": 7,
                    "failure_family": "SELFTEST_FAILURE",
                    "raw_error_bounded": "bounded stderr",
                },
                "diagnostic_classification": "CLASSIFICATION_SUFFICIENT",
            },
        )

        first = invoke_pump(
            pump,
            "Enqueue",
            outbox=outbox,
            artifact=pass_artifact,
            attempt="ATTEMPT-PASS",
        )
        first_fields = parse_fields(first.stdout)
        package = Path(first_fields["RETURN_BRIDGE_PACKAGE"])
        ready = package / "READY.json"
        names = [path.name for path in sorted(package.iterdir(), key=lambda path: path.stat().st_mtime_ns)]
        record("outbox-atomic-ready-last", ready.is_file() and names[-1] == "READY.json", ",".join(names))

        duplicate = invoke_pump(
            pump,
            "Enqueue",
            outbox=outbox,
            artifact=pass_artifact,
            attempt="ATTEMPT-PASS",
        )
        record(
            "duplicate-no-duplicate-effect",
            parse_fields(duplicate.stdout).get("RETURN_BRIDGE_STATE") == "DUPLICATE",
        )

        collision_artifact = work / "pass-collision.json"
        collision_artifact.write_text('{"result":"DIFFERENT"}\n', encoding="utf-8")
        collision = invoke_pump(
            pump,
            "Enqueue",
            outbox=outbox,
            artifact=collision_artifact,
            attempt="ATTEMPT-PASS",
            expected=(1,),
        )
        record("id-hash-collision-reject", "ID_HASH_COLLISION" in collision.stderr)

        drain = invoke_pump(pump, "Drain", outbox=outbox, drive=drive)
        drain_fields = parse_fields(drain.stdout)
        provider = drive / first_fields["RETURN_BRIDGE_ENVELOPE"]
        verify = invoke_pump(pump, "Verify", package=provider)
        verified = json.loads(verify.stdout)
        record(
            "pass-provider-readback",
            drain_fields.get("RETURN_BRIDGE_STATE") == "DELIVERED"
            and verified.get("Result") == "PASS",
        )

        failure = invoke_pump(
            pump,
            "Enqueue",
            outbox=outbox,
            artifact=fail_artifact,
            attempt="ATTEMPT-FAIL",
            result="FAIL",
            failure_family="SELFTEST_FAILURE",
        )
        invoke_pump(pump, "Drain", outbox=outbox, drive=drive)
        failure_provider = drive / parse_fields(failure.stdout)["RETURN_BRIDGE_ENVELOPE"]
        failure_verify = json.loads(invoke_pump(pump, "Verify", package=failure_provider).stdout)
        diag_files = [
            item
            for item in failure_provider.iterdir()
            if item.name not in {"READY.json", "manifest.json", "envelope.json"}
        ]
        diag_text = "\n".join(path.read_text(encoding="utf-8-sig") for path in diag_files)
        record(
            "fail-auto-return-useful-diagnostic",
            failure_verify.get("Result") == "PASS"
            and "bounded useful diagnostic" in diag_text
            and "CLASSIFICATION_SUFFICIENT" in diag_text,
        )

        unavailable_root = work / "missing-drive"
        pending = invoke_pump(
            pump,
            "Enqueue",
            outbox=outbox,
            artifact=pass_artifact,
            attempt="ATTEMPT-RECOVERY",
        )
        unavailable = invoke_pump(pump, "Drain", outbox=outbox, drive=unavailable_root)
        record(
            "drive-unavailable-pending-no-rerun",
            parse_fields(unavailable.stdout).get("RETURN_BRIDGE_STATE")
            == "PENDING_DRIVE_UNAVAILABLE"
            and Path(parse_fields(pending.stdout)["RETURN_BRIDGE_PACKAGE"]).is_dir(),
        )
        unavailable_root.mkdir()
        recovery = invoke_pump(pump, "Drain", outbox=outbox, drive=unavailable_root)
        record(
            "recovery-drain",
            parse_fields(recovery.stdout).get("RETURN_BRIDGE_STATE") == "DELIVERED",
        )

        incomplete = work / "incomplete"
        incomplete.mkdir()
        write_json(incomplete / "envelope.json", {"schema": "cerebro-patch-result-return-envelope/v1"})
        incomplete_verify = invoke_pump(pump, "Verify", package=incomplete, expected=(1,))
        incomplete_result = json.loads(incomplete_verify.stdout)
        record("incomplete-ignored", incomplete_result.get("Result") == "INCOMPLETE")

        tampered = work / "tampered"
        shutil.copytree(provider, tampered)
        artifact_targets = [
            path
            for path in tampered.iterdir()
            if path.name not in {"READY.json", "manifest.json", "envelope.json"}
        ]
        artifact_targets[0].write_bytes(artifact_targets[0].read_bytes() + b"tamper")
        tampered_verify = invoke_pump(pump, "Verify", package=tampered, expected=(1,))
        record("hash-mismatch-reject", "ARTIFACT_SHA256" in tampered_verify.stdout)

    python = shutil.which("python") or shutil.which("python.exe") or "python"
    diag_selftest = run([os.fspath(python), "-B", str(diagnostic), "selftest"])
    record("diagnostic-selftest", '"result": "PASS"' in diag_selftest.stdout)

    passed = all(test["result"] == "PASS" for test in tests)
    basis_paths = [
        "standards/delivery-kernel.yaml",
        "standards/patch-result-return-bridge.yaml",
        "tooling/delivery/Cerebro.StandardDeliveryLauncher.ps1",
        "tooling/host/diagnostic_capsule.py",
        "tooling/return-bridge/Cerebro.ReturnBridgePump.ps1",
        "tooling/validator/checks.yaml",
        "tooling/validator/patch_result_return_bridge_validation.py",
    ]
    basis = hashlib.sha256()
    for relative in basis_paths:
        path = root / relative
        basis.update(relative.encode("utf-8"))
        basis.update(b"\0")
        basis.update(path.read_bytes())
        basis.update(b"\0")
    return {
        "schema": SCHEMA,
        "result": "PASS" if passed else "FAIL",
        "binding_id": "PATCH_RESULT_RETURN_BRIDGE",
        "proves_bindings": ["PATCH_RESULT_RETURN_BRIDGE"],
        "authority": "DERIVED_OPERATIONAL_EVIDENCE",
        "basis_files": basis_paths,
        "source_state_fingerprint": basis.hexdigest(),
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("selftest", "activation-probe"), default="selftest")
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output")
    args = parser.parse_args()
    report = validate(Path(args.source_root).resolve())
    rendered = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
