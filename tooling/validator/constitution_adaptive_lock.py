#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


CONSTITUTION_PATH = "mcp/constitution.yaml"
HUMAN_PATH = "mcp/constitution.md"
MODEL_PATH = "standards/development/adaptive-analysis-model.yaml"
DECISION_PATH = "history/CONSTITUTION_ADAPTIVE_ANALYSIS_LOCK_20260812.yaml"
LEGACY_PATHS = (
    "mcp/identity.yaml",
    "mcp/authority.yaml",
    "mcp/architecture.yaml",
    "mcp/priorities.yaml",
    "mcp/activation.yaml",
    "mcp/boot-architecture-control.yaml",
    "engines/presentation/rules.yaml",
    "engines/interaction/rules.yaml",
)
ARTICLE_IDS = tuple(f"C-{index:02d}" for index in range(1, 9))
LEGACY_IDS = (
    "MCP-001", "MCP-002", "MCP-003", "MCP-004",
    "MCP-010", "MCP-011", "MCP-012", "MCP-013", "MCP-014", "MCP-015",
    "MCP-020", "MCP-021", "MCP-022", "MCP-023", "MCP-024", "MCP-025",
    "MCP-030", "MCP-031", "MCP-032", "MCP-033", "MCP-034",
    "MCP-040", "MCP-041", "MCP-042", "MCP-043", "MCP-044", "MCP-045",
    "MCP-046", "MCP-047", "MCP-048", "MCP-049",
    "MCP-060", "MCP-061", "MCP-062", "MCP-063", "MCP-064", "MCP-065", "MCP-066",
    "PRE-023", "INT-HCS-001",
)
LENSES = ("SYSTEM_GOVERNANCE", "EXECUTION", "HUMAN_ALIGNMENT", "OUTCOME_FIDELITY")
SESSION_DEPTHS = ("NONE", "LIGHT", "STANDARD", "DEEP")
BINDING_ID = "CONSTITUTION_AND_ADAPTIVE_ANALYSIS_LOCK"
ACTIVATION_SCHEMA = "cerebro-constitution-adaptive-analysis-activation-proof/v1"
BASIS_FILES = (
    CONSTITUTION_PATH,
    HUMAN_PATH,
    MODEL_PATH,
    DECISION_PATH,
    *LEGACY_PATHS,
    "cerebro.yaml",
    "mcp/manifest.yaml",
    "mcp/control-resolution.yaml",
    "mcp/control_resolution.py",
    "standards/standards.yaml",
    "standards/rule-model.yaml",
    "standards/development/adaptive-quality-workform.yaml",
    "standards/development/reflective-adjustment-loop.yaml",
    "standards/development/implementation-learning-loop.yaml",
    "tooling/validator/checks.yaml",
    "tooling/validator/contract-activation-bindings.json",
    "tooling/validator/constitution_adaptive_lock.py",
)


class LockValidationError(ValueError):
    pass


def read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LockValidationError(f"yaml-object-required:{path}")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_text(value: Any) -> str:
    return " ".join(str(value).split())


def semantic_subject(constitution: dict[str, Any]) -> dict[str, Any]:
    articles = constitution.get("articles")
    if not isinstance(articles, list):
        raise LockValidationError("constitution-articles-list-required")
    return {
        "id": constitution.get("id"),
        "version": str(constitution.get("version")),
        "articles": [
            {
                "id": article.get("id"),
                "title": article.get("title"),
                "machine_statement": normalize_text(article.get("machine_statement")),
                "human_statement": normalize_text(article.get("human_statement")),
                "protected_invariants": article.get("protected_invariants"),
            }
            for article in articles
            if isinstance(article, dict)
        ],
        "breach_protocol": constitution.get("breach_protocol"),
        "amendment_protocol": constitution.get("amendment_protocol"),
    }


