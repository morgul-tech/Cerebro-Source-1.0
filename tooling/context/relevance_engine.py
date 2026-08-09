#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SOURCE_ROOT = Path(__file__).resolve().parents[2]
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
STOP = {"and", "the", "for", "med", "som", "til", "from", "this", "that", "current"}


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object-required:{path}")
    return value


def terms(value: Any) -> set[str]:
    if isinstance(value, dict):
        value = " ".join(f"{k} {v}" for k, v in value.items())
    elif isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return {token.lower() for token in TOKEN.findall(str(value)) if token.lower() not in STOP}


def overlap(candidate: set[str], query: set[str]) -> int:
    return len(candidate & query)


def wisdom_records(doc: dict[str, Any]) -> list[dict[str, Any]]:
    records = doc.get("working_context", {}).get("records", [])
    return [item for item in records if isinstance(item, dict) and item.get("type") == "WISDOM_RECORD"]


def knowledge_records(doc: dict[str, Any]) -> list[dict[str, Any]]:
    records = doc.get("knowledge", {}).get("records", [])
    return [item for item in records if isinstance(item, dict)]


def knowledge_eligible(item: dict[str, Any]) -> tuple[bool, str]:
    if item.get("status") != "ACTIVE":
        return False, "status-not-ACTIVE"
    verification = item.get("verification", {})
    if verification.get("state") != "VERIFIED":
        return False, "not-VERIFIED"
    contradiction = item.get("contradiction", {})
    if contradiction.get("state") == "UNRESOLVED_MATERIAL":
        return False, "unresolved-material-contradiction"
    if item.get("validity", {}).get("state") in {"EXPIRED", "OUT_OF_SCOPE"}:
        return False, "validity-not-current"
    return True, "eligible"


def wisdom_eligible(item: dict[str, Any]) -> tuple[bool, str]:
    relations = item.get("relations", {})
    if relations.get("revoked_by_ref") or relations.get("superseded_by_ref"):
        return False, "revoked-or-superseded"
    return True, "eligible"


def rank(item: dict[str, Any], request: dict[str, Any], friction: set[str]) -> tuple[int, dict[str, int]]:
    objective = terms(request.get("current_objective", ""))
    scope = terms(request.get("current_scope", ""))
    tags = terms(request.get("tags", []))
    state = terms([request.get("current_failure_state", ""), request.get("current_decision_state", "")])
    candidate_scope = terms(item.get("scope", ""))
    candidate_body = terms([item.get("claim", ""), item.get("statement", ""), item.get("tags", []), item.get("payload", {})])
    dimensions = {
        "objective": min(overlap(candidate_body | candidate_scope, objective), 3) * 4,
        "scope": min(overlap(candidate_scope | candidate_body, scope), 3) * 3,
        "tags": min(overlap(candidate_body, tags), 3) * 3,
        "state": min(overlap(candidate_body, state), 3) * 2,
        "friction": 2 if item.get("id") in friction else 0,
    }
    return sum(dimensions.values()), dimensions


def material_insight(request: dict[str, Any]) -> bool:
    insight = request.get("material_user_insight")
    if isinstance(insight, dict):
        return bool(insight.get("material")) or bool(set(insight.get("signals", [])) & {
            "challenges-current-premise", "identifies-repeated-failure",
            "changes-objective-or-scope", "alleges-existing-learning-was-not-applied",
            "presents-new-decision-relevant-evidence",
        })
    return bool(insight)


