#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT_SOURCE = "github:morgul-tech/Cerebro-Source-1.0/main/cerebro.yaml"


def resolve(command: str, candidates: list[dict[str, str]], current_commit: str) -> dict[str, str]:
    if command.strip().lower() not in {"boot cerebro", "bootcerebro", "bootini"}:
        raise ValueError("BOOT_COMMAND_UNRECOGNIZED")
    source = next((item for item in candidates if item.get("identity") == CURRENT_SOURCE), None)
    if source is None:
        raise ValueError("AUTHORITATIVE_SOURCE_MISSING")
    if source.get("commit") != current_commit:
        raise ValueError("AUTHORITATIVE_SOURCE_COMMIT_MISMATCH")
    return {"canonical_command": "bootCerebro", "authority": CURRENT_SOURCE, "commit": current_commit}


def selftest(bootengine_path: Path | None = None) -> dict[str, object]:
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

    handboot = (ROOT / "standards/runtime/handboot.yaml").read_text(encoding="utf-8")
    source_authority = (ROOT / "standards/source-authority.yaml").read_text(encoding="utf-8")
    activation = (ROOT / "mcp/activation.yaml").read_text(encoding="utf-8")
    required = ["CURRENT-in-a-filename-confers-no-authority", "REJECT_INPUT_CONTINUE_CURRENT_SOURCE", "boot cerebro", "ACTIVE_CONTROL_TRANSFERRED"]
    combined = handboot + source_authority + activation
    check("source-contract-tokens", all(token in combined for token in required))
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
    return {"schema": "cerebro-boot-authority-selftest/v0.1", "result": "PASS" if passed else "FAIL", "tests": tests}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootengine-path", type=Path)
    args = parser.parse_args()
    report = selftest(args.bootengine_path)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["result"] == "PASS" else 1)
