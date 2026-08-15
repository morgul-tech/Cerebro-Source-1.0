#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA = "cerebro-interaction-integrity-intent/v1"
AUTHORITY = "DERIVED_INTERACTION_ASSESSMENT"
ENTRYPOINTS = {
    "integrity": ("ADAPTIVE", "ALL"),
    "integrity full": ("FULL", "ALL"),
    "mcp-loop?": ("ADAPTIVE", "MCP_LOOP_INTEGRITY"),
    "mcp-loop": ("ADAPTIVE", "MCP_LOOP_INTEGRITY"),
}


def _normalize(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    # Canonical tokens remain literal; only case/spacing are normalized for command matching.
    return value.casefold()


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve(text: str) -> dict[str, Any]:
    normalized = _normalize(text)
    matched = ENTRYPOINTS.get(normalized)
    if matched is None:
        return {
            "schema": SCHEMA,
            "authority": AUTHORITY,
            "result": "NOT_APPLICABLE",
            "recognized": False,
            "raw_text": str(text or ""),
            "canonical_command": None,
            "coverage_mode": None,
            "primary_scope": None,
            "state_mutation_authority": False,
            "final_control_authority": False,
        }
    coverage_mode, primary_scope = matched
    canonical_command = (
        "Integrity Full" if normalized == "integrity full" else
        "MCP-loop?" if normalized in {"mcp-loop?", "mcp-loop"} else
        "Integrity"
    )
    subject = {
        "canonical_command": canonical_command,
        "coverage_mode": coverage_mode,
        "primary_scope": primary_scope,
    }
    return {
        "schema": SCHEMA,
        "assessment_id": "INTENT-INTG-" + _fingerprint(subject)[:16].upper(),
        "authority": AUTHORITY,
        "result": "PASS",
        "recognized": True,
        "raw_text": str(text or ""),
        "canonical_command": canonical_command,
        "coverage_mode": coverage_mode,
        "primary_scope": primary_scope,
        "route": "MCP_INTEGRITY_SUBRESOLUTION",
        "state_mutation_authority": False,
        "final_control_authority": False,
    }


def selftest() -> dict[str, Any]:
    checks = {
        "integrity_adaptive": resolve("Integrity")["coverage_mode"] == "ADAPTIVE",
        "integrity_full": resolve("  integrity   full ")["coverage_mode"] == "FULL",
        "mcp_loop_scope": resolve("MCP-loop?")["primary_scope"] == "MCP_LOOP_INTEGRITY",
        "manual_entrypoints_same_route": len({resolve(x).get("route") for x in ("Integrity", "Integrity Full", "MCP-loop?")}) == 1,
        "unknown_not_applicable": resolve("integrity status")["result"] == "NOT_APPLICABLE",
        "no_control_authority": all(resolve(x)["final_control_authority"] is False for x in ("Integrity", "Integrity Full", "MCP-loop?")),
    }
    return {"schema": "cerebro-interaction-integrity-intent-selftest/v1", "result": "PASS" if all(checks.values()) else "FAIL", "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve Cerebro Integrity manual entrypoint intent")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = selftest() if args.selftest else resolve(args.text or "")
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result.get("result") in {"PASS", "NOT_APPLICABLE"} else 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
