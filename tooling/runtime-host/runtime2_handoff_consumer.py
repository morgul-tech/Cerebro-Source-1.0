#!/usr/bin/env python3
"""Consume one validated Runtime2 handoff without mutating or publishing Source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_ROOT = SOURCE_ROOT / "tooling" / "validator"
if str(VALIDATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(VALIDATOR_ROOT))

from runtime2_human_execution_handoff import (  # noqa: E402
    Runtime2HandoffError,
    load_runtime2_context_binding,
    resolve_unique_cmd,
    validate_envelope,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def consume(envelope_path: Path, search_root: Path, *, execute: bool = True) -> tuple[dict[str, object], int]:
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    validation = validate_envelope(envelope, load_runtime2_context_binding(SOURCE_ROOT))
    target = resolve_unique_cmd(search_root, validation["cmd_sha256"])
    before = _hash(target)
    if before != validation["cmd_sha256"]:
        raise Runtime2HandoffError("runtime2-consumer-prelaunch-rehash-mismatch")
    exit_code = 0
    launched = False
    if execute:
        command_host = os.environ.get("COMSPEC") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
        completed = subprocess.run([command_host, "/d", "/c", str(target)], cwd=target.parent, check=False)
        exit_code = int(completed.returncode)
        launched = True
    after = _hash(target)
    if after != before:
        raise Runtime2HandoffError("runtime2-target-bytes-changed-during-consumption")
    return ({
        "schema": "cerebro-runtime2-handoff-consumption-receipt/v1",
        "result": "PASS" if exit_code == 0 else "TARGET_EXIT_NONZERO",
        "binding_id": validation["binding_id"],
        "handoff_fingerprint": validation["handoff_fingerprint"],
        "target_sha256": before,
        "target_path": str(target),
        "unique_cardinality_verified": True,
        "independent_prelaunch_rehash_verified": True,
        "target_bytes_preserved": after == before,
        "launched": launched,
        "exit_code": exit_code,
        "source_mutated_by_consumer": False,
        "runtime_mutated_outside_target_launch": False,
    }, exit_code)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelope", required=True)
    parser.add_argument("--search-root", required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        receipt, exit_code = consume(Path(args.envelope), Path(args.search_root), execute=not args.validate_only)
        print(json.dumps(receipt, indent=2, ensure_ascii=True))
        return exit_code
    except Exception as exc:
        print(json.dumps({"result": "BLOCK", "error": str(exc)}, indent=2, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
