#!/usr/bin/env python3
"""Fail-closed static contract validation for Working Source currentization."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REQUIRED = (
    "symbolic-ref", "remote', 'get-url", "status', '--porcelain=v1'", "fetch', '--no-tags'",
    "merge-base', '--is-ancestor", "merge', '--ff-only'", "HOLD_LOCAL_AHEAD",
    "HOLD_DIVERGED_HISTORY", "HOLD_REQUALIFY", "PASS_NOOP", "PASS_FF_ONLY",
    "FileMode]::CreateNew", "payload_sha256", "cerebro-working-source-currentization-receipt/v1",
)
PROHIBITED = (
    r"\bgit\s+pull\b", r"\bgit\s+reset\b", r"\bgit\s+checkout\b", r"\bgit\s+switch\b",
    r"\bgit\s+rebase\b", r"\bgit\s+push\b", r"\bgit\s+clean\b", r"\bgit\s+stash\b",
    r"cerebro_sync\s+@", r"cerebro_sync\s+-",
)

def validate(root: Path) -> dict[str, object]:
    script = root / "tooling/delivery/Cerebro.WorkingSourceCurrentization.ps1"
    schema = root / "tooling/delivery/working-source-currentization-receipt.schema.json"
    errors: list[str] = []
    if not script.is_file(): errors.append(f"missing:{script}")
    if not schema.is_file(): errors.append(f"missing:{schema}")
    text = script.read_text(encoding="utf-8") if script.is_file() else ""
    for token in REQUIRED:
        if token not in text: errors.append(f"required-token-missing:{token}")
    for pattern in PROHIBITED:
        if re.search(pattern, text, re.IGNORECASE): errors.append(f"prohibited-pattern:{pattern}")
    try:
        doc = json.loads(schema.read_text(encoding="utf-8"))
        if doc.get("$schema") != "https://json-schema.org/draft/2020-12/schema": errors.append("schema-draft")
    except Exception as exc:
        errors.append(f"schema-json:{exc}")
    for counter in ("pull", "reset", "checkout", "switch", "rebase", "push", "source_write", "runtime_write", "attempt_epoch", "host_dispatch", "physical_effect", "human_git_courier"):
        if not re.search(rf"\b{re.escape(counter)}\s*=\s*0\b", text): errors.append(f"zero-counter-missing:{counter}")
    return {"schema": "cerebro-validator-result/v1", "check": "working_source_currentization", "pass": not errors, "errors": errors}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = validate(args.source_root.resolve())
    rendered = json.dumps(result, sort_keys=True, separators=(",", ":"))
    print(rendered)
    if args.json_output:
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["pass"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