def semantic_fingerprint(constitution: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(semantic_subject(constitution)).encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LockValidationError(message)


def validate_constitution_document(document: dict[str, Any]) -> dict[str, Any]:
    _require(document.get("schema") == "cerebro-constitution/v1", "constitution-schema-mismatch")
    constitution = document.get("constitution")
    _require(isinstance(constitution, dict), "constitution-object-required")
    _require(constitution.get("id") == "CEREBRO-CONSTITUTION-001", "constitution-id-mismatch")
    _require(str(constitution.get("version")) == "1.0.0", "constitution-version-mismatch")
    _require(constitution.get("status") == "LOCKED", "constitution-not-locked")
    articles = constitution.get("articles")
    _require(isinstance(articles, list), "constitution-articles-required")
    ids = tuple(str(item.get("id")) for item in articles if isinstance(item, dict))
    _require(ids == ARTICLE_IDS, "constitution-article-identity-or-order-mismatch")
    _require(len(set(ids)) == len(ids), "constitution-article-id-duplicate")
    for article in articles:
        _require(bool(normalize_text(article.get("machine_statement"))), f"machine-statement-missing:{article.get('id')}")
        _require(bool(normalize_text(article.get("human_statement"))), f"human-statement-missing:{article.get('id')}")
        invariants = article.get("protected_invariants")
        _require(isinstance(invariants, list) and len(invariants) >= 3, f"protected-invariants-insufficient:{article.get('id')}")
    actual = semantic_fingerprint(constitution)
    _require(constitution.get("semantic_fingerprint") == actual, "constitution-semantic-fingerprint-mismatch")
    representation = constitution.get("representation_contract") or {}
    _require(representation.get("machine_representation_is_canonical") is True, "machine-representation-not-canonical")
    _require(representation.get("parallel_constitutional_authority") == "PROHIBITED", "parallel-constitution-not-prohibited")
    breach = constitution.get("breach_protocol") or {}
    _require(breach.get("candidate_event") == "CONSTITUTIONAL_BREACH_CANDIDATE", "breach-candidate-event-mismatch")
    _require(breach.get("verified_material_effect") == "BLOCK_AFFECTED_ACTION", "verified-material-breach-not-blocking")
    _require(breach.get("automatic_constitution_amendment") is False, "automatic-constitution-amendment-not-prohibited")
    amendment = constitution.get("amendment_protocol") or {}
    _require(amendment.get("decision_owner") == "ADMIN", "constitution-amendment-owner-mismatch")
    _require(amendment.get("automatic_or_threshold_amendment") == "PROHIBITED", "threshold-amendment-not-prohibited")
    mappings = (constitution.get("legacy_consolidation") or {}).get("mappings")
    _require(isinstance(mappings, dict), "legacy-consolidation-mappings-required")
    _require(tuple(sorted(mappings)) == tuple(sorted(LEGACY_IDS)), "legacy-consolidation-scope-mismatch")
    for rule_id, disposition in mappings.items():
        _require(isinstance(disposition, dict), f"legacy-disposition-invalid:{rule_id}")
        _require(disposition.get("article") in ARTICLE_IDS, f"legacy-article-invalid:{rule_id}")
        _require(disposition.get("disposition") in {"RECLASSIFIED", "REPLACED_AND_RECLASSIFIED"}, f"legacy-disposition-unknown:{rule_id}")
    return {"semantic_fingerprint": actual, "article_count": len(articles), "legacy_mapping_count": len(mappings)}


def validate_human_text(text: str, constitution: dict[str, Any]) -> dict[str, Any]:
    _require(f"Constitution-ID: `{constitution['id']}`" in text, "human-constitution-id-mismatch")
    _require(f"Version: `{constitution['version']}`" in text, "human-constitution-version-mismatch")
    _require(f"Status: `{constitution['status']}`" in text, "human-constitution-status-mismatch")
    _require(f"Semantic fingerprint: `{constitution['semantic_fingerprint']}`" in text, "human-constitution-fingerprint-mismatch")
    headings = re.findall(r"^## (C-\d{2})\s+—", text, flags=re.MULTILINE)
    _require(tuple(headings) == ARTICLE_IDS, "human-article-identity-or-order-mismatch")
    normalized_human = normalize_text(text)
    for article in constitution["articles"]:
        statement = normalize_text(article["human_statement"])
        _require(normalized_human.count(statement) == 1, f"human-statement-parity-mismatch:{article['id']}")
    return {"human_article_count": len(headings), "semantic_parity": True}


def validate_human_parity(root: Path, constitution: dict[str, Any]) -> dict[str, Any]:
    return validate_human_text((root / HUMAN_PATH).read_text(encoding="utf-8"), constitution)


def validate_no_parallel_constitutional_authority(text_by_path: dict[str, str]) -> None:
    constitutional_left = [
        relative
        for relative, text in text_by_path.items()
        if re.search(r"(?m)^\s*authority:\s*[\"']?constitutional[\"']?\s*$", text, flags=re.IGNORECASE)
    ]
    _require(not constitutional_left, "parallel-constitutional-rules-remain:" + ",".join(sorted(constitutional_left)))


def _iter_rule_objects(value: Any):
    if isinstance(value, dict):
        if "id" in value and "authority" in value:
            yield value
        for child in value.values():
            yield from _iter_rule_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_rule_objects(child)


def validate_legacy_consolidation(root: Path) -> dict[str, Any]:
    found: dict[str, dict[str, Any]] = {}
    for relative in LEGACY_PATHS:
        document = read_yaml(root / relative)
        for rule in _iter_rule_objects(document):
            rule_id = str(rule.get("id"))
            if rule_id not in LEGACY_IDS:
                continue
            if rule_id in found:
                raise LockValidationError(f"legacy-rule-duplicate:{rule_id}")
            found[rule_id] = rule
    _require(tuple(sorted(found)) == tuple(sorted(LEGACY_IDS)), "legacy-rule-inventory-mismatch")
    active_yaml: dict[str, str] = {}
    for path in root.rglob("*.yaml"):
        if "history" in path.relative_to(root).parts:
            continue
        active_yaml[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    validate_no_parallel_constitutional_authority(active_yaml)
    _require(all(str(found[rule_id].get("authority")).lower() == "normative" for rule_id in LEGACY_IDS), "legacy-rule-not-normative")
    _require("Source-status og lokal Runtime-verifikasjon" in str(found["MCP-049"].get("statement")), "mcp-049-status-separation-not-installed")
    return {"legacy_rule_count": len(found), "parallel_constitutional_rule_count": 0}


def validate_model_document(document: dict[str, Any]) -> dict[str, Any]:
    _require(document.get("schema") == "cerebro-adaptive-analysis-model/v1", "adaptive-model-schema-mismatch")
    model = document.get("adaptive_analysis_model")
    _require(isinstance(model, dict), "adaptive-model-object-required")
    _require(model.get("id") == "CEREBRO-ADAPTIVE-ANALYSIS-MODEL-001", "adaptive-model-id-mismatch")
    _require(str(model.get("version")) == "1.0.0", "adaptive-model-version-mismatch")
    _require(model.get("status") == "LOCKED", "adaptive-model-not-locked")
    architecture = model.get("architecture") or {}
    _require(architecture.get("new_top_level_component") is False, "adaptive-model-created-top-level-component")
    _require(architecture.get("parallel_learning_engine") == "PROHIBITED", "parallel-learning-engine-not-prohibited")
    lenses = (model.get("analysis_lenses") or {}).get("values")
    _require(isinstance(lenses, dict) and tuple(lenses) == LENSES, "adaptive-model-lenses-mismatch")
    depths = (model.get("session_review_depth") or {}).get("values")
    _require(tuple(depths or ()) == SESSION_DEPTHS, "adaptive-model-depths-mismatch")
    depth_rules = (model.get("depth_resolution") or {}).get("rules") or []
    for token in (
        "select-minimum-sufficient-depth",
        "LIGHT-may-escalate-immediately-on-material-discovery",
        "constitutional-breach-candidate-is-DEEP",
        "suspected-breach-does-not-block-until-verified-and-material",
    ):
        _require(token in depth_rules, f"adaptive-depth-rule-missing:{token}")
    cross_session = model.get("cross_session_learning") or {}
    _require(cross_session.get("default_synthesis_window") == 3, "adaptive-synthesis-window-mismatch")
    _require(cross_session.get("default_synthesis_window_is_configuration_not_constitution") is True, "adaptive-window-frozen-as-constitution")
    _require(cross_session.get("material_trigger_may_synthesize_early") is True, "adaptive-early-synthesis-missing")
    governance = model.get("learning_and_governance") or {}
    for key in ("automatic_rule_creation", "automatic_control_promotion", "automatic_source_mutation", "automatic_constitution_amendment"):
        _require(governance.get(key) is False, f"adaptive-automatic-authority-not-prohibited:{key}")
    _require(governance.get("counterevidence_required") is True, "adaptive-counterevidence-not-required")
    _require(governance.get("lesson_requires_later_effect_check") is True, "adaptive-effect-check-not-required")
    lock = model.get("lock") or {}
    _require(lock.get("decision") == "LOCK", "adaptive-lock-decision-missing")
    _require(len(lock.get("locked_elements") or []) >= 8, "adaptive-locked-elements-insufficient")
    return {"lens_count": len(lenses), "session_depth_count": len(depths), "synthesis_window": 3}


def validate_registration(root: Path) -> dict[str, Any]:
    required_tokens = {
        "cerebro.yaml": ("constitution: mcp/constitution.yaml", "adaptive_analysis_model: standards/development/adaptive-analysis-model.yaml"),
        "mcp/manifest.yaml": ("mcp/constitution.yaml", "standards/development/adaptive-analysis-model.yaml", "CONSTITUTION_AND_ADAPTIVE_ANALYSIS_LOCK"),
        "mcp/control-resolution.yaml": ("constitutional_control:", "CONSTITUTIONAL_COMPLIANCE_PRECEDES_ADAPTIVE_EXECUTION"),
        "mcp/control_resolution.py": ("evaluate_constitutional_compliance", "VERIFIED_MATERIAL_BREACH"),
        "standards/standards.yaml": ("STD-ADAPTIVE-ANALYSIS-MODEL", "standards/development/adaptive-analysis-model.yaml"),
        "standards/rule-model.yaml": ("canonical_constitution: mcp/constitution.yaml", "parallel_constitutional_authority: PROHIBITED"),
        "tooling/validator/checks.yaml": ("constitution_and_adaptive_analysis_lock:",),
        "tooling/validator/contract-activation-bindings.json": (BINDING_ID,),
    }
    for relative, tokens in required_tokens.items():
        text = (root / relative).read_text(encoding="utf-8")
        for token in tokens:
            _require(token in text, f"registration-token-missing:{relative}:{token}")
    decision = read_yaml(root / DECISION_PATH).get("decision") or {}
    _require(decision.get("status") == "LOCKED", "governance-decision-not-locked")
    _require(decision.get("decision_owner") == "ADMIN", "governance-decision-owner-mismatch")
    return {"registration_files": len(required_tokens), "decision_record_locked": True}


def validate_root(root: Path) -> dict[str, Any]:
    constitution_document = read_yaml(root / CONSTITUTION_PATH)
    constitution_result = validate_constitution_document(constitution_document)
    constitution = constitution_document["constitution"]
    human_result = validate_human_parity(root, constitution)
    legacy_result = validate_legacy_consolidation(root)
    model_result = validate_model_document(read_yaml(root / MODEL_PATH))
    registration_result = validate_registration(root)
    return {
        "schema": "cerebro-constitution-adaptive-analysis-lock-validation/v1",
        "result": "PASS",
        "binding_id": BINDING_ID,
        "constitution": constitution_result,
        "human_representation": human_result,
        "legacy_consolidation": legacy_result,
        "adaptive_analysis_model": model_result,
        "registration": registration_result,
    }


def selftest(root: Path) -> dict[str, Any]:
    valid = validate_root(root)
    constitution_document = read_yaml(root / CONSTITUTION_PATH)
    model_document = read_yaml(root / MODEL_PATH)
    checks: list[dict[str, Any]] = []

    def reject(name: str, function, value: dict[str, Any]) -> None:
        try:
            function(value)
        except LockValidationError:
            checks.append({"name": name, "result": "PASS"})
            return
        checks.append({"name": name, "result": "FAIL"})

    changed_statement = copy.deepcopy(constitution_document)
    changed_statement["constitution"]["articles"][0]["machine_statement"] += " TAMPER"
    reject("semantic-tamper-rejected", validate_constitution_document, changed_statement)

    duplicate_article = copy.deepcopy(constitution_document)
    duplicate_article["constitution"]["articles"][1]["id"] = "C-01"
    reject("duplicate-article-rejected", validate_constitution_document, duplicate_article)

    missing_migration = copy.deepcopy(constitution_document)
    del missing_migration["constitution"]["legacy_consolidation"]["mappings"]["MCP-049"]
    reject("incomplete-legacy-migration-rejected", validate_constitution_document, missing_migration)

    unlocked_model = copy.deepcopy(model_document)
    unlocked_model["adaptive_analysis_model"]["status"] = "DRAFT"
    reject("unlocked-adaptive-model-rejected", validate_model_document, unlocked_model)

    automatic_mutation = copy.deepcopy(model_document)
    automatic_mutation["adaptive_analysis_model"]["learning_and_governance"]["automatic_source_mutation"] = True
    reject("automatic-source-mutation-rejected", validate_model_document, automatic_mutation)

    human_text = (root / HUMAN_PATH).read_text(encoding="utf-8")
    changed_human = human_text.replace("Cerebro skal hjelpe mennesket", "Cerebro kan hjelpe mennesket", 1)
    reject(
        "human-representation-drift-rejected",
        lambda value: validate_human_text(value["text"], value["constitution"]),
        {"text": changed_human, "constitution": constitution_document["constitution"]},
    )

    reject(
        "parallel-constitutional-authority-rejected",
        validate_no_parallel_constitutional_authority,
        {"tamper.yaml": "rule:\n  authority: constitutional\n"},
    )

    passed = valid.get("result") == "PASS" and all(item["result"] == "PASS" for item in checks)
    return {
        "schema": "cerebro-constitution-adaptive-analysis-lock-selftest/v1",
        "result": "PASS" if passed else "FAIL",
        "positive_validation": valid,
        "negative_canaries": checks,
    }


def source_state_fingerprint(root: Path) -> str:
    rows: list[str] = []
    for relative in sorted(BASIS_FILES):
        path = root / relative
        if not path.is_file():
            raise LockValidationError(f"activation-basis-file-missing:{relative}")
        rows.append(f"{relative}|{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def activation_probe(root: Path) -> dict[str, Any]:
    tests = selftest(root)
    return {
        "schema": ACTIVATION_SCHEMA,
        "result": tests.get("result"),
        "binding_id": BINDING_ID,
        "proves_bindings": [BINDING_ID],
        "basis_files": list(BASIS_FILES),
        "source_state_fingerprint": source_state_fingerprint(root),
        "constitution_locked": tests.get("positive_validation", {}).get("constitution", {}).get("article_count") == 8,
        "machine_human_parity_verified": tests.get("positive_validation", {}).get("human_representation", {}).get("semantic_parity") is True,
        "legacy_constitutional_rules_consolidated": tests.get("positive_validation", {}).get("legacy_consolidation", {}).get("legacy_rule_count") == 40,
        "parallel_constitutional_authority_absent": tests.get("positive_validation", {}).get("legacy_consolidation", {}).get("parallel_constitutional_rule_count") == 0,
        "adaptive_analysis_model_locked": tests.get("positive_validation", {}).get("adaptive_analysis_model", {}).get("lens_count") == 4,
        "negative_canaries_passed": all(item.get("result") == "PASS" for item in tests.get("negative_canaries", [])),
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def write_output(value: dict[str, Any], output: str | None) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=True) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    else:
        print(text, end="")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the locked Cerebro Constitution and adaptive analysis model")
    parser.add_argument("command", choices=("fingerprint", "validate", "selftest", "activation-probe"))
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output")
    args = parser.parse_args()
    root = Path(args.source_root).resolve()
    try:
        if args.command == "fingerprint":
            value = semantic_fingerprint(read_yaml(root / CONSTITUTION_PATH)["constitution"])
            print(value)
            return 0
        if args.command == "validate":
            value = validate_root(root)
        elif args.command == "selftest":
            value = selftest(root)
        else:
            value = activation_probe(root)
        write_output(value, args.output)
        return 0 if value.get("result") == "PASS" else 1
    except Exception as exc:
        value = {
            "schema": "cerebro-constitution-adaptive-analysis-lock-error/v1",
            "result": "FAIL",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        write_output(value, args.output)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
