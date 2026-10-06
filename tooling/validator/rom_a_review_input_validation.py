#!/usr/bin/env python3
"""Focused regression checks for the offline Rom A BK05 review-input producer."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
from typing import Any


PRODUCER_PATH = Path(__file__).resolve().parents[1] / "return-bridge" / "rom_a_review_input.py"
OLD_A4_REFS = ("RYG358", "RYG359")
NON_A4_REF = "RYG365"


def _load_producer():
    spec = importlib.util.spec_from_file_location("rom_a_review_input_under_test", PRODUCER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("PRODUCER_NOT_IMPORTABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _empty_evidence_gap(producer: Any, source_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rom-a-bk05-empty-") as temp:
        result = producer.prepare(Path(temp), source_root)
    _assert(result["state"] == "EXACT_INPUT_GAP", "empty evidence must return exact gap")
    _assert(result["gap"] == "PARENT_OR_CURRENTNESS_EVIDENCE_INCOMPLETE", "empty gap reason")
    _assert(result["authority"] == "NONE" and result["host_binding"] == "NONE", "no authority/binding")
    _assert("NO_CAPSULE" in result["limits"], "empty evidence cannot produce capsule")
    _assert(not result.get("capsule"), "empty evidence must not contain capsule")


def _same_actor_repair(producer: Any) -> None:
    source_delta = {"requested_actor_ref": "stale-actor", "review_ref": "A4-REAL-RETURN",
                    "typed_test_delta": {"added": ["current-head"]}}
    before = json.dumps(source_delta, sort_keys=True)
    repaired, repairs = producer._repair_same_actor(source_delta, "1BDEA0B3")
    _assert(json.dumps(source_delta, sort_keys=True) == before, "source delta must remain unchanged")
    _assert(repaired["requested_actor_ref"] == "1BDEA0B3", "actor must come from parent")
    _assert(repaired["review_ref"] == source_delta["review_ref"], "review ref must be retained")
    _assert(repaired["typed_test_delta"] == source_delta["typed_test_delta"], "typed delta must be retained")
    _assert(len(repairs) == 1 and repairs[0]["field"] == "requested_actor_ref", "repair must be recorded")


def _real_gap_without_a4(producer: Any, evidence_root: Path, source_root: Path) -> None:
    result = producer.prepare(evidence_root, source_root)
    _assert(result["state"] == "EXACT_INPUT_GAP", "missing A4 return must remain a gap")
    _assert(result["gap"] == "A4_NEW_HEAD_VERIFIER_BYTES_AND_TYPED_DELTA_REQUIRED", "A4 gap reason")
    _assert(result["missing_inputs"] == ["verifier.txt", "verifier_delta.json"], "exact missing inputs")
    _assert(result["parent_review"]["state"] == "OWNER_REVIEWED", "preserve supplied parent review")
    _assert(result["parent_review"]["assertion"] == "CALLER_ASSERTED_UNAUTHENTICATED",
            "do not overstate parent review")
    _assert(result["capture"]["semantic_review"] == "UNREVIEWED", "historical capture remains unreviewed")
    _assert(result["authority"] == "NONE" and result["host_binding"] == "NONE", "no authority/binding")
    _assert(not result.get("capsule"), "no capsule before current A4 review")
    labels = result["prior_verdicts_not_substitutes_for_current_a4_review"]
    _assert(all(ref in labels for ref in OLD_A4_REFS), "prior A4 refs must be explicitly excluded")
    _assert(NON_A4_REF in labels, "A1 adapter start must be explicitly excluded")
    bindings = result["source_binding"]["input_bindings"]
    for ref in OLD_A4_REFS + (NON_A4_REF,):
        _assert(ref not in bindings, "RYG rows are provenance labels, not verifier byte files")


def validate(evidence_root: Path, source_root: Path) -> dict[str, Any]:
    producer = _load_producer()
    _empty_evidence_gap(producer, source_root)
    _same_actor_repair(producer)
    _real_gap_without_a4(producer, evidence_root, source_root)
    return {"state": "PASS", "checks": ["empty_evidence_exact_gap", "same_actor_repair_preserves_source_delta",
                                          "current_a4_bytes_required", "no_capsule_or_host_binding"],
            "evidence_dir": str(evidence_root), "authority": "NONE", "host_binding": "NONE"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        result = validate(args.evidence_dir.resolve(), args.source_root.resolve())
        print(json.dumps(result, sort_keys=True))
        return 0
    except (AssertionError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"state": "FAIL", "reason": str(exc).split(":", 1)[0],
                          "authority": "NONE", "host_binding": "NONE"}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
