#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT_SOURCE = "github:morgul-tech/Cerebro-Source-1.0/main/cerebro.yaml"
ACTIVATION_SCHEMA = "cerebro-operational-status-semantics-activation-proof/v1"


def resolve(command: str, candidates: list[dict[str, str]], current_commit: str) -> dict[str, str]:
    if command.strip().lower() not in {"boot cerebro", "bootcerebro", "bootini"}:
        raise ValueError("BOOT_COMMAND_UNRECOGNIZED")
    source = next((item for item in candidates if item.get("identity") == CURRENT_SOURCE), None)
    if source is None:
        raise ValueError("AUTHORITATIVE_SOURCE_MISSING")
    if source.get("commit") != current_commit:
        raise ValueError("AUTHORITATIVE_SOURCE_COMMIT_MISMATCH")
    return {"canonical_command": "bootCerebro", "authority": CURRENT_SOURCE, "commit": current_commit}


def selftest(root: Path = ROOT, bootengine_path: Path | None = None) -> dict[str, object]:
    commit = "CURRENT-COMMIT"
    source = {"identity": CURRENT_SOURCE, "commit": commit, "kind": "SOURCE"}
    legacy_docx = {"identity": "library:Regelverk_v1.4_Master_CURRENT.docx", "commit": "", "kind": "DOCUMENT"}
    stale_receipt = {"identity": "receipt:SELF-RUNNING-REBUILD-006", "commit": "OLD", "kind": "RECEIPT"}
    stale_handoff = {"identity": "handoff:OLD", "commit": "OLD", "kind": "HANDOFF"}
    tests: list[dict[str, str]] = []

    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})

    for alias in ("boot cerebro", "BOOT CEREBRO", "bootCerebro", "bootini"):
        result = resolve(alias, [legacy_docx, stale_receipt, stale_handoff, source], commit)
        check(f"alias:{alias}", result["canonical_command"] == "bootCerebro")
    result = resolve("boot cerebro", [legacy_docx, stale_receipt, stale_handoff, source], commit)
    check("legacy-docx-current-has-no-authority", result["authority"] == CURRENT_SOURCE)
    check("stale-rebuild-receipt-cannot-block", result["commit"] == commit)
    check("stale-handoff-cannot-override", result["commit"] != stale_handoff["commit"])

    handboot = (root / "standards/runtime/handboot.yaml").read_text(encoding="utf-8")
    source_authority = (root / "standards/source-authority.yaml").read_text(encoding="utf-8")
    activation = (root / "mcp/activation.yaml").read_text(encoding="utf-8")
    boot_control = (root / "mcp/boot-architecture-control.yaml").read_text(encoding="utf-8")
    boot_architecture = (root / "standards/boot-critical-path-architecture.yaml").read_text(encoding="utf-8")
    terminology = (root / "modules/terminology/terms.yaml").read_text(encoding="utf-8")
    interaction = (root / "engines/interaction/rules.yaml").read_text(encoding="utf-8")

    required = [
        "CURRENT-in-a-filename-confers-no-authority",
        "REJECT_INPUT_CONTINUE_CURRENT_SOURCE",
        "boot cerebro",
        "ACTIVE_CONTROL_TRANSFERRED",
    ]
    combined = handboot + source_authority + activation
    check("source-contract-tokens", all(token in combined for token in required))

    check(
        "universal-source-operational-does-not-require-working-source",
        "CEREBRO_SOURCE_OPERATIONAL" in boot_control
        and "It does not require Working Source or local runtime access." in boot_control,
    )
    check(
        "conversation-source-alignment-is-explicit",
        "CONVERSATION_SOURCE_ALIGNMENT" in boot_control
        and "conversation_source_alignment:" in boot_architecture
        and "local_working_source_equality_required: false" in boot_architecture,
    )
    check(
        "strict-cerebro-sync-verified-is-separate",
        "CEREBRO_SYNC_VERIFIED" in boot_control
        and "canonical cerebro_sync" in boot_control
        and "strict_non_equivalence: CEREBRO_SYNC_VERIFIED" in terminology,
    )
    check(
        "legacy-handboot-is-local-machine-extension",
        "class: LOCAL_MACHINE_CAPABILITY_EXTENSION" in handboot
        and "status_dimension: LOCAL_MACHINE_OPERATIONAL_VERIFIED" in handboot
        and "does_not_gate: [CEREBRO_SOURCE_OPERATIONAL, CONVERSATION_SOURCE_ALIGNMENT]" in handboot,
    )
    check(
        "ordinary-status-routes-source-first",
        "INT-OPS-001" in interaction
        and "resolve_primary_dimension: CEREBRO_SOURCE_OPERATIONAL" in interaction,
    )
    check(
        "source-sync-phrase-routes-conversation-alignment",
        "INT-OPS-002" in interaction
        and "resolve_primary_dimension: CONVERSATION_SOURCE_ALIGNMENT" in interaction
        and "do_not_promote_to: CEREBRO_SYNC_VERIFIED" in interaction,
    )
    check(
        "local-caveat-is-relevance-conditioned",
        "INT-OPS-004" in interaction
        and "suppress_automatic_working_source_caveat: true" in interaction
        and "surface_only_when_explicitly_asked_or_material: true" in interaction,
    )
    check(
        "success-semantics-are-available",
        "Yes — this conversation is now running against the current authoritative Cerebro Source, and Cerebro is actively being used for my responses." in interaction,
    )

    if bootengine_path is not None:
        bootengine = bootengine_path.read_text(encoding="utf-8")
        bootengine_required = [
            "Command.NaturalLanguageAlias := boot cerebro",
            "Command.Match := CASE_INSENSITIVE_EXACT_TRIMMED",
            "Authority.First := github:morgul-tech/Cerebro-Source-1.0/main/cerebro.yaml",
            "FilenameMarker.CURRENT.Authority := NONE",
            "StaleDerivedState.Action := REJECT_INPUT_CONTINUE_CURRENT_SOURCE",
            "OperationalClaim.Requires := ACTIVE_CONTROL_TRANSFERRED_RECEIPT_AT_CURRENT_SOURCE_COMMIT",
        ]
        check("bootengine-earliest-gate-contract", all(token in bootengine for token in bootengine_required))

    passed = all(item["result"] == "PASS" for item in tests)
    return {
        "schema": "cerebro-boot-authority-selftest/v0.2",
        "result": "PASS" if passed else "FAIL",
        "tests": tests,
    }


def activation_probe(root: Path) -> dict[str, object]:
    report = selftest(root=root)
    passed = report["result"] == "PASS"
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": "PASS" if passed else "FAIL",
        "authority": "DERIVED_OPERATIONAL_EVIDENCE",
        "binding_id": "",
        "proves_bindings": [],
        "normal_call_path_exercised": True,
        "source_status_semantics_verified": passed,
        "basis_files": [
            "mcp/boot-architecture-control.yaml",
            "standards/boot-critical-path-architecture.yaml",
            "standards/runtime/handboot.yaml",
            "modules/terminology/terms.yaml",
            "engines/interaction/rules.yaml",
            "tooling/runtime-host/boot_authority_selftest.py",
        ],
        "source_state_fingerprint": "",
        "selftest": report,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("selftest", "activation-probe"), default="selftest")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--bootengine-path", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    root = args.source_root.resolve()
    report = activation_probe(root) if args.command == "activation-probe" else selftest(root, args.bootengine_path)
    text = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
