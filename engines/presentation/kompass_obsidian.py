#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "cerebro-kompass-obsidian-render-receipt/v1"
AUTHORITY = "PRESENTATION_ONLY_NON_AUTHORITATIVE"
NOTE_FILES = (
    "00_KOMPASS.md",
    "10_HER_ER_VI.md",
    "20_REISEN.md",
    "30_BLOKKERE_OG_HUMAN_GATER.md",
    "40_INFO.md",
    "50_FORANKRET_GRUNNLAG.md",
    "90_KILDER_OG_EVIDENS.md",
)
CANVAS_FILE = "CEREBRO_KOMPASS.canvas"

class KompassRenderError(ValueError):
    pass

def _load_hap():
    path = Path(__file__).with_name("human_admin_projection.py")
    spec = importlib.util.spec_from_file_location("cerebro_hap_for_kompass", path)
    if spec is None or spec.loader is None:
        raise KompassRenderError("HAP_MODULE_LOAD_FAILED")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _md_list(items: list[str], empty: str = "NONE") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)

def _info_cards(projection: Mapping[str, Any], hap: Any) -> str:
    cards: list[str] = []
    for obj in projection.get("objects", []):
        ref = str(obj.get("canonical_ref") or "")
        if not ref:
            continue
        view = hap.render_info(projection, ref)
        if view.get("result") != "RESOLVED":
            continue
        cards.append("### " + ref + "\n\n" + str(view.get("text") or "UNKNOWN"))
    return "\n\n".join(cards) if cards else "Ingen resolvable objekter i gjeldende projection."


def build_notes(projection: Mapping[str, Any]) -> dict[str, str]:
    hap = _load_hap()
    p = hap.validate_projection(projection)
    orientation = p["orientation"]
    currentness = p["currentness"]
    objective = p["current_objective"]
    human_location = p["human_cognitive_location"]
    return_points = list(p.get("return_points") or [])
    blockers = list(p.get("blockers") or [])
    human_gate = p["next_human_gate"]
    notes: dict[str, str] = {}
    notes["00_KOMPASS.md"] = (
        "# CEREBRO KOMPASS\n\n"
        "**HER VI ER · VEIEN VIDERE**\n\n"
        f"- Tilstand: `{currentness}`\n"
        f"- Gjeldende mål: {objective}\n"
        "- Myndighet: PRESENTATION_ONLY_NON_AUTHORITATIVE\n\n"
        "Kompass er en levende, utskiftbar projeksjon over samme HUMAN_ADMIN_PROJECTION. "
        "Den eier ingen arbeidstilstand og kan slettes uten å endre Core, CLI eller N0.\n\n"
        "## Direkte ruter\n"
        "- [[10_HER_ER_VI|Her er vi]]\n"
        "- [[20_REISEN|Reisen]]\n"
        "- [[30_BLOKKERE_OG_HUMAN_GATER|Blokkere og Human-grenser]]\n"
        "- [[40_INFO|Info]]\n"
        "- [[50_FORANKRET_GRUNNLAG|Forankret grunnlag]]\n"
        "- [[90_KILDER_OG_EVIDENS|Kilder og evidens]]\n"
    )
    notes["10_HER_ER_VI.md"] = (
        "# Her er vi\n\n"
        f"**CURRENTNESS:** `{currentness}`\n\n"
        f"## Gjeldende mål\n{objective}\n\n"
        f"## Nå\n{orientation['where_we_are']}\n\n"
        f"## Kognitiv plassering\n{human_location}\n\n"
        "## Return point\n" + _md_list(return_points, "UNKNOWN") + "\n\n"
        f"## Neste Human-grense\n{human_gate}\n"
    )
    notes["20_REISEN.md"] = (
        "# Reisen\n\n"
        "## FORANKRET GRUNNLAG · HVOR VI VAR\n"
        f"{orientation['where_we_were']}\n\n"
        "## LEVENDE KOMPASS · HER ER VI\n"
        f"[{currentness}] {orientation['where_we_are']}\n\n"
        "## RETNING · HVOR VI SKAL\n"
        f"{orientation['where_we_are_going']}\n"
    )
    notes["30_BLOKKERE_OG_HUMAN_GATER.md"] = (
        "# Blokkere og Human-grenser\n\n"
        "## Materielle blokkere\n" + _md_list(blockers, "Ingen material blocker") + "\n\n"
        f"## Neste Human-grense\n{human_gate}\n\n"
        "UNKNOWN/HOLD/NONPASS er egne tilstander og skal ikke skjules som tomme felt.\n"
    )
    notes["40_INFO.md"] = (
        "# Info\n\n"
        "Samme semantiske kortgrammatikk som `cerebro info <ref>`:\n"
        "WHAT · WHY · NOW · OWNER · BLOCKED_BY · HUMAN · RELATED.\n\n"
        + _info_cards(p, hap) + "\n"
    )
    notes["50_FORANKRET_GRUNNLAG.md"] = (
        "# Forankret grunnlag\n\n"
        "FORANKRET GRUNNLAG er den versjonerte, beviste kontrakt- og informasjonsarkitekturen. "
        "LEVENDE KOMPASS er kun gjeldende presentasjon og kan aldri omskrive grunnlaget.\n\n"
        f"- Source revision: `{p['source_revision']}`\n"
        f"- Projection: `{p['projection_ref']}` rev {p['projection_revision']}\n"
        f"- Authority: `{p['authority']}`\n"
    )
    basis_lines = [
        f"- `{b['object_ref']}` — owner `{b['owner_ref']}` — {b['currentness']} — revision/token `{b['revision_or_token']}`"
        for b in p.get("basis_set", [])
    ]
    evidence_lines = [f"- `{ref}`" for ref in p.get("evidence_refs", [])]
    notes["90_KILDER_OG_EVIDENS.md"] = (
        "# Kilder og evidens\n\n"
        "Denne siden er siste lag. Kompassets førstevisning skal ikke kreve rå maskinmateriale.\n\n"
        "## Basis\n" + ("\n".join(basis_lines) if basis_lines else "- NONE") + "\n\n"
        "## Evidens\n" + ("\n".join(evidence_lines) if evidence_lines else "- NONE") + "\n\n"
        f"## Projection fingerprint\n`{p['projection_fingerprint']}`\n"
    )
    return notes


