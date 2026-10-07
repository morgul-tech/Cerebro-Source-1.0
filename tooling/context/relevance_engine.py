#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SOURCE_ROOT = Path(__file__).resolve().parents[2]
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}")
STOP = {"and", "the", "for", "med", "som", "til", "from", "this", "that", "current"}
MATERIAL_SIGNALS = {
    "challenges-current-premise",
    "identifies-repeated-failure",
    "changes-objective-or-scope",
    "alleges-existing-learning-was-not-applied",
    "presents-new-decision-relevant-evidence",
}


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"object-required:{path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(value: Any) -> str:
    if isinstance(value, dict):
        value = " ".join(f"{k} {v}" for k, v in sorted(value.items(), key=lambda item: str(item[0])))
    elif isinstance(value, (list, tuple, set)):
        value = " ".join(str(item) for item in value)
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[_/\\:;,.()\[\]{}]+", " ", text)
    text = re.sub(r"[-–—]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def terms(value: Any) -> set[str]:
    normalized = normalize_text(value)
    return {token for token in TOKEN.findall(normalized) if token not in STOP}


def overlap(candidate: set[str], query: set[str]) -> int:
    return len(candidate & query)


def all_wisdom_records(doc: dict[str, Any]) -> list[dict[str, Any]]:
    records = doc.get("working_context", {}).get("records", [])
    return [item for item in records if isinstance(item, dict) and item.get("type") == "WISDOM_RECORD"]


def current_wisdom_refs(doc: dict[str, Any]) -> set[str]:
    index = doc.get("working_context", {}).get("current_index")
    if not isinstance(index, dict):
        raise ValueError("working-context-current-index-required")
    refs = index.get("current_wisdom_refs")
    if not isinstance(refs, list):
        raise ValueError("working-context-current-wisdom-refs-required")
    return {str(ref) for ref in refs}


def knowledge_records(doc: dict[str, Any]) -> list[dict[str, Any]]:
    records = doc.get("knowledge", {}).get("records", [])
    return [item for item in records if isinstance(item, dict)]


def history_records(doc: dict[str, Any]) -> list[dict[str, Any]]:
    records = doc.get("development_history", {}).get("records", [])
    return [item for item in records if isinstance(item, dict)]


def knowledge_eligible(item: dict[str, Any]) -> tuple[bool, str]:
    if item.get("status") != "ACTIVE":
        return False, "status-not-ACTIVE"
    if item.get("verification", {}).get("state") != "VERIFIED":
        return False, "not-VERIFIED"
    if item.get("contradiction", {}).get("state") == "UNRESOLVED_MATERIAL":
        return False, "unresolved-material-contradiction"
    if item.get("validity", {}).get("state") in {"EXPIRED", "OUT_OF_SCOPE"}:
        return False, "validity-not-current"
    return True, "eligible"


def wisdom_eligible(item: dict[str, Any], current_refs: set[str]) -> tuple[bool, str]:
    item_id = str(item.get("id", ""))
    if item_id not in current_refs:
        return False, "not-current-wisdom"
    relations = item.get("relations", {}) if isinstance(item.get("relations"), dict) else {}
    if relations.get("revoked_by_ref") or relations.get("superseded_by_ref"):
        return False, "revoked-or-superseded"
    return True, "eligible"


def history_eligible(item: dict[str, Any]) -> tuple[bool, str]:
    if item.get("role") not in {"EVENT", "CORRECTION", "RETRACTION"}:
        return False, "invalid-history-role"
    return True, "eligible"


def rank(item: dict[str, Any], request: dict[str, Any], friction: set[str]) -> tuple[int, dict[str, int]]:
    objective = terms(request.get("current_objective", ""))
    scope = terms(request.get("current_scope", ""))
    tags = terms(request.get("tags", []))
    state = terms([request.get("current_failure_state", ""), request.get("current_decision_state", "")])
    candidate_scope = terms(item.get("scope", ""))
    candidate_body = terms([
        item.get("claim", ""), item.get("statement", ""), item.get("title", ""), item.get("fact", ""),
        item.get("event_class", ""), item.get("significance", ""), item.get("tags", []),
        item.get("payload", {}), item.get("impact", {}),
    ])
    dimensions = {
        "objective": min(overlap(candidate_body | candidate_scope, objective), 3) * 4,
        "scope": min(overlap(candidate_scope | candidate_body, scope), 3) * 3,
        "tags": min(overlap(candidate_body, tags), 3) * 3,
        "state": min(overlap(candidate_body, state), 3) * 2,
        "friction": 2 if str(item.get("id")) in friction else 0,
    }
    return sum(dimensions.values()), dimensions


def material_insight(request: dict[str, Any]) -> bool:
    insight = request.get("material_user_insight")
    if isinstance(insight, dict):
        return bool(insight.get("material")) or bool(set(insight.get("signals", [])) & MATERIAL_SIGNALS)
    return bool(insight)


def source_fingerprints(root: Path) -> dict[str, str]:
    paths = {
        "knowledge": root / "engines/context/knowledge.yaml",
        "working_context": root / "engines/context/working-context.yaml",
        "wisdom_evidence": root / "engines/context/wisdom-evidence.yaml",
        "development_history": root / "engines/context/development-history.yaml",
    }
    return {name: sha256_file(path) for name, path in paths.items()}


def select_bounded_content(read: dict[str, Any]) -> dict[str, Any]:
    """Project one current provider selection; never return unselected source parts.

    The caller owns provider authentication and may pass a prior selected view for
    same-revision reuse. This function creates no cache, authority, or truth store.
    """
    if not isinstance(read, dict):
        raise ValueError("bounded-read-object-required")
    required = ("content_ref", "revision", "authority_ref", "selector")
    if any(not isinstance(read.get(key), str) or not read[key].strip() for key in required):
        raise ValueError("bounded-read-identity-or-selector-missing")
    if read.get("currentness") != "CURRENT" or read.get("provider_readback_verified") is not True:
        raise ValueError("bounded-read-current-provider-readback-required")
    selector_kind = read.get("selector_kind")
    if selector_kind not in {"TAB", "SECTION", "RANGE"}:
        raise ValueError("bounded-read-selector-kind-invalid")
    if read.get("depth") == "DEEPER_READ" and not read.get("task_need_ref"):
        raise ValueError("bounded-read-deeper-task-need-required")
    provenance = read.get("provenance_refs")
    if not isinstance(provenance, list) or not provenance or any(
        not isinstance(ref, str) or not ref.strip() for ref in provenance
    ):
        raise ValueError("bounded-read-provenance-required")
    key = read["selector"]
    prior = read.get("prior_selection")
    same_prior = isinstance(prior, dict) and all(prior.get(field) == read.get(field)
        for field in ("content_ref", "revision", "selector", "selector_kind", "authority_ref", "provenance_refs"))
    parts = read.get("parts")
    if parts is None:
        if not same_prior or not isinstance(prior.get("selected_text"), str):
            raise ValueError("bounded-read-selected-content-or-exact-cache-required")
        selected = prior["selected_text"]
        mode = "SAME_REVISION_REUSE"
    else:
        if not isinstance(parts, dict) or key not in parts or not isinstance(parts[key], str):
            raise ValueError("bounded-read-selected-part-missing")
        selected = parts[key]
        mode = "SELECTED_PROVIDER_PART"
    if not selected.strip() or len(selected.encode("utf-8")) > 16_384:
        raise ValueError("bounded-read-selection-empty-or-too-large")
    digest = hashlib.sha256(selected.encode("utf-8")).hexdigest()
    if same_prior and prior.get("selected_sha256") != digest:
        raise ValueError("bounded-read-same-revision-content-conflict")
    delta = "NO_PRIOR_SELECTION"
    if isinstance(prior, dict) and prior.get("content_ref") == read["content_ref"]:
        delta = "UNCHANGED" if prior.get("selected_sha256") == digest else "MATERIAL_DELTA"
    return {"schema": "cerebro-bounded-content-selection/v1", "content_ref": read["content_ref"],
            "revision": read["revision"], "selector_kind": selector_kind, "selector": key,
            "selected_text": selected, "selected_sha256": digest, "selection_mode": mode,
            "delta": delta, "authority_ref": read["authority_ref"],
            "provenance_refs": list(provenance), "provider_readback_verified": True,
            "backend_part_count": len(parts) if isinstance(parts, dict) else 0,
            "model_part_count": 1}


def retrieve(request: dict[str, Any], root: Path = SOURCE_ROOT) -> dict[str, Any]:
    if not request.get("current_objective") or not request.get("current_scope"):
        raise ValueError("current_objective-and-current_scope-required")

    knowledge = load_yaml(root / "engines/context/knowledge.yaml")
    context = load_yaml(root / "engines/context/working-context.yaml")
    evidence = load_yaml(root / "engines/context/wisdom-evidence.yaml")
    history = load_yaml(root / "engines/context/development-history.yaml")
    history_ledger = history.get("development_history")
    history_active = isinstance(history_ledger, dict) and history_ledger.get("lifecycle") == "ACTIVE"
    current_refs = current_wisdom_refs(context)
    fingerprints = source_fingerprints(root)

    friction = {
        str(item.get("subject_ref"))
        for item in evidence.get("wisdom_evidence", {}).get("profiles", [])
        if isinstance(item, dict) and item.get("friction_state") in {"REVIEW", "ESCALATE"}
    }

    accepted: dict[str, list[dict[str, Any]]] = {"knowledge": [], "wisdom": [], "history": []}
    rejected: list[dict[str, str]] = []

    for item in knowledge_records(knowledge):
        eligible, reason = knowledge_eligible(item)
        if not eligible:
            rejected.append({"ref": str(item.get("id")), "reason": reason})
            continue
        score, dimensions = rank(item, request, friction)
        if score < 3:
            rejected.append({"ref": str(item.get("id")), "reason": "below-relevance-threshold"})
            continue
        accepted["knowledge"].append({"ref": str(item["id"]), "score": score, "dimensions": dimensions})

    for item in all_wisdom_records(context):
        eligible, reason = wisdom_eligible(item, current_refs)
        if not eligible:
            rejected.append({"ref": str(item.get("id")), "reason": reason})
            continue
        score, dimensions = rank(item, request, friction)
        if score < 3:
            rejected.append({"ref": str(item.get("id")), "reason": "below-relevance-threshold"})
            continue
        accepted["wisdom"].append({"ref": str(item["id"]), "score": score, "dimensions": dimensions})

    if history_active:
        for item in history_records(history):
            eligible, reason = history_eligible(item)
            if not eligible:
                rejected.append({"ref": str(item.get("id")), "reason": reason})
                continue
            score, dimensions = rank(item, request, friction)
            if score < 3:
                rejected.append({"ref": str(item.get("id")), "reason": "below-relevance-threshold"})
                continue
            accepted["history"].append({"ref": str(item["id"]), "score": score, "dimensions": dimensions})
    else:
        rejected.append({"ref": "engines/context/development-history.yaml", "reason": "ledger-lifecycle-not-ACTIVE"})

    for kind, limit in (("knowledge", 5), ("wisdom", 5), ("history", 3)):
        accepted[kind] = sorted(accepted[kind], key=lambda row: (-row["score"], row["ref"]))[:limit]

    knowledge_refs = [item["ref"] for item in accepted["knowledge"]]
    wisdom_refs = [item["ref"] for item in accepted["wisdom"]]
    history_refs = [item["ref"] for item in accepted["history"]]
    basis_refs = sorted(knowledge_refs + wisdom_refs + history_refs)

    expected_prior_learning = bool(request.get("expected_prior_learning"))
    coverage_audit_complete = bool(request.get("coverage_audit_complete"))
    coverage_audit_refs = sorted(str(value) for value in request.get("coverage_audit_refs", []))
    coverage_state = "COMPLETE" if history_active else "FAILED"
    coverage_reason = "NORMAL_RETRIEVAL_COMPLETE" if history_active else "DEVELOPMENT_HISTORY_LEDGER_NOT_ACTIVE"
    if history_active and expected_prior_learning and not basis_refs and not coverage_audit_complete:
        coverage_state = "INCOMPLETE"
        coverage_reason = "EXPECTED_PRIOR_LEARNING_MISSING_NORMAL_RESULT"
    elif history_active and expected_prior_learning and not basis_refs and coverage_audit_complete:
        coverage_reason = "BOUNDED_COVERAGE_AUDIT_COMPLETE"

    basis_value = {
        "objective_ref": request.get("objective_ref", request["current_objective"]),
        "objective": normalize_text(request["current_objective"]),
        "scope": normalize_text(request["current_scope"]),
        "failure": normalize_text(request.get("current_failure_state", "")),
        "decision": normalize_text(request.get("current_decision_state", "")),
        "basis_refs": basis_refs,
        "source_fingerprints": fingerprints,
        "current_wisdom_refs": sorted(current_refs),
        "coverage_state": coverage_state,
        "coverage_audit_refs": coverage_audit_refs,
    }
    fingerprint = hashlib.sha256(
        json.dumps(basis_value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    checkpoint = material_insight(request)

    if not history_active or checkpoint:
        next_event = "RE_RESOLVE_CONTROL"
    elif coverage_state != "COMPLETE":
        next_event = "COVERAGE_AUDIT_REQUIRED"
    else:
        next_event = "CONTROL_RESOLUTION"

    return {
        "schema": "cerebro-relevance-assessment/v0.3",
        "assessment_id": "REL-" + fingerprint[:16].upper(),
        "objective_ref": basis_value["objective_ref"],
        "semantic_normalization": "DETERMINISTIC_SURFACE_ONLY_NO_AUTHORITY",
        "retrieval_state": "COMPLETE" if history_active else "FAILED",
        "coverage_state": coverage_state,
        "coverage_reason": coverage_reason,
        "expected_prior_learning": expected_prior_learning,
        "no_relevant_prior_learning": history_active and coverage_state == "COMPLETE" and not basis_refs,
        "current_wisdom_boundary": "working_context.current_index.current_wisdom_refs",
        "applicable_knowledge_refs": knowledge_refs,
        "applicable_wisdom_refs": wisdom_refs,
        "applicable_history_refs": history_refs,
        "ranking": accepted,
        "rejected_refs": sorted(rejected, key=lambda row: (row["ref"], row["reason"])),
        "basis_refs": basis_refs,
        "basis_fingerprint": fingerprint,
        "source_fingerprints": fingerprints,
        "evidence_authority": "EVIDENCE_ONLY",
        "human_insight_checkpoint": "CRITIQUE_AND_REASSESS" if checkpoint else "NONE",
        "next_control_event": next_event,
    }


def feedback(value: dict[str, Any]) -> dict[str, Any]:
    required = {"objective_ref", "action_ref", "result", "verification_state", "evidence_refs"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("missing-feedback-fields:" + ",".join(missing))
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "schema": "cerebro-effect-feedback/v0.2",
        "feedback_id": "EFF-" + digest[:16].upper(),
        "authority": "EVIDENCE_ONLY",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "observation": value,
        "knowledge_admission": "REQUIRES_SEPARATE_VERIFICATION",
        "wisdom_promotion": "PROHIBITED_AUTOMATICALLY",
        "source_mutation": False,
        "next_control_event": "RETRIEVAL_REASSESSMENT",
    }


def _fixture(root: Path) -> None:
    (root / "engines/context").mkdir(parents=True, exist_ok=True)
    knowledge = {"knowledge": {"records": [
        {"id": "K1", "claim": "PowerShell runner requires hash verification", "scope": "delivery runner", "tags": ["powershell"], "status": "ACTIVE", "verification": {"state": "VERIFIED"}, "contradiction": {"state": "NONE"}, "validity": {"state": "CURRENT"}},
        {"id": "K2", "claim": "PowerShell runner old unverified rule", "scope": "delivery runner", "status": "ACTIVE", "verification": {"state": "FAILED"}, "contradiction": {"state": "NONE"}},
    ]}}
    context = {"working_context": {
        "records": [
            {"id": "W1", "type": "WISDOM_RECORD", "scope": "delivery runner", "statement": "Use bounded backup and hash verification", "payload": {}, "relations": {}},
            {"id": "WOLD", "type": "WISDOM_RECORD", "scope": "delivery runner", "statement": "PowerShell runner hash verification obsolete historical guidance", "payload": {}, "relations": {}},
            {"id": "WREV", "type": "WISDOM_RECORD", "scope": "delivery runner", "statement": "PowerShell runner hash verification revoked guidance", "payload": {}, "relations": {"revoked_by_ref": "WNEW"}},
        ],
        "current_index": {"current_wisdom_refs": ["W1", "WREV"]},
    }}
    evidence = {"wisdom_evidence": {"profiles": [{"subject_ref": "W1", "friction_state": "NORMAL"}]}}
    history = {"development_history": {"lifecycle": "ACTIVE", "records": [
        {"id": "H1", "event_class": "LEARNING_EVENT", "significance": "MAJOR", "role": "EVENT", "title": "PowerShell delivery learning", "fact": "PowerShell runner hash verification prevented repeat delivery failure", "impact": {}, "relations": {}, "provenance": {}}
    ]}}
    for name, value in (("knowledge.yaml", knowledge), ("working-context.yaml", context), ("wisdom-evidence.yaml", evidence), ("development-history.yaml", history)):
        (root / "engines/context" / name).write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def selftest() -> dict[str, Any]:
    tests: list[dict[str, str]] = []
    def check(name: str, ok: bool) -> None:
        tests.append({"name": name, "result": "PASS" if ok else "FAIL"})
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); _fixture(root)
        req = {"current_objective": "build PowerShell runner with hash verification", "current_scope": "delivery runner"}
        first = retrieve(req, root); second = retrieve(req, root)
        check("eligible-relevant-knowledge-retrieved", first["applicable_knowledge_refs"] == ["K1"])
        check("failed-knowledge-rejected", any(x["ref"] == "K2" for x in first["rejected_refs"]))
        check("current-wisdom-retrieved", first["applicable_wisdom_refs"] == ["W1"])
        check("non-current-wisdom-rejected", any(x["ref"] == "WOLD" and x["reason"] == "not-current-wisdom" for x in first["rejected_refs"]))
        check("revoked-current-wisdom-rejected", any(x["ref"] == "WREV" and x["reason"] == "revoked-or-superseded" for x in first["rejected_refs"]))
        check("relevant-history-retrieved", first["applicable_history_refs"] == ["H1"])
        history_path = root / "engines/context/development-history.yaml"
        inactive = load_yaml(history_path)
        inactive["development_history"]["lifecycle"] = "INACTIVE"
        history_path.write_text(yaml.safe_dump(inactive, sort_keys=False), encoding="utf-8")
        failed = retrieve({**req, "expected_prior_learning": True, "coverage_audit_complete": True,
                           "coverage_audit_refs": ["AUDIT-1"]}, root)
        check("inactive-history-ledger-fails-closed-despite-other-results-and-audit-claim",
              failed["retrieval_state"] == "FAILED" and failed["coverage_state"] == "FAILED"
              and failed["applicable_history_refs"] == [] and not failed["no_relevant_prior_learning"]
              and failed["next_control_event"] == "RE_RESOLVE_CONTROL")
        del inactive["development_history"]["lifecycle"]
        history_path.write_text(yaml.safe_dump(inactive, sort_keys=False), encoding="utf-8")
        missing_lifecycle = retrieve(req, root)
        check("missing-history-ledger-lifecycle-fails-closed",
              missing_lifecycle["retrieval_state"] == "FAILED"
              and missing_lifecycle["coverage_state"] == "FAILED")
        _fixture(root)
        check("basis-fingerprint-deterministic", first["basis_fingerprint"] == second["basis_fingerprint"])
        missing = retrieve({"current_objective": "unmatched subject", "current_scope": "unmatched scope", "expected_prior_learning": True}, root)
        check("expected-prior-learning-empty-result-incomplete", missing["coverage_state"] == "INCOMPLETE" and not missing["no_relevant_prior_learning"])
        audited = retrieve({"current_objective": "unmatched subject", "current_scope": "unmatched scope", "expected_prior_learning": True, "coverage_audit_complete": True, "coverage_audit_refs": ["AUDIT-1"]}, root)
        check("bounded-coverage-audit-can-complete", audited["coverage_state"] == "COMPLETE" and audited["no_relevant_prior_learning"])
        req2 = dict(req); req2["material_user_insight"] = {"signals": ["alleges-existing-learning-was-not-applied"]}
        check("material-insight-reresolves", retrieve(req2, root)["next_control_event"] == "RE_RESOLVE_CONTROL")
        result = feedback({"objective_ref": "O", "action_ref": "A", "result": "PASS", "verification_state": "VERIFIED", "evidence_refs": ["E"]})
        check("feedback-remains-evidence-only", result["authority"] == "EVIDENCE_ONLY" and not result["source_mutation"])
    bounded = {"content_ref": "doc:1", "revision": "r1", "authority_ref": "source:1",
               "selector_kind": "TAB", "selector": "current", "currentness": "CURRENT",
               "provider_readback_verified": True, "provenance_refs": ["provider:1"],
               "parts": {"current": "Relevant current fact", "history": "Unselected private history"}}
    selected = select_bounded_content(bounded)
    check("ordinary-read-projects-only-selected-part",
          selected["selected_text"] == "Relevant current fact" and
          "Unselected private history" not in str(selected) and selected["model_part_count"] == 1)
    reused = select_bounded_content({**bounded, "parts": None, "prior_selection": selected})
    check("same-revision-exact-selection-reused", reused["selection_mode"] == "SAME_REVISION_REUSE")
    changed = select_bounded_content({**bounded, "revision": "r2", "parts": {"current": "Changed fact"},
                                      "prior_selection": selected})
    check("changed-revision-relevant-delta", changed["delta"] == "MATERIAL_DELTA")
    for name, bad in (
        ("stale-provider-readback-refused", {**bounded, "currentness": "STALE"}),
        ("missing-selector-refused", {**bounded, "selector": "missing"}),
        ("unjustified-deeper-read-refused", {**bounded, "depth": "DEEPER_READ"}),
        ("changed-authority-cache-refused", {**bounded, "parts": None,
                                             "authority_ref": "source:other", "prior_selection": selected}),
        ("same-revision-content-conflict-refused", {**bounded,
                                                     "parts": {"current": "Changed fact"},
                                                     "prior_selection": selected}),
    ):
        try:
            select_bounded_content(bad)
            check(name, False)
        except ValueError:
            check(name, True)
    deeper = select_bounded_content({**bounded, "selector_kind": "SECTION", "selector": "history",
                                     "depth": "DEEPER_READ", "task_need_ref": "task:verified-gap"})
    check("task-justified-deeper-read-targets-one-section",
          deeper["selected_text"] == "Unselected private history" and deeper["model_part_count"] == 1)
    return {"schema": "cerebro-relevance-selftest/v0.3", "result": "PASS" if all(x["result"] == "PASS" for x in tests) else "FAIL", "tests": tests}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    retrieve_cmd = sub.add_parser("retrieve")
    retrieve_cmd.add_argument("--request", required=True)
    retrieve_cmd.add_argument("--output")
    feedback_cmd = sub.add_parser("record-effect")
    feedback_cmd.add_argument("--input", required=True)
    feedback_cmd.add_argument("--output", required=True)
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