def retrieve(request: dict[str, Any], root: Path = SOURCE_ROOT) -> dict[str, Any]:
    if not request.get("current_objective") or not request.get("current_scope"):
        raise ValueError("current_objective-and-current_scope-required")
    knowledge = load_yaml(root / "engines/context/knowledge.yaml")
    context = load_yaml(root / "engines/context/working-context.yaml")
    evidence = load_yaml(root / "engines/context/wisdom-evidence.yaml")
    friction = {
        item.get("subject_ref") for item in evidence.get("wisdom_evidence", {}).get("profiles", [])
        if item.get("friction_state") in {"REVIEW", "ESCALATE"}
    }
    accepted: dict[str, list[dict[str, Any]]] = {"knowledge": [], "wisdom": []}
    rejected: list[dict[str, str]] = []
    for kind, records, check in (
        ("knowledge", knowledge_records(knowledge), knowledge_eligible),
        ("wisdom", wisdom_records(context), wisdom_eligible),
    ):
        for item in records:
            eligible, reason = check(item)
            if not eligible:
                rejected.append({"ref": str(item.get("id")), "reason": reason})
                continue
            score, dimensions = rank(item, request, friction)
            if score < 3:
                rejected.append({"ref": str(item.get("id")), "reason": "below-relevance-threshold"})
                continue
            accepted[kind].append({"ref": item["id"], "score": score, "dimensions": dimensions})
        accepted[kind] = sorted(accepted[kind], key=lambda row: (-row["score"], row["ref"]))[:5]
    knowledge_refs = [item["ref"] for item in accepted["knowledge"]]
    wisdom_refs = [item["ref"] for item in accepted["wisdom"]]
    basis_refs = sorted(knowledge_refs + wisdom_refs)
    basis_value = {
        "objective_ref": request.get("objective_ref", request["current_objective"]),
        "scope": request["current_scope"], "basis_refs": basis_refs,
        "failure": request.get("current_failure_state"), "decision": request.get("current_decision_state"),
    }
    fingerprint = hashlib.sha256(json.dumps(basis_value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    checkpoint = material_insight(request)
    return {
        "schema": "cerebro-relevance-assessment/v0.1",
        "assessment_id": "REL-" + fingerprint[:16].upper(),
        "objective_ref": basis_value["objective_ref"],
        "applicable_knowledge_refs": knowledge_refs,
        "applicable_wisdom_refs": wisdom_refs,
        "ranking": accepted,
        "rejected_refs": sorted(rejected, key=lambda row: row["ref"]),
        "basis_refs": basis_refs,
        "basis_fingerprint": fingerprint,
        "evidence_authority": "EVIDENCE_ONLY",
        "human_insight_checkpoint": "CRITIQUE_AND_REASSESS" if checkpoint else "NONE",
        "next_control_event": "RE_RESOLVE_CONTROL" if checkpoint else "CONTROL_RESOLUTION",
    }


def feedback(value: dict[str, Any]) -> dict[str, Any]:
    required = {"objective_ref", "action_ref", "result", "verification_state", "evidence_refs"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("missing-feedback-fields:" + ",".join(missing))
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return {
        "schema": "cerebro-effect-feedback/v0.1", "feedback_id": "EFF-" + digest[:16].upper(),
        "authority": "EVIDENCE_ONLY", "captured_at": datetime.now(timezone.utc).isoformat(),
        "observation": value, "knowledge_admission": "REQUIRES_SEPARATE_VERIFICATION",
        "wisdom_promotion": "PROHIBITED_AUTOMATICALLY", "source_mutation": False,
        "next_control_event": "RETRIEVAL_REASSESSMENT",
    }


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []
    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "engines/context").mkdir(parents=True)
        (root / "engines/context/knowledge.yaml").write_text(yaml.safe_dump({"knowledge": {"records": [
            {"id": "K1", "claim": "PowerShell runner requires hash verification", "scope": "delivery runner", "tags": ["powershell"], "status": "ACTIVE", "verification": {"state": "VERIFIED"}, "contradiction": {"state": "NONE"}, "validity": {"state": "CURRENT"}},
            {"id": "K2", "claim": "unverified", "scope": "delivery", "status": "ACTIVE", "verification": {"state": "FAILED"}, "contradiction": {"state": "NONE"}},
        ]}}), encoding="utf-8")
        (root / "engines/context/working-context.yaml").write_text(yaml.safe_dump({"working_context": {"records": [
            {"id": "W1", "type": "WISDOM_RECORD", "scope": "delivery runner", "statement": "Use bounded backup and hash verification", "payload": {}, "relations": {}}
        ]}}), encoding="utf-8")
        (root / "engines/context/wisdom-evidence.yaml").write_text(yaml.safe_dump({"wisdom_evidence": {"profiles": []}}), encoding="utf-8")
        req = {"current_objective": "build PowerShell runner with hash verification", "current_scope": "delivery runner"}
        first = retrieve(req, root); second = retrieve(req, root)
        check("relevant-knowledge-retrieved", first["applicable_knowledge_refs"] == ["K1"])
        check("ineligible-knowledge-rejected", any(x["ref"] == "K2" for x in first["rejected_refs"]))
        check("relevant-wisdom-retrieved", first["applicable_wisdom_refs"] == ["W1"])
        check("basis-fingerprint-deterministic", first["basis_fingerprint"] == second["basis_fingerprint"])
        req["material_user_insight"] = {"signals": ["alleges-existing-learning-was-not-applied"]}
        check("material-insight-reresolves", retrieve(req, root)["next_control_event"] == "RE_RESOLVE_CONTROL")
        result = feedback({"objective_ref": "O", "action_ref": "A", "result": "PASS", "verification_state": "VERIFIED", "evidence_refs": ["E"]})
        check("feedback-remains-evidence-only", result["authority"] == "EVIDENCE_ONLY" and not result["source_mutation"])
    return {"schema": "cerebro-relevance-selftest/v0.1", "result": "PASS" if all(x["result"] == "PASS" for x in tests) else "FAIL", "tests": tests}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    retrieve_cmd = sub.add_parser("retrieve"); retrieve_cmd.add_argument("--request", required=True); retrieve_cmd.add_argument("--output")
    feedback_cmd = sub.add_parser("record-effect"); feedback_cmd.add_argument("--input", required=True); feedback_cmd.add_argument("--output", required=True)
    sub.add_parser("selftest")
    args = parser.parse_args()
    if args.command == "selftest":
        result = selftest()
    elif args.command == "retrieve":
        result = retrieve(json.loads(Path(args.request).read_text(encoding="utf-8")))
    else:
        result = feedback(json.loads(Path(args.input).read_text(encoding="utf-8")))
    text = json.dumps(result, indent=2) + "\n"
    output = getattr(args, "output", None)
    if output:
        Path(output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result.get("result", "PASS") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
