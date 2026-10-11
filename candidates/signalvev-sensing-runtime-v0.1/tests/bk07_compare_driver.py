"""Deterministic OFFLINE comparison driver (SYNTHETIC_TEST_ONLY): the existing DNI orient_paragraph known bounded
baseline vs the new read_front entrypoint on the SAME frozen fixtures. Integration evidence, not a natural resource
gain. usage: python bk07_compare_driver.py --dni-import-root DIR --work NEW_DIR --out NEW_FILE"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from signalvev_sensing.bk07_read_front import (AccessContext, FrontRequest, ReadFrontPorts,  # noqa: E402
                                               load_dni_orient, read_front)
from signalvev_sensing.context_slice import ContextSliceResolver  # noqa: E402
from signalvev_sensing.context_slice_provider import LocalPinnedSnapshotProvider  # noqa: E402
from signalvev_sensing.read_front_state import ReadFrontState, canonical  # noqa: E402

FIX = HERE / "bk07_read_front_fixtures"
SCOPE = {"scope": "fixture-known-BK07-rows", "message_ids": ["fixture-bk07-message", "fixture-bk11-message"],
         "max_rows": 128}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 0.001                                   # deterministic: 1 ms per observation
        return self.t


def load(name):
    return json.loads((FIX / name).read_bytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dni-import-root", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    work, out = Path(a.work), Path(a.out)
    work.mkdir(parents=True)                               # refuses an existing dir
    root = work / "root"
    root.mkdir()
    shutil.copyfile(FIX / "bk07-r1.txt", root / "bk07-r1.txt")
    cap = load("cap26_registry.json")
    (root / "cap26_registry.json").write_text(json.dumps(cap))
    nav, manifest = load("navigation_registry.json"), load("channel_manifest_r1.json")
    find, paragraph = load("dni_find.json"), load("dni_paragraph.json")
    orient = load_dni_orient(a.dni_import_root)
    e = nav["entries"][0]

    # ---- baseline: existing DNI known bounded read (one find + one paragraph observation) ----
    b = orient(find, paragraph, expected_document_id=e["file_id"], expected_revision=e["revision"],
               expected_tab_id=e["section"]["tab_id"], max_chars=2000, include_text=True)
    baseline = {"function": "dni reader_v2_adapter.orient_paragraph (existing, pinned)", "status": b["status"],
                "authority": b["authority"], "connector_lookups": 2,
                "delivered_text_chars": len(b["bounded_text_projection"]),
                "delivered_text_utf8_bytes": len(b["bounded_text_projection"].encode()),
                "serialized_tool_result_bytes": len(canonical(find)) + len(canonical(paragraph)),
                "serialized_envelope_bytes": len(canonical(b)), "provider_describes": 0, "content_opens": 0,
                "identity": {"document_id": b["document_id"], "revision": b["paragraph_revision"],
                             "tab_id": b["locator"]["tab_id"]}}

    # ---- new entrypoint: cold, restart hit, owner edit (new revision + channel edit/sort), permission removed ----
    clock = Clock()
    access = AccessContext("principal:fixture-x2", "scope:bk07-fixture", "provider:local-pinned",
                           "SYNTHETIC_TEST_ONLY", 4_102_444_800.0)
    state_path = work / "state.json"

    def run(name):
        provider = LocalPinnedSnapshotProvider(root, root / "cap26_registry.json", clock=clock)
        ports = ReadFrontPorts(clock=clock, wall_clock=lambda: 1_790_000_000.0, provider_stats=provider.stats)
        ports.registry = lambda: json.dumps(nav).encode()
        ports.cap26 = ContextSliceResolver(provider, clock=clock)
        ports.manifest = lambda: copy.deepcopy(manifest)
        ports.dni_orient = orient
        ports.dni = lambda doc: (copy.deepcopy(find), copy.deepcopy(paragraph)) if doc == find["documentId"] else None
        ports.access = lambda: access
        res = read_front(FrontRequest(question="BK07", knagg="BK07.READ_FRONT", subject_ref="actor:fixture-x2",
                                      scope=dict(SCOPE)), ports, ReadFrontState.load(state_path))
        env = res.to_json()
        cur = (res.navigation or {}).get("current") or {}
        return {"scenario": name, "outcome": res.outcome, "reason": res.reason,
                "reuse_status": (res.content or {}).get("reuse_status"),
                "identity": {"document_id": cur.get("file_id"), "revision": cur.get("revision"),
                             "tab_id": cur.get("tab_id")},
                "row_changes": {c["message_id"]: c["change"] for c in (res.rows or {}).get("changes", [])},
                "counters": {k: v for k, v in res.counters.items() if k not in ("elapsed_ms", "state_ms")},
                "provider_actual_ops": provider.stats.as_dict(), "serialized_envelope_bytes": len(env),
                "receipt_bytes": len(res.receipt())}

    scenarios = [run("COLD")]
    scenarios.append(run("RESTART_HIT"))
    new = (FIX / "bk07-r1.txt").read_bytes().replace(b"PM_CONTROLHOST", b"PM_CONTROLHST2")   # same length edit
    (root / "bk07-r2.txt").write_bytes(new)
    full2 = hashlib.sha256(new).hexdigest()
    cap["bindings"][0].update(path="bk07-r2.txt", revision="fixture-r2", sha256=full2,
                              superseded_revisions=["fixture-r1"])
    (root / "cap26_registry.json").write_text(json.dumps(cap))
    e.update(revision="fixture-r2", full_sha256=full2)
    e["section"]["selector"]["expected_slice_sha256"] = hashlib.sha256(new[33:126]).hexdigest()
    for o in (find, paragraph):
        o["revisionId"] = "fixture-r2"
    manifest = load("channel_manifest_edit_sort_r2.json")
    scenarios.append(run("OWNER_EDIT_NEW_REVISION_AND_CHANNEL_EDIT_SORT"))
    cap["bindings"][0]["authorized_subjects"] = []
    (root / "cap26_registry.json").write_text(json.dumps(cap))
    scenarios.append(run("PERMISSION_REMOVED"))

    result = {
        "schema": "bk07.read_front_comparison/v1", "assertion": "SYNTHETIC_TEST_ONLY", "authority": "NONE",
        "same_fixtures": ["navigation_registry.json", "cap26_registry.json", "bk07-r1.txt", "dni_find.json",
                          "dni_paragraph.json", "channel_manifest_r1.json", "channel_manifest_edit_sort_r2.json"],
        "baseline_known_bounded_read": baseline, "new_entrypoint": scenarios,
        "identity_comparison": {
            "baseline": baseline["identity"], "new_cold": scenarios[0]["identity"],
            "same_document_revision_tab": baseline["identity"] == scenarios[0]["identity"],
            "disposition_baseline": baseline["status"], "disposition_new_cold": scenarios[0]["outcome"],
            "note": "baseline gives ORIENTATION_ONLY text; new cold call gives the CAP26-verified exact slice PLUS the "
                    "same DNI orientation; dispositions are different kinds by design"},
        "natural_baseline": {"source": "evidence/A1_BK07_FIRST_NATURAL_READ.json (actual, 16 lookups, 4665 front "
                                       "chars, 11950 serialized bytes, 0 repeated reads)",
                             "qualification": "NO_MATCHED_BASELINE",
                             "reason": "different document, revision, question and scope; offline fixture bytes are "
                                       "not comparable with the natural A1 read"},
        "tokens": "UNKNOWN", "model_starts": "UNKNOWN", "spend": "UNKNOWN", "weekly": "UNKNOWN",
        "net_saving": "UNKNOWN", "backend_bytes": "UNKNOWN",
        "caveat": "A reuse hit still spends 2 authorized metadata describes, a registry lookup and a manifest read; "
                  "it saves the content open/full hash, not necessarily a model activation."}
    out.write_bytes(json.dumps(result, indent=1, sort_keys=True).encode() + b"\n")
    print(json.dumps({"outcomes": [s["outcome"] for s in scenarios]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
