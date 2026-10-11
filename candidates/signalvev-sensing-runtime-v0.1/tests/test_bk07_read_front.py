"""BK07 read front: focused tests for the NEW navigation/bookmark/reuse seams + direct CAP26/DNI composition."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from bk07_read_front_support import (FIX, FULL_SHA, KNAGG, ORIGINAL, SCOPE, SLICE, SLICE_SHA, FrontCase, dni_root,
                                     load, sha)
from signalvev_sensing.bk07_read_front import (AMBIGUOUS, DENIED, FALLBACK_REQUIRED, FRESH_SLICE, INVALID_REQUEST,
                                               INVALIDATED, MISSING_PORT, ORIENTATION_ONLY, REUSED_SLICE, STALE,
                                               SUPERSEDED, UNREADABLE, AccessContext, FrontError, FrontRequest,
                                               ResultUnavailable, build_from_config, parse_navigation_registry,
                                               resolve_current)
from signalvev_sensing.context_slice import SliceLimits
from signalvev_sensing.read_front_state import CORRUPT, ReadFrontState, canonical

SRC = Path(__file__).resolve().parents[1] / "src"


def entry(**over):
    e = copy.deepcopy(load("navigation_registry.json")["entries"][0])
    e.update(over)
    return e


def reg(*entries, revision="r-test"):
    return json.dumps({"schema": "bk07-read-front-import-fixture/v1", "revision": revision,
                       "assertion": "SYNTHETIC_TEST_ONLY", "entries": list(entries)}).encode()


class A_RegistryCurrentOwnerSupersession(FrontCase):
    def test_a_supplied_registry_resolves_exact_current(self):
        cur, chain = resolve_current(parse_navigation_registry((FIX / "navigation_registry.json").read_bytes()), KNAGG)
        self.assertEqual((cur.file_id, cur.revision, cur.status), ("fixture-doc-bk07", "fixture-r1", "CURRENT"))

    def test_a_explicit_supersession_old_original_found_never_current(self):
        old = entry(file_id="doc-old", status="SUPERSEDED", superseded_by="doc-new", revision="r-old")
        new = entry(file_id="doc-new", knagg="BK07.SUCCESSOR")
        for order in ((old, new), (new, old)):                       # row order is not authority
            cur, chain = resolve_current(parse_navigation_registry(reg(*order)), KNAGG)
            self.assertEqual(cur.file_id, "doc-new")
            self.assertEqual(chain, [{"original": "doc-old", "chain": ["doc-old", "doc-new"], "end_status": "CURRENT"}])
        self.nav = json.loads(reg(old, new))
        res = self.call()
        self.assertEqual(res.outcome, FRESH_SLICE)
        self.assertEqual((res.navigation["current"]["file_id"], res.navigation["historical"]), ("doc-new", ["doc-old"]))

    def test_a_cycle_depth_missing_replacement_unknown_status_and_evidence(self):
        cases = [
            ((entry(file_id="a", status="SUPERSEDED", superseded_by="b"),
              entry(file_id="b", knagg="X.B", status="SUPERSEDED", superseded_by="a")), "SUPERSESSION_CYCLE"),
            (tuple(entry(file_id=f"d{i}", knagg=KNAGG if i == 0 else f"X.D{i}", status="SUPERSEDED",
                         superseded_by=f"d{i + 1}") for i in range(6)) + (entry(file_id="d6", knagg="X.D6"),),
             "SUPERSESSION_DEPTH_OVER_4"),
            ((entry(status="SUPERSEDED", superseded_by=None),), "SUPERSEDED_WITHOUT_OWNER_REPLACEMENT"),
            ((entry(status="SUPERSEDED", superseded_by="nowhere"),), "REPLACEMENT_NOT_IN_REGISTRY"),
            ((entry(status="UNKNOWN"),), "OWNER_STATUS_UNKNOWN"),
            ((entry(evidence_ref=None),), "CURRENT_WITHOUT_OWNER_EVIDENCE"),
        ]
        for entries, reason in cases:
            self.nav = json.loads(reg(*entries))
            res = self.call()
            self.assertEqual((res.outcome, res.reason), (FALLBACK_REQUIRED, reason))
            self.assert_no_content(res)
        self.assertEqual(self.provider.stats.describes, 0)

    def test_a_conflicting_active_referents_ambiguous_no_content(self):
        self.nav = json.loads(reg(entry(), entry(file_id="fixture-doc-other")))
        res = self.call()
        self.assertEqual((res.outcome, res.reason), (AMBIGUOUS, "MULTIPLE_ACTIVE_REFERENTS"))
        self.assert_no_content(res)
        self.assertEqual((res.counters["provider_describes"], self.provider.stats.content_opens), (0, 0))
        self.nav = json.loads(reg(entry(), entry(revision="other")))       # same file id, conflicting records
        self.assertEqual(self.call().outcome, AMBIGUOUS)

    def test_a_bounded_strict_registry(self):
        for bad in (reg(*[entry(file_id=f"f{i}", knagg=f"K.{i}") for i in range(65)]),
                    reg(entry(content_ref="../bk07-r1.txt")), reg(entry(file_id=r"\\server\share")),
                    b'{"schema":"bk07-read-front-import-fixture/v1","schema":"x"}', b"x" * 70000,
                    reg({**entry(), "extra": 1})):
            self.nav = None
            state = ReadFrontState.load(self.state_path)
            ports = self.ports()
            ports.registry = lambda bad=bad: bad
            from signalvev_sensing.bk07_read_front import read_front
            res = read_front(self.request(), ports, state)
            self.assertIn(res.outcome, (UNREADABLE, FALLBACK_REQUIRED))
            self.assert_no_content(res)


class B_ColdThenRestartReuse(FrontCase):
    def test_b_cold_cap26_then_restart_hit_same_authorized_slice(self):
        cold = self.call()
        self.assertEqual((cold.outcome, cold.content["slice_text"].encode(), cold.content["slice_sha256"]),
                         (FRESH_SLICE, SLICE, SLICE_SHA))
        self.assertEqual(cold.content["full_sha256"], FULL_SHA)
        self.assertEqual((self.provider.stats.content_opens, self.provider.stats.describes), (1, 2))
        self.assertEqual(cold.counters["bytes_hashed"], len(ORIGINAL))
        hit = self.call()                                     # new state object from disk + new provider = restart
        self.assertEqual((hit.outcome, hit.content["slice_text"].encode(), hit.content["slice_sha256"]),
                         (REUSED_SLICE, SLICE, SLICE_SHA))
        self.assertEqual((self.provider.stats.content_opens, self.provider.stats.describes,
                          hit.counters["provider_describes"], hit.counters["repeated_identical_reads"]), (0, 2, 2, 1))
        self.assertEqual(hit.content["currentness"]["semantic_currentness"], "UNKNOWN")   # never authenticated CURRENT
        self.assertEqual((self.root / "bk07-r1.txt").read_bytes(), ORIGINAL)
        st = json.loads(self.state_path.read_bytes())
        self.assertNotIn(ORIGINAL.decode()[:120], json.dumps(st))                         # only the slice is cached
        self.assertLessEqual(len(hit.receipt()), 2048)
        self.assertNotIn(b"STATE=CODE_PASS", hit.receipt())


class C_RowBookmarks(FrontCase):
    def rows(self, res):
        return {c["message_id"]: c for c in res.rows["changes"]}

    def test_c_edit_and_sort_detected_from_fresh_manifest(self):
        self.call()
        self.manifest = load("channel_manifest_edit_sort_r2.json")
        rows = self.rows(self.call())
        self.assertEqual(rows["fixture-bk07-message"]["change"], "EDITED")
        self.assertEqual(rows["fixture-bk07-message"]["content"], "BK07 source binding supplied, session unknown")
        self.assertEqual((rows["fixture-bk11-message"]["change"], rows["fixture-bk11-message"]["row"]), ("MOVED", 6))
        self.assertNotIn("content", rows["fixture-bk11-message"])                        # unchanged identity not re-sent
        third = self.rows(self.call())
        self.assertEqual({k: v["change"] for k, v in third.items()},
                         {"fixture-bk07-message": "UNCHANGED", "fixture-bk11-message": "UNCHANGED"})

    def test_c_edit_before_highwater_delete_tombstone(self):
        self.call()
        m = load("channel_manifest_r1.json")
        m["rows"][0]["content"] = "BK07 edited in place at row 6, below highwater 7"
        self.manifest = m
        self.assertEqual(self.rows(self.call())["fixture-bk07-message"]["change"], "EDITED")
        m = copy.deepcopy(m)
        m["rows"] = [r for r in m["rows"] if r["message_id"] != "fixture-bk07-message"]
        self.manifest = m
        rows = self.rows(self.call())
        self.assertEqual((rows["fixture-bk07-message"]["change"], rows["fixture-bk11-message"]["change"]),
                         ("TOMBSTONE", "UNCHANGED"))

    def test_c_duplicate_ids_and_partial_manifest_no_reuse(self):
        self.call()
        m = load("channel_manifest_r1.json")
        m["rows"].append({**m["rows"][0], "content": "conflicting duplicate"})
        self.manifest = m
        res = self.call()
        self.assertEqual((res.rows["outcome"], res.rows["reason"]), (AMBIGUOUS, "DUPLICATE_MESSAGE_IDS"))
        dup = self.rows(res)["fixture-bk07-message"]
        self.assertEqual(dup["change"], "AMBIGUOUS_DUPLICATE_ID")
        self.assertNotIn("content", dup)
        m = load("channel_manifest_r1.json")
        m["coverage"]["complete"] = False
        m["rows"] = m["rows"][1:]
        self.manifest = m
        res = self.call()
        self.assertEqual((res.rows["outcome"], res.rows["global_completeness"]), (FALLBACK_REQUIRED, "UNKNOWN"))
        self.assertNotIn("TOMBSTONE", json.dumps(res.rows))                               # no inference from partial
        self.assertEqual(self.rows(res)["fixture-bk11-message"]["change"], "FRESH_UNQUALIFIED")

    def test_c_unrelated_bk11_entry_kept_when_only_bk07_binding_changes(self):
        bk11 = b"BK11 synthetic sister front\nREADY=INDEPENDENT\n"
        self.add_content("bk11-r1.txt", bk11, "fixture:bk11-front")
        e11 = entry(knagg="BK11.READ_FRONT", file_id="fixture-doc-bk11", content_ref="fixture:bk11-front",
                    full_sha256=sha(bk11), section={"tab_id": "t", "semantic_anchor": "BK11",
                                                    "selector": {"kind": "UTF8_BYTE_RANGE", "start": 0, "end": 4,
                                                                 "expected_slice_sha256": sha(bk11[:4])}})
        self.nav = json.loads(reg(entry(), e11))
        self.assertEqual(self.call(request=self.request(knagg="BK11.READ_FRONT", question="BK11")).outcome,
                         FRESH_SLICE)
        self.call()
        before = canonical(self.stored("BK11.READ_FRONT"))
        sel = {"kind": "UTF8_BYTE_RANGE", "start": 0, "end": 33, "expected_slice_sha256": sha(ORIGINAL[:33])}
        self.nav = json.loads(reg(entry(section={"tab_id": "fixture-tab", "semantic_anchor": "BK07_TITLE",
                                                 "selector": sel}), e11, revision="r-test-2"))
        res = self.call()
        self.assertEqual((res.outcome, res.content["slice_text"].encode()), (FRESH_SLICE, ORIGINAL[:33]))
        self.assertEqual(canonical(self.stored("BK11.READ_FRONT")), before)
        hit11 = self.call(request=self.request(knagg="BK11.READ_FRONT", question="BK11"))
        self.assertEqual(hit11.outcome, REUSED_SLICE)


class D_AccessAndAuthorization(FrontCase):
    def test_d_revoked_permission_on_hit_denied_no_payload(self):
        self.call()
        self.cap26_registry["bindings"][0]["authorized_subjects"] = ["actor:someone-else"]
        self.write_cap26()
        res = self.call()
        self.assertEqual(res.outcome, DENIED)
        self.assert_no_content(res)
        self.assertEqual(self.provider.stats.content_opens, 0)
        self.assertNotIn("slice_text", self.stored())                                    # payload removed

    def test_d_revoked_between_first_and_second_describe(self):
        self.call()

        def revoke():
            self.cap26_registry["bindings"][0]["authorized_subjects"] = []
            self.write_cap26()
        res = self.call(wrap=lambda w: w.after_describe.__setitem__(1, revoke))
        self.assertEqual((res.outcome, res.reason[:15]), (DENIED, "HIT_DESCRIBE_2:"))
        self.assert_no_content(res)
        self.assertEqual(self.provider.stats.content_opens, 0)

    def test_d_changed_principal_scope_provider_expired_unknown_access(self):
        variants = [AccessContext("principal:other", "scope:bk07-fixture", "provider:local-pinned",
                                  "SYNTHETIC_TEST_ONLY", 4e9),
                    AccessContext("principal:fixture-x2", "scope:other", "provider:local-pinned",
                                  "SYNTHETIC_TEST_ONLY", 4e9),
                    AccessContext("principal:fixture-x2", "scope:bk07-fixture", "provider:other",
                                  "SYNTHETIC_TEST_ONLY", 4e9),
                    AccessContext("principal:fixture-x2", "scope:bk07-fixture", "provider:local-pinned",
                                  "SYNTHETIC_TEST_ONLY", 1.0),
                    None]
        base = self.access
        for v in variants:
            self.state_path.unlink(missing_ok=True)
            self.access = base
            self.call()
            self.access = v
            res = self.call(access=v is not None)
            self.assertEqual(res.outcome, FRESH_SLICE)                                  # cold bounded read, no reuse
            self.assertEqual(self.provider.stats.content_opens, 1)
            if v is None or v.expires_at == 1.0:
                self.assertEqual(res.content["reuse_status"], "REUSE_UNQUALIFIED")
                self.assertNotIn("slice_text", self.stored())

    def test_d_denied_cold_read_zero_body_opens(self):
        res = self.call(request=self.request(subject_ref="actor:stranger"))
        self.assertEqual(res.outcome, DENIED)
        self.assertEqual((self.provider.stats.content_opens, self.provider.stats.read_calls), (0, 0))
        self.assert_no_content(res)


class E_DriftAndCorruption(FrontCase):
    def test_e_stale_descriptor_fails_closed_then_next_call_cold(self):
        self.call()
        os.utime(self.root / "bk07-r1.txt", ns=(1, 1))                  # source identity changed after cache fill
        res = self.call()
        self.assertEqual((res.outcome, res.reason), (INVALIDATED, "STALE_DESCRIPTOR_AT_DESCRIBE_1"))
        self.assert_no_content(res)
        self.assertEqual((self.provider.stats.content_opens, self.provider.stats.describes), (0, 1))   # no retry
        again = self.call()
        self.assertEqual((again.outcome, self.provider.stats.content_opens), (FRESH_SLICE, 1))

    def test_e_racing_revision_between_describes(self):
        self.call()
        res = self.call(wrap=lambda w: w.after_describe.__setitem__(
            1, lambda: os.utime(self.root / "bk07-r1.txt", ns=(2, 2))))
        self.assertEqual((res.outcome, res.reason), (INVALIDATED, "STALE_DESCRIPTOR_AT_DESCRIBE_2"))
        self.assert_no_content(res)

    def test_e_owner_supersedes_revision(self):
        self.call()
        self.cap26_registry["bindings"][0].update(revision="fixture-r2", superseded_revisions=["fixture-r1"])
        self.write_cap26()
        res = self.call()
        self.assertEqual(res.outcome, SUPERSEDED)
        self.assert_no_content(res)

    def test_e_registry_tab_and_export_drift(self):
        self.call()
        self.nav["entries"][0]["section"]["tab_id"] = "fixture-tab-2"
        res = self.call()
        self.assertEqual(res.outcome, FRESH_SLICE)                       # binding changed => one cold bounded read
        self.assertEqual(self.provider.stats.content_opens, 1)
        self.nav["entries"][0]["representation"] = "NATIVE_DOCS_PROJECTION_V1"   # export domain changed
        res = self.call()
        self.assertEqual((res.outcome, res.reason), (FALLBACK_REQUIRED, "UTF16_DOC_INDEX_IS_NOT_UTF8_BYTE_RANGE"))
        self.assert_no_content(res)
        self.assertEqual(self.provider.stats.describes, 0)

    def test_e_state_corruption_and_tampered_cached_slice(self):
        self.call()
        doc = json.loads(self.state_path.read_bytes())
        doc["body"]["entries"][KNAGG]["slice_text"] = "STATE=EDITED_ON_DISK"       # digest left unchanged
        self.state_path.write_text(json.dumps(doc))
        res = self.call()
        self.assertEqual((res.outcome, res.state_status, self.provider.stats.content_opens), (FRESH_SLICE, CORRUPT, 1))
        self.assertNotIn("EDITED_ON_DISK", json.dumps(res.to_dict()))
        self.state_path.write_bytes(b"{not json")
        res = self.call()
        self.assertEqual((res.outcome, res.state_status, self.provider.stats.content_opens), (FRESH_SLICE, CORRUPT, 1))
        st = ReadFrontState.load(self.state_path)
        e = st.entry(KNAGG)
        e["slice_text"] = "STATE=FORGED\n"
        st.put_entry(KNAGG, e)
        st.commit()                                                      # valid file digest, forged payload
        res = self.call()
        self.assertEqual((res.outcome, res.content["slice_text"].encode()), (FRESH_SLICE, SLICE))
        self.assertEqual(self.provider.stats.content_opens, 1)
        self.assertNotIn("FORGED", json.dumps(res.to_dict()))


class F_RepairedCap26Failures(FrontCase):
    def test_f_late_metadata_and_impossible_caps(self):
        res = self.call(wrap=lambda w: w.after_describe.__setitem__(1, lambda: self.clock.advance(6)))
        self.assertEqual(res.outcome, UNREADABLE)
        self.assertIn("DEADLINE", res.reason)
        self.assert_no_content(res)
        self.assertEqual(self.provider.stats.content_opens, 0)
        res = self.call(request=self.request(limits=SliceLimits(max_result_bytes=300)))
        self.assertEqual((res.outcome, res.reason), (UNREADABLE, "CAP26_RESULT_ENVELOPE_UNAVAILABLE"))
        self.assertEqual(self.provider.stats.content_opens, 0)
        from signalvev_sensing.bk07_read_front import read_front
        out = read_front(self.request(max_result_bytes=512), self.ports(), ReadFrontState.load(self.state_path))
        with self.assertRaises(ResultUnavailable):
            out.to_json()

    def test_f_unbounded_request_and_path_refs_rejected(self):
        for kw in (dict(question="BK07; rm -rf"), dict(question="B" * 40), dict(knagg="x" * 200),
                   dict(scope={**SCOPE, "message_ids": [f"m{i}" for i in range(129)]}),
                   dict(scope={**SCOPE, "max_rows": 10_000}), dict(scope={"scope": "../etc"}),
                   dict(max_result_bytes=10**6)):
            with self.assertRaises(FrontError):
                self.request(**kw)
        cfg = {"schema": "bk07.read_front_config/v1", "enabled": True, "mode": "FIXTURE", "subject_ref": "a:b",
               "questions": {"BK07": KNAGG}, "known_scope": SCOPE, "registry_path": "a\x00b"}
        with self.assertRaises(FrontError) as cm:
            build_from_config(cfg, "BK07")
        self.assertEqual(cm.exception.outcome, INVALID_REQUEST)


class G_DniNativeDocs(FrontCase):
    def native(self, **over):
        e = entry(representation="NATIVE_DOCS_PROJECTION_V1", content_ref=None, full_sha256=None,
                  section={"tab_id": "fixture-tab", "semantic_anchor": "BK07_SYNTHETIC_ORIENTATION",
                           "anchor_text": "BK07", "selector": None})
        e.update(over)
        self.nav = json.loads(reg(e))

    def test_g_orientation_only_reuse_unqualified_never_current(self):
        self.native()
        for _ in range(2):
            res = self.call()
            self.assertEqual(res.outcome, ORIENTATION_ONLY)
            o = res.orientation
            self.assertEqual((o["reuse_status"], o["production_revalidated"], o["reader_issued_receipt"]),
                             ("REUSE_UNQUALIFIED", False, "UNSUPPORTED_BY_PRESENT_CONNECTOR"))
            self.assertEqual(o["projection_text"], self.paragraph["resolvedRange"]["matchedText"])
            self.assertEqual(res.counters["connector_observations"], 2)
            self.assertIsNone(res.content)
        self.assertEqual(self.provider.stats.describes, 0)                  # Docs indices never reach CAP26
        self.assertNotIn("CURRENT", json.dumps(res.orientation))

    def test_g_locator_changes_with_revision_anchor_refind_and_negatives(self):
        self.native()
        self.call()
        self.native(revision="fixture-r2")
        for o in (self.find, self.paragraph):
            o["revisionId"] = "fixture-r2"
        self.find["resolvedRange"].update(startIndex=41, endIndex=45)
        self.paragraph["resolvedRange"].update(startIndex=41, endIndex=117)
        res = self.call()
        self.assertEqual((res.outcome, res.orientation["relocated_since_last_bookmark"]), (ORIENTATION_ONLY, True))
        self.assertEqual(res.orientation["locator"]["start_index"], 41)
        self.native(revision="fixture-r3")                                     # observation still r2 => STALE
        res = self.call()
        self.assertEqual(res.outcome, STALE)
        self.assert_no_content(res)
        self.native(revision="fixture-r2", section={"tab_id": "fixture-tab", "semantic_anchor": "X",
                                                    "anchor_text": "BK11", "selector": None})
        res = self.call()
        self.assertEqual((res.outcome, res.reason), (INVALIDATED, "DNI_ANCHOR_TEXT_NOT_REFOUND"))
        self.native(revision="fixture-r2", section={"tab_id": "other-tab", "semantic_anchor": "X", "selector": None})
        self.assertEqual(self.call().outcome, INVALIDATED)                     # tab drift

    def test_g_utf16_index_confusion_negative(self):
        self.native(section={"tab_id": "fixture-tab", "semantic_anchor": "BK07", "selector": {
            "kind": "UTF8_BYTE_RANGE", "start": 1, "end": 77, "expected_slice_sha256": "a" * 64}})
        res = self.call()
        self.assertEqual((res.outcome, res.reason), (FALLBACK_REQUIRED, "UTF16_DOC_INDEX_IS_NOT_UTF8_BYTE_RANGE"))
        self.assertEqual(self.provider.stats.describes, 0)
        self.assert_no_content(res)


class H_CliOneShot(FrontCase):
    def cfg(self, **over) -> Path:
        c = {"schema": "bk07.read_front_config/v1", "enabled": True, "mode": "FIXTURE",
             "state_path": str(self.state_path), "subject_ref": "actor:fixture-x2", "questions": {"BK07": KNAGG},
             "registry_path": str(FIX / "navigation_registry.json"),
             "manifest_path": str(FIX / "channel_manifest_r1.json"), "known_scope": SCOPE,
             "cap26": {"provider": "LOCAL_PINNED_SNAPSHOT_FIXTURE", "root": str(self.root),
                       "registry_path": str(self.root / "cap26_registry.json")},
             "import_roots": {"dni_reader_v2": str(dni_root())},
             "dni_observations": {"fixture-doc-bk07": {"find": str(FIX / "dni_find.json"),
                                                       "paragraph": str(FIX / "dni_paragraph.json")}},
             "access": {"principal_ref": "principal:fixture-x2", "scope_fingerprint": "scope:bk07-fixture",
                        "provider_context": "provider:local-pinned", "attestation": "SYNTHETIC_TEST_ONLY",
                        "expires_at": 4102444800},
             "human_turn_flag_path": str(self.dir / "human_active.flag")}
        c.update(over)
        p = self.dir / f"cfg-{len(list(self.dir.glob('cfg-*')))}.json"
        p.write_text(json.dumps(c))
        return p

    def run_cli(self, cfg: Path, receipt: str):
        p = subprocess.run([sys.executable, "-B", "-m", "signalvev_sensing.bk07_read_front", "--config", str(cfg),
                            "--question", "BK07", "--receipt", str(self.dir / receipt)], capture_output=True,
                           timeout=60, env=dict(os.environ, PYTHONPATH=str(SRC)))
        out = json.loads(p.stdout) if p.stdout.strip() else None
        return p.returncode, out, p

    def test_h_one_shot_restart_reuse_receipts_original_preserved(self):
        before = {f.name: sha(f.read_bytes()) for f in FIX.iterdir()}
        cfg = self.cfg()
        rc, out, _ = self.run_cli(cfg, "r1.json")
        self.assertEqual((rc, out["outcome"]), (0, FRESH_SLICE))
        rc, out, _ = self.run_cli(cfg, "r2.json")
        self.assertEqual((rc, out["outcome"], out["counters"]["content_opens"]), (0, REUSED_SLICE, 0))
        for r in ("r1.json", "r2.json"):
            data = (self.dir / r).read_bytes()
            self.assertLessEqual(len(data), 2048)
            self.assertNotIn(b"STATE=CODE_PASS", data)
        rc, _, p = self.run_cli(cfg, "r2.json")
        self.assertEqual(rc, 64)                                                        # receipt never overwritten
        self.assertEqual({f.name: sha(f.read_bytes()) for f in FIX.iterdir()}, before)
        self.assertEqual((self.root / "bk07-r1.txt").read_bytes(), ORIGINAL)

    def test_h_default_off_missing_port_and_live_never_synthetic(self):
        rc, out, _ = self.run_cli(self.cfg(enabled=False), "off.json")
        self.assertEqual((rc, out["outcome"], out["reason"]), (3, "DISABLED", "DEFAULT_OFF_ENABLED_NOT_TRUE"))
        self.assertFalse((self.dir / "off.json").exists())
        self.assertFalse(self.state_path.exists())
        rc, out, _ = self.run_cli(self.cfg(registry_path=None), "mp.json")
        self.assertEqual((rc, out["outcome"], out["missing_port"]), (3, MISSING_PORT, "registry"))
        self.assertEqual(out["unresolved_action"], "A1_SUPPLIES_OWNER_REGISTRY_EXPORT")
        rc, out, _ = self.run_cli(self.cfg(mode="LIVE_OBSERVATIONS"), "live.json")
        self.assertEqual((rc, out["outcome"], out["reason"]), (3, MISSING_PORT, "CAP26_LIVE_PROVIDER_NOT_AVAILABLE"))
        rc, out, _ = self.run_cli(self.cfg(mode="LIVE_OBSERVATIONS", cap26=None), "live2.json")
        self.assertEqual((rc, out["outcome"], out["missing_port"], out["assertion"]),
                         (0, ORIENTATION_ONLY, "cap26_provider", "LIVE_OBSERVATION_INPUTS_UNAUTHENTICATED"))
        self.assertIsNone(out["content"])

    def test_h_active_human_turn_no_wake_no_injection(self):
        (self.dir / "human_active.flag").write_text("1")
        files_before = set(self.dir.iterdir())
        rc, out, p = self.run_cli(self.cfg(), "h.json")
        self.assertEqual(rc, 0)
        self.assertEqual((out["wake"], out["chat_message_injected"], out["counters"]["human_turn_active_observed"]),
                         ("NONE", False, True))
        new = {f.name for f in set(self.dir.iterdir()) - files_before}
        self.assertEqual(new, {"h.json", "state.json", "cfg-0.json"} - {f.name for f in files_before})


class I_ComparisonDriver(FrontCase):
    def test_i_driver_same_identity_honest_counters(self):
        out = self.dir / "cmp.json"
        p = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("bk07_compare_driver.py")),
                            "--dni-import-root", str(dni_root()), "--work", str(self.dir / "cmpwork"),
                            "--out", str(out)], capture_output=True, timeout=120,
                           env=dict(os.environ, PYTHONPATH=str(SRC)))
        self.assertEqual(p.returncode, 0, p.stderr)
        d = json.loads(out.read_bytes())
        self.assertEqual(d["assertion"], "SYNTHETIC_TEST_ONLY")
        self.assertTrue(d["identity_comparison"]["same_document_revision_tab"])
        self.assertEqual([s["outcome"] for s in d["new_entrypoint"]],
                         [FRESH_SLICE, REUSED_SLICE, FRESH_SLICE, DENIED])
        self.assertEqual(d["natural_baseline"]["qualification"], "NO_MATCHED_BASELINE")


if __name__ == "__main__":
    unittest.main()