def build_canvas() -> dict[str, Any]:
    files = [
        ("past", "20_REISEN.md", -980, 0),
        ("now", "10_HER_ER_VI.md", -330, 0),
        ("gate", "30_BLOKKERE_OG_HUMAN_GATER.md", 320, 0),
        ("info", "40_INFO.md", 970, 0),
        ("foundation", "50_FORANKRET_GRUNNLAG.md", -650, 430),
        ("evidence", "90_KILDER_OG_EVIDENS.md", 650, 430),
    ]
    nodes = [
        {"id": node_id, "type": "file", "file": file_name, "x": x, "y": y, "width": 520, "height": 300}
        for node_id, file_name, x, y in files
    ]
    edges = [
        {"id": "past-now", "fromNode": "past", "toNode": "now"},
        {"id": "now-gate", "fromNode": "now", "toNode": "gate"},
        {"id": "gate-info", "fromNode": "gate", "toNode": "info"},
        {"id": "past-foundation", "fromNode": "past", "toNode": "foundation"},
        {"id": "info-evidence", "fromNode": "info", "toNode": "evidence"},
    ]
    return {"nodes": nodes, "edges": edges}


def simplification_receipt() -> dict[str, Any]:
    return {
        "schema": "cerebro-kompass-simplification-receipt/v1",
        "external_dependencies": [],
        "background_processes": 0,
        "state_stores": 0,
        "markdown_notes": len(NOTE_FILES),
        "canvas_files": 1,
        "duplicate_truth_fields": 0,
    }

def render_vault(projection: Mapping[str, Any], output_dir: str | Path) -> dict[str, Any]:
    hap = _load_hap()
    p = hap.validate_projection(projection)
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    notes = build_notes(p)
    if set(notes) != set(NOTE_FILES):
        raise KompassRenderError("NOTE_SET_MISMATCH")
    files: dict[str, str] = {}
    for name in NOTE_FILES:
        data = (notes[name].rstrip() + "\n").encode("utf-8")
        (target / name).write_bytes(data)
        files[name] = _sha256(data)
    canvas_data = (json.dumps(build_canvas(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (target / CANVAS_FILE).write_bytes(canvas_data)
    files[CANVAS_FILE] = _sha256(canvas_data)
    body = {
        "schema": SCHEMA,
        "authority": AUTHORITY,
        "projection_fingerprint": p["projection_fingerprint"],
        "source_revision": p["source_revision"],
        "markdown_count": len(NOTE_FILES),
        "canvas_count": 1,
        "files": dict(sorted(files.items())),
        "simplification": simplification_receipt(),
        "source_mutation": False,
        "state_owner": False,
    }
    return {**body, "receipt_fingerprint": _sha256(_canonical(body))}

def validate_rendered_vault(output_dir: str | Path, receipt: Mapping[str, Any]) -> dict[str, Any]:
    target = Path(output_dir)
    if receipt.get("schema") != SCHEMA or receipt.get("authority") != AUTHORITY:
        raise KompassRenderError("RECEIPT_IDENTITY_MISMATCH")
    expected = set(NOTE_FILES) | {CANVAS_FILE}
    actual = {p.name for p in target.iterdir() if p.is_file()}
    if actual != expected:
        raise KompassRenderError("RENDERED_FILE_SET_MISMATCH")
    for name, expected_hash in receipt.get("files", {}).items():
        path = target / name
        if not path.is_file() or _sha256(path.read_bytes()) != expected_hash:
            raise KompassRenderError("RENDERED_FILE_HASH_MISMATCH:" + name)
    body = {k: v for k, v in receipt.items() if k != "receipt_fingerprint"}
    if receipt.get("receipt_fingerprint") != _sha256(_canonical(body)):
        raise KompassRenderError("RECEIPT_FINGERPRINT_MISMATCH")
    return {
        "schema": "cerebro-kompass-obsidian-readback/v1",
        "result": "PASS",
        "markdown_count": len(NOTE_FILES),
        "canvas_count": 1,
        "projection_fingerprint": receipt["projection_fingerprint"],
        "source_mutation": False,
    }
