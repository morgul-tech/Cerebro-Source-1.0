#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module-load-failed:{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command: str, request: dict[str, Any], root: Path = SOURCE_ROOT) -> dict[str, Any]:
    interaction = load_module(root / "engines/interaction/integrity_intent.py", "cerebro_integrity_intent")
    intent = interaction.resolve(command)
    if intent.get("recognized") is not True:
        raise ValueError("unrecognized-integrity-entrypoint")
    control = load_module(root / "mcp/control_resolution.py", "cerebro_integrity_canonical_control")
    payload = dict(request)
    payload["integrity_intent"] = intent
    payload["integrity_required"] = True
    result = control.resolve(payload, root)
    assessment = result.get("integrity_assessment")
    if not isinstance(assessment, dict):
        raise RuntimeError("canonical-control-did-not-consume-integrity")
    presentation = load_module(root / "engines/presentation/integrity_presentation.py", "cerebro_integrity_presentation")
    return {
        "schema": "cerebro-integrity-manual-entrypoint-result/v1",
        "authority": "DERIVED_ENTRYPOINT_RESULT",
        "canonical_mcp_path_exercised": True,
        "intent": intent,
        "mcp_control_decision": result.get("mcp_control_decision"),
        "integrity_assessment": assessment,
        "presentation": presentation.render(assessment),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Cerebro Integrity manual ADMIN entrypoint through canonical MCP")
    parser.add_argument("command", choices=["Integrity", "Integrity Full", "MCP-loop?"])
    parser.add_argument("--request", required=True)
    parser.add_argument("--source-root", default=str(SOURCE_ROOT))
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        result = run(args.command, request, Path(args.source_root).resolve())
        text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        # A FAIL Integrity assessment is a valid domain result, not CLI failure.
        return 0
    except Exception as exc:
        failure = {"result": "ERROR", "error": str(exc), "error_class": type(exc).__name__}
        text = json.dumps(failure, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
