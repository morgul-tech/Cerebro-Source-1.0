#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HAP_PATH = ROOT / "engines/presentation/human_admin_projection.py"
RENDERER_PATH = ROOT / "engines/presentation/kompass_obsidian.py"
COMPONENT_PATH = ROOT / "engines/presentation/component.yaml"

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

def _fixture(hap):
    source = "1fd3f76d74e9ff018c514850e56ff23fd8f400d0"
    snapshots = []
    snapshots.append({
        "schema": "cerebro-owner-snapshot/v1",
        "object_ref": "WORK_CLAIMS:842",
        "object_type": "WORK_CLAIM",
        "owner_ref": "SHARED_WORK",
        "currentness": "CURRENT",
        "revision_or_token": 842,
        "evidence_ref": "WORK_CLAIMS:842",
        "human_summary": "P613 Kompass renderer claim",
        "why_it_matters": "Governing presentation sidefront.",
        "aliases": ["P613"],
        "related_refs": ["WORK_PACKETS:631/P613"],
        "state": {"status": "ACTIVE_BOUND_START_DISPATCHED"},
    })
    snapshots[0]["state"].update({
        "current_objective": "CEREBRO KOMPASS v0",
        "where_we_were": "P612 HAP core PASS",
        "where_we_are": "P613 renderer validation",
        "where_we_are_going": "P613 readback and PM admission",
        "human_cognitive_location": "Kompass closure",
        "return_point": "P613 terminal report",
        "blockers": [],
        "human_action": "NONE",
    })
    snapshots.append({
        "schema": "cerebro-owner-snapshot/v1",
        "object_ref": "SOURCE:FROZEN_ROADMAP",
        "object_type": "ROADMAP_FOUNDATION",
        "owner_ref": "CEREBRO_SOURCE",
        "currentness": "CURRENT",
        "revision_or_token": source,
        "evidence_ref": "roadmap-schema@source",
        "human_summary": "Forankret roadmap foundation",
        "why_it_matters": "Preserves proven information architecture.",
        "aliases": ["forankret grunnlag"],
        "related_refs": ["engines/project/roadmap-schema.yaml"],
        "state": {"status": "SOURCE_CURRENT"},
    })
    return hap.build_projection(
        source_revision=source,
        owner_snapshots=snapshots,
        required_refs=["WORK_CLAIMS:842"],
        projection_revision=3,
    )


def run_all():
    hap = _load(HAP_PATH, "cerebro_hap_kompass_validation")
    renderer = _load(RENDERER_PATH, "cerebro_kompass_renderer_validation")
    projection = _fixture(hap)
    tests = []
    def check(name, condition):
        tests.append({"name": name, "result": "PASS" if condition else "FAIL"})

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        vault = root / "vault"
        t0 = time.perf_counter()
        receipt = renderer.render_vault(projection, vault)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        readback = renderer.validate_rendered_vault(vault, receipt)
        check("render-readback-pass", readback["result"] == "PASS")
        check("one-canvas-seven-notes", receipt["canvas_count"] == 1 and receipt["markdown_count"] == 7)
        check("presentation-only-authority", receipt["authority"] == "PRESENTATION_ONLY_NON_AUTHORITATIVE")
        check("projection-fingerprint-bound", receipt["projection_fingerprint"] == projection["projection_fingerprint"])
        entry = (vault / "00_KOMPASS.md").read_text(encoding="utf-8")
        now = (vault / "10_HER_ER_VI.md").read_text(encoding="utf-8")
        journey = (vault / "20_REISEN.md").read_text(encoding="utf-8")
        blockers = (vault / "30_BLOKKERE_OG_HUMAN_GATER.md").read_text(encoding="utf-8")
        info = (vault / "40_INFO.md").read_text(encoding="utf-8")
        evidence = (vault / "90_KILDER_OG_EVIDENS.md").read_text(encoding="utf-8")
        check("orientation-5-10-second-fields", all(x in journey for x in ("HVOR VI VAR", "HER ER VI", "HVOR VI SKAL")))
        check("human-cognitive-location-preserved", "Kompass closure" in now)
        check("blocker-and-human-gate-explicit", "Neste Human-grense" in blockers and "NONE" in blockers)
        cli_info = hap.render_info(projection, "P613")["text"]
        check("info-grammar-matches-cli", all(line in info for line in cli_info.splitlines()))
        check("evidence-last-layer", "roadmap-schema@source" in evidence and "roadmap-schema@source" not in entry)
        canvas = json.loads((vault / renderer.CANVAS_FILE).read_text(encoding="utf-8"))
        check("canvas-calm-bounded", len(canvas["nodes"]) <= 7 and len(canvas["edges"]) <= 6)
        check("canvas-file-nodes-only", all(node.get("type") == "file" for node in canvas["nodes"]))
        check("status-meaning-not-color-dependent", all("color" not in node for node in canvas["nodes"]))
        second = root / "vault2"
        receipt2 = renderer.render_vault(projection, second)
        check("deterministic-file-manifest", receipt["files"] == receipt2["files"])
        check("deterministic-receipt", receipt["receipt_fingerprint"] == receipt2["receipt_fingerprint"])
        simp = receipt["simplification"]
        check("simplification-no-extra-deps-processes-stores", simp["external_dependencies"] == [] and simp["background_processes"] == 0 and simp["state_stores"] == 0)
        before = projection["projection_fingerprint"]
        shutil.rmtree(vault)
        check("delete-renderer-output-zero-core-effect", hap.validate_projection(projection)["projection_fingerprint"] == before and not vault.exists())
        check("ordinary-host-benchmark-recorded", elapsed_ms >= 0.0)

    source_text = RENDERER_PATH.read_text(encoding="utf-8")
    check("no-network-or-provider-client", not any(token in source_text for token in ("requests.", "http://", "https://", "socket.", "subprocess.")))
    component = COMPONENT_PATH.read_text(encoding="utf-8")
    check("presentation-component-registers-kompass", "kompass_obsidian" in component and "obsidian-ne-authority" in component)
    result = "PASS" if all(t["result"] == "PASS" for t in tests) else "FAIL"
    return {
        "schema": "cerebro-kompass-obsidian-validation/v1",
        "result": result,
        "tests": tests,
        "test_count": len(tests),
        "pass_count": sum(t["result"] == "PASS" for t in tests),
        "benchmark_ms": round(elapsed_ms, 3),
        "simplification": receipt["simplification"],
    }


if __name__ == "__main__":
    output = run_all()
    print(json.dumps(output, indent=2, ensure_ascii=False))
    raise SystemExit(0 if output["result"] == "PASS" else 2)
