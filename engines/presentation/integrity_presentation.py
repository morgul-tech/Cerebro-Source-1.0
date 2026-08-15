#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "cerebro-presentation-integrity/v1"
TRUTH_OWNER = False
LABELS = {
    "OBJECTIVE_ALIGNMENT": "Objective",
    "MCP_LOOP_INTEGRITY": "MCP loop",
    "WORK_POSITION": "Work position",
    "WORKFORM_ADEQUACY": "Workform",
    "BASIS_AND_PRIOR_KNOWLEDGE": "Basis / prior knowledge",
    "NEXT_GATE_READINESS": "Next gate",
}


def render(assessment: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(assessment, dict) or assessment.get("schema") != "cerebro-mcp-integrity-assessment/v1":
        raise ValueError("integrity-assessment-required")
    rows = []
    for item in assessment.get("dimensions", []):
        if not isinstance(item, dict):
            continue
        rows.append({
            "dimension": item.get("dimension"),
            "label": LABELS.get(str(item.get("dimension")), str(item.get("dimension"))),
            "result": item.get("result"),
            "status": item.get("status"),
            "sufficiency": item.get("sufficiency"),
            "reason": item.get("reason"),
        })
    headline = f"Integrity {assessment.get('result', 'UNKNOWN')}"
    if assessment.get("coverage_mode") == "FULL":
        headline = f"Integrity Full {assessment.get('result', 'UNKNOWN')}"
    if assessment.get("primary_scope") == "MCP_LOOP_INTEGRITY":
        headline = f"MCP-loop {assessment.get('result', 'UNKNOWN')}"
    return {
        "schema": SCHEMA,
        "authority": "PRESENTATION_ONLY",
        "truth_owner": TRUTH_OWNER,
        "assessment_ref": assessment.get("assessment_id"),
        "headline": headline,
        "overall": {
            "result": assessment.get("result"),
            "status": assessment.get("status"),
            "sufficiency": assessment.get("sufficiency"),
            "coverage_mode": assessment.get("coverage_mode"),
            "coverage_complete": assessment.get("coverage_complete"),
            "gate_profile": assessment.get("gate_profile"),
        },
        "dimensions": rows,
        "control_implications": assessment.get("control_implications"),
    }


def render_text(assessment: dict[str, Any]) -> str:
    view = render(assessment)
    lines = [view["headline"]]
    for row in view["dimensions"]:
        suffix = f" — {row['reason']}" if row.get("reason") else ""
        lines.append(f"{row['label']}: {row['result']} / {row['sufficiency']}{suffix}")
    implication = (view.get("control_implications") or {}).get("recommended_mcp_outcome")
    if implication:
        lines.append(f"MCP recommendation: {implication}")
    return "\n".join(lines)


def selftest() -> dict[str, Any]:
    sample = {
        "schema": "cerebro-mcp-integrity-assessment/v1",
        "assessment_id": "INTG-TEST",
        "result": "PASS",
        "status": "COMPLETE",
        "sufficiency": "COMPLETE",
        "coverage_mode": "ADAPTIVE",
        "coverage_complete": True,
        "primary_scope": "ALL",
        "gate_profile": None,
        "dimensions": [{"dimension": key, "result": "PASS", "status": "COMPLETE", "sufficiency": "COMPLETE", "reason": None} for key in LABELS],
        "control_implications": {"recommended_mcp_outcome": None},
    }
    view = render(sample)
    checks = {
        "presentation_only": view["authority"] == "PRESENTATION_ONLY" and view["truth_owner"] is False,
        "assessment_ref_preserved": view["assessment_ref"] == "INTG-TEST",
        "all_dimensions_rendered": len(view["dimensions"]) == 6,
        "text_does_not_change_truth": "Integrity PASS" in render_text(sample),
    }
    return {"schema": "cerebro-presentation-integrity-selftest/v1", "result": "PASS" if all(checks.values()) else "FAIL", "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Cerebro Integrity assessment")
    parser.add_argument("--assessment")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        result = selftest()
        print(json.dumps(result, indent=2))
        return 0 if result["result"] == "PASS" else 1
    if not args.assessment:
        parser.error("--assessment required")
    assessment = json.loads(Path(args.assessment).read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(render(assessment), indent=2, ensure_ascii=False))
    else:
        print(render_text(assessment))
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
