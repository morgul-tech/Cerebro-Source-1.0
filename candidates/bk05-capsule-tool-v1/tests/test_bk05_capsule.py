"""Deterministic offline tests for bk05_capsule (standard library only).

Run from the archive root:   python3 -B -m unittest tests.test_bk05_capsule -v

All data is the self-created SYNTHETIC fixture in bk05_capsule/synthetic.py.  Nothing here reads a network, a provider,
Drive, or any real PM/claim/queue export.
"""
import ast
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.dont_write_bytecode = True

from bk05_capsule import cli, core, synthetic  # noqa: E402

FILES = synthetic.positive_fixture()
ARG = {"parent": "parent.txt", "manifest": "parent_manifest.json", "verifier": "verifier.txt",
       "delta": "verifier_delta.json", "currentness": "currentness.json"}


def rb(path):
    with open(path, "rb") as fh:
        return fh.read()


def jbytes(obj):
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode("utf-8")


def jload(name):
    return json.loads(FILES[name].decode("utf-8"))


def fx(**over):
    """A copy of the positive fixture; ``over`` replaces raw bytes by file name (use name with '__' for '.')."""
    f = dict(FILES)
    for k, v in over.items():
        f[k.replace("__", ".")] = v
    return f


def with_json(name, fn, base=None):
    f = dict(base or FILES)
    obj = json.loads(f[name].decode("utf-8"))
    fn(obj)
    f[name] = jbytes(obj)
    return f


def with_manifest(fn, base=None):
    return with_json("parent_manifest.json", fn, base)


def with_delta(fn, base=None):
    return with_json("verifier_delta.json", fn, base)


def with_current(fn, base=None):
    return with_json("currentness.json", fn, base)


def run(f, prior=None, reuse="REREAD_ALL", full=True):
    return core.evaluate(f.get("parent.txt"), f.get("parent_manifest.json"), f.get("verifier.txt"),
                         f.get("verifier_delta.json"), f.get("currentness.json"),
                         f.get("full_continuation.txt") if full else None, prior, reuse)


def codes(ev):
    return [r["code"] for r in ev.reasons]


def refresh_hashes(f):
    """Re-point manifest/delta hashes at (changed) parent/verifier bytes so only the intended defect remains."""
    f = dict(f)
    f = with_manifest(lambda m: m.update(parent_sha256=hashlib.sha256(f["parent.txt"]).hexdigest()), f)
    return with_delta(lambda d: d.update(verifier_sha256=hashlib.sha256(f["verifier.txt"]).hexdigest()), f)


class Positive(unittest.TestCase):
    def test_candidate_and_labels(self):
        ev = run(FILES)
        self.assertEqual(ev.decision, "LOCAL_CAPSULE_CANDIDATE")
        cap = ev.capsule
        self.assertEqual((cap["authority"], cap["status"], cap["validation"]), ("NONE", "LOCAL_CANDIDATE_ONLY", "STRUCTURAL_ONLY"))
        self.assertEqual(cap["reviews"]["parent"]["assertion"], "CALLER_ASSERTED_UNAUTHENTICATED")
        self.assertEqual(cap["reviews"]["verifier"]["assertion"], "CALLER_ASSERTED_UNAUTHENTICATED")
        self.assertEqual(cap["currentness_basis"]["assertion"], "CALLER_SUPPLIED_NOT_LIVE_PROOF")
        self.assertEqual(cap["allowed_repair_paths"], ["adapter.py", "test_adapter.py"])
        self.assertEqual(cap["parent"]["revision"], "rev-3")
        self.assertEqual(cap["source_head"], synthetic.SOURCE_HEAD)

    def test_capsule_carries_every_required_field_without_omission(self):
        cap, m, d = run(FILES).capsule, jload("parent_manifest.json"), jload("verifier_delta.json")
        self.assertEqual(cap["required_invariants"], m["required_invariants"])
        self.assertEqual(cap["stop_edges"], m["stop_edges"])
        self.assertEqual((cap["return_target"], cap["way_home"], cap["actor_ref"]),
                         (m["return_target"], m["way_home"], m["actor_ref"]))
        self.assertEqual(cap["required_test_delta"], d["required_test_delta"])
        self.assertEqual(cap["inherited_proof_refs"], d["evidence_refs"])
        self.assertEqual(cap["idempotency_key"], d["idempotency_key"])
        self.assertEqual(cap["verifier"]["ref"], d["verifier_ref"])
        self.assertEqual(cap["parent"]["sha256"], hashlib.sha256(FILES["parent.txt"]).hexdigest())
        self.assertEqual(cap["verifier"]["sha256"], hashlib.sha256(FILES["verifier.txt"]).hexdigest())
        self.assertEqual(set(cap), set(core.CAPSULE_SPEC))

    def test_no_forbidden_claims_and_no_authentication_wording(self):
        ev = run(FILES)
        text = core.capsule_file_bytes(ev.capsule).decode("utf-8") + json.dumps(ev.decision_document())
        self.assertEqual(ev.capsule["not_claimed"], ["BIND", "CONSUMPTION", "DEPLOYMENT", "OWNER_APPROVAL",
                                                     "PRODUCTION", "SEND", "START"])
        for word in ("AUTHENTICATED", "VERIFIED_CURRENT", "LIVE_VERIFIED", "APPROVED"):
            stripped = text.replace("UNAUTHENTICATED", "").replace("NOT_AUTHENTICATED", "")
            self.assertNotIn(word, stripped, word)

    def test_adversarial_owner_strings_and_omitted_parent_edge_never_gain_authority(self):
        """Even forged caller assertions and incomplete prose extraction stay structural-only, never effect."""
        f = with_manifest(lambda m: m.update(
            stop_edges=m["stop_edges"][:1],
            semantic_review={"status": "OWNER_REVIEWED", "receipt_ref": "forged-owner-receipt"},
        ))
        f = with_delta(lambda d: d.update(
            semantic_review={"status": "VERIFIER_REVIEWED", "receipt_ref": "forged-verifier-receipt"},
        ), f)
        f = with_current(lambda c: c.update(
            provenance="fabricated authenticated live currentness and approved effect",
        ), f)
        ev = run(f)
        self.assertEqual(ev.decision, "LOCAL_CAPSULE_CANDIDATE")
        self.assertEqual(ev.capsule["authority"], "NONE")
        self.assertEqual(ev.capsule["validation"], "STRUCTURAL_ONLY")
        self.assertEqual(ev.capsule["status"], "LOCAL_CANDIDATE_ONLY")
        self.assertIn("NOT_SEMANTIC_COMPLETENESS", ev.decision_document()["limits"])
        self.assertEqual(ev.capsule["not_claimed"], core.NOT_CLAIMED)
        self.assertEqual(len(ev.capsule["stop_edges"]), 1)

    def test_payload_hash_excludes_itself_and_matches_recomputation(self):
        cap = run(FILES).capsule
        body = {k: v for k, v in cap.items() if k != "payload_sha256"}
        self.assertEqual(cap["payload_sha256"], hashlib.sha256(core.canonical_bytes(body)).hexdigest())

    def test_deterministic_outputs(self):
        a, b = run(FILES), run(FILES)
        self.assertEqual(core.capsule_file_bytes(a.capsule), core.capsule_file_bytes(b.capsule))
        self.assertEqual(core.canonical_bytes(a.measurement), core.canonical_bytes(b.measurement))
        self.assertEqual(json.dumps(a.decision_document(), sort_keys=True), json.dumps(b.decision_document(), sort_keys=True))
        # key order of the input JSON must not matter
        shuffled = with_manifest(lambda m: None)
        self.assertEqual(core.capsule_file_bytes(run(shuffled).capsule), core.capsule_file_bytes(a.capsule))

    def test_payload_changes_when_a_carried_field_changes(self):
        other = run(with_delta(lambda d: d.update(finding="a different finding"))).capsule["payload_sha256"]
        self.assertNotEqual(other, run(FILES).capsule["payload_sha256"])

    def test_cli_end_to_end_and_byte_identical_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            fixd = os.path.join(td, "in")
            self.assertEqual(cli.main(["synthetic-fixture", "--out-dir", fixd], io.BytesIO(), io.BytesIO()), 0)
            res = []
            for n in ("o1", "o2"):
                out = io.BytesIO()
                args = ["build"] + sum(([("--" + k), os.path.join(fixd, v)] for k, v in
                                        (("parent", "parent.txt"), ("parent-manifest", "parent_manifest.json"),
                                         ("verifier", "verifier.txt"), ("verifier-delta", "verifier_delta.json"),
                                         ("currentness", "currentness.json"), ("full-continuation", "full_continuation.txt"))), [])
                self.assertEqual(cli.main(args + ["--out-dir", os.path.join(td, n)], out, io.BytesIO()), 0)
                res.append({f: rb(os.path.join(td, n, f)) for f in os.listdir(os.path.join(td, n))})
            self.assertEqual(res[0], res[1])
            self.assertEqual(sorted(res[0]), ["capsule.json", "decision.json", "measurement.json"])
            self.assertEqual(res[0]["capsule.json"], core.capsule_file_bytes(run(FILES).capsule))


class InvalidInput(unittest.TestCase):
    def assertInvalid(self, ev, code):
        self.assertEqual(ev.decision, "INVALID_INPUT", ev.reasons)
        self.assertIn(code, codes(ev))
        self.assertIsNone(ev.capsule)
        self.assertIsNone(ev.measurement)

    def test_parent_hash_changed(self):
        self.assertInvalid(run(fx(parent__txt=FILES["parent.txt"] + b"x")), "PARENT_HASH_MISMATCH")

    def test_verifier_bytes_changed(self):
        self.assertInvalid(run(fx(verifier__txt=FILES["verifier.txt"] + b"x")), "VERIFIER_HASH_MISMATCH")

    def test_absent_referenced_bytes(self):
        f = dict(FILES)
        del f["parent.txt"]
        self.assertInvalid(run(f), "ABSENT_REFERENCED_BYTES")
        f = dict(FILES)
        del f["verifier.txt"]
        self.assertInvalid(run(f), "ABSENT_REFERENCED_BYTES")

    def test_absent_json_inputs(self):
        for name in ("parent_manifest.json", "verifier_delta.json", "currentness.json"):
            f = dict(FILES)
            del f[name]
            self.assertInvalid(run(f), "ABSENT_INPUT_FILE")

    def test_stop_edge_removed(self):
        self.assertInvalid(run(with_manifest(lambda m: m.update(stop_edges=[]))), "MISSING_STOP_EDGE")
        self.assertInvalid(run(with_manifest(lambda m: m.pop("stop_edges"))), "MISSING_STOP_EDGE")

    def test_other_missing_inheritance_fields(self):
        for key, code in (("required_invariants", "MISSING_REQUIRED_INVARIANT"), ("return_target", "MISSING_RETURN_TARGET"),
                          ("way_home", "MISSING_WAY_HOME")):
            self.assertInvalid(run(with_manifest(lambda m, k=key: m.pop(k))), code)
        self.assertInvalid(run(with_manifest(lambda m: m.update(way_home="  "))), "MISSING_WAY_HOME")
        self.assertInvalid(run(with_manifest(lambda m: m.update(required_invariants=[]))), "MISSING_REQUIRED_INVARIANT")

    def test_removing_one_of_several_stop_edges_is_not_detectable_and_is_documented(self):
        """LIMITATION (README/RETURN): the CLI only sees the typed manifest.  If the owner drops one stop edge from a
        non-empty list the tool cannot know; the capsule carries exactly what the manifest says."""
        ev = run(with_manifest(lambda m: m.update(stop_edges=m["stop_edges"][:1])))
        self.assertEqual(ev.decision, "LOCAL_CAPSULE_CANDIDATE")
        self.assertEqual(len(ev.capsule["stop_edges"]), 1)
        self.assertIn("NOT_SEMANTIC_COMPLETENESS", ev.decision_document()["limits"])

    def test_duplicate_json_key(self):
        raw = FILES["parent_manifest.json"].replace(b'"task_ref"', b'"task_ref": "x",\n  "task_ref"', 1)
        self.assertInvalid(run(fx(parent_manifest__json=raw)), "DUPLICATE_JSON_KEY")
        nested = FILES["verifier_delta.json"].replace(b'"status"', b'"status": "UNREVIEWED", "status"', 1)
        self.assertInvalid(run(fx(verifier_delta__json=nested)), "DUPLICATE_JSON_KEY")

    def test_malformed_and_non_json(self):
        for raw in (b"{", b"[]", b'"x"', b"null", b'{"a": NaN}', b"\xef\xbb\xbf{}", b"   "):
            ev = run(fx(currentness__json=raw))
            self.assertEqual(ev.decision, "INVALID_INPUT")
            self.assertTrue(set(codes(ev)) & {"MALFORMED_JSON", "JSON_NOT_OBJECT"}, (raw, codes(ev)))

    def test_empty_files(self):
        self.assertInvalid(run(fx(parent__txt=b"")), "EMPTY_INPUT")
        self.assertInvalid(run(fx(currentness__json=b"")), "EMPTY_INPUT")

    def test_non_utf8(self):
        self.assertInvalid(run(refresh_hashes(fx(parent__txt=b"\xff\xfe bad"))), "NON_UTF8_INPUT")
        self.assertInvalid(run(fx(currentness__json=b'{"a": "\xff"}')), "NON_UTF8_INPUT")

    def test_lone_surrogate_escape_is_rejected(self):
        raw = FILES["currentness.json"].replace(b"SYNTHETIC_FIXTURE", b"\\ud800")
        self.assertInvalid(run(fx(currentness__json=raw)), "INVALID_STRING")

    def test_unknown_fields_rejected_at_every_object_level(self):
        for fn in (lambda m: m.update(extra=1), lambda m: m["semantic_review"].update(authenticated=True)):
            self.assertInvalid(run(with_manifest(fn)), "UNKNOWN_FIELD")
            self.assertInvalid(run(with_delta(fn)), "UNKNOWN_FIELD")
        self.assertInvalid(run(with_current(lambda c: c.update(authenticated=True))), "UNKNOWN_FIELD")
        self.assertInvalid(run(with_current(lambda c: c.update(live_verified=True))), "UNKNOWN_FIELD")
        pr = jbytes({"idempotency_key": "k", "payload_sha256": "0" * 64, "x": 1})
        self.assertInvalid(run(FILES, prior=pr), "UNKNOWN_FIELD")

    def test_review_flags_cannot_claim_authentication(self):
        for status in ("OWNER_AUTHENTICATED", "AUTHENTICATED", "owner_reviewed", "OWNER_REVIEWED ", ""):
            self.assertInvalid(run(with_manifest(lambda m, s=status: m["semantic_review"].update(status=s))), "INVALID_ENUM_VALUE")
        self.assertInvalid(run(with_delta(lambda d: d["semantic_review"].update(status="OWNER_REVIEWED"))), "INVALID_ENUM_VALUE")
        self.assertInvalid(run(with_manifest(lambda m: m["semantic_review"].update(status=True))), "INVALID_ENUM_VALUE")

    def test_provenance_text_claiming_live_authentication_does_not_change_labels(self):
        f = with_current(lambda c: c.update(provenance="authenticated live provider read, verified current"))
        ev = run(f)
        self.assertEqual(ev.decision, "LOCAL_CAPSULE_CANDIDATE")
        self.assertEqual(ev.capsule["currentness_basis"]["assertion"], "CALLER_SUPPLIED_NOT_LIVE_PROOF")
        self.assertIn("NOT_LIVE_CURRENTNESS_PROOF", ev.decision_document()["limits"])

    def test_path_traversal_and_ambiguity(self):
        bad = {"../adapter.py": "PATH_TRAVERSAL", "/etc/passwd": "PATH_TRAVERSAL", "a/../b.py": "PATH_TRAVERSAL",
               "C:/x.py": "PATH_TRAVERSAL", "~/x.py": "PATH_TRAVERSAL", "..": "PATH_TRAVERSAL",
               "./adapter.py": "PATH_AMBIGUOUS_NORMALIZATION", "a//b.py": "PATH_AMBIGUOUS_NORMALIZATION",
               "a/./b.py": "PATH_AMBIGUOUS_NORMALIZATION", "a\\b.py": "PATH_AMBIGUOUS_NORMALIZATION",
               "adapter.py/": "PATH_AMBIGUOUS_NORMALIZATION", " adapter.py": "PATH_AMBIGUOUS_NORMALIZATION",
               "a%2e%2e/b.py": "PATH_AMBIGUOUS_NORMALIZATION", "a\tb.py": "PATH_AMBIGUOUS_NORMALIZATION",
               "e\u0301.py": "PATH_AMBIGUOUS_NORMALIZATION"}
        for p, code in bad.items():
            self.assertInvalid(run(with_delta(lambda d, p=p: d.update(repair_paths=["adapter.py", p]))), code)
            self.assertInvalid(run(with_manifest(lambda m, p=p: m.update(allowed_paths=["adapter.py", p]))), code)

    def test_case_variant_paths_are_ambiguous_not_silently_accepted_or_widened(self):
        self.assertInvalid(run(with_delta(lambda d: d.update(repair_paths=["Adapter.py"]))), "PATH_AMBIGUOUS_NORMALIZATION")
        self.assertInvalid(run(with_manifest(lambda m: m.update(allowed_paths=["adapter.py", "ADAPTER.PY"]))),
                           "PATH_AMBIGUOUS_NORMALIZATION")
        self.assertInvalid(run(with_delta(lambda d: d.update(repair_paths=["adapter.py", "adapter.py"]))), "DUPLICATE_LIST_ITEM")

    def test_observed_at_must_be_explicit_with_offset(self):
        for v in ("yesterday", "2026-10-05T00:00:00", "", "2026-13-40T00:00:00+00:00"):
            self.assertInvalid(run(with_current(lambda c, v=v: c.update(observed_at=v))), "OBSERVED_AT_UNPARSEABLE")
        self.assertEqual(run(with_current(lambda c: c.update(observed_at="2026-10-05T00:00:00Z"))).decision, "LOCAL_CAPSULE_CANDIDATE")

    def test_wrong_types(self):
        self.assertInvalid(run(with_manifest(lambda m: m.update(actor_ref=7))), "INVALID_STRING")
        self.assertInvalid(run(with_manifest(lambda m: m.update(parent_sha256="ABC"))), "INVALID_SHA256")
        self.assertInvalid(run(with_manifest(lambda m: m.update(parent_sha256=m["parent_sha256"].upper()))), "INVALID_SHA256")
        self.assertInvalid(run(with_delta(lambda d: d.update(required_test_delta=[]))), "EMPTY_OR_WRONG_LIST")
        self.assertInvalid(run(with_delta(lambda d: d.update(idempotency_key=""))), "INVALID_STRING")
        self.assertInvalid(run(with_manifest(lambda m: m.update(parent_revision=""))), "INVALID_STRING")

    def test_idempotency_collision_with_prior_record(self):
        first = run(FILES)
        prior = jbytes({"idempotency_key": "SYN-IDEM-0001", "payload_sha256": first.capsule["payload_sha256"]})
        same = run(FILES, prior=prior)
        self.assertEqual((same.decision, same.idempotency["status"]), ("LOCAL_CAPSULE_CANDIDATE", "PRIOR_RECORD_MATCH"))
        changed = with_delta(lambda d: d.update(finding="changed payload, same key"))
        ev = run(changed, prior=prior)
        self.assertEqual(ev.idempotency["status"], "IDEMPOTENCY_COLLISION")
        self.assertInvalid(ev, "IDEMPOTENCY_COLLISION")
        other_key = with_delta(lambda d: d.update(finding="changed", idempotency_key="SYN-IDEM-0002"))
        self.assertEqual(run(other_key, prior=prior).idempotency["status"], "PRIOR_RECORD_DIFFERENT_KEY")

    def test_no_prior_record_is_stated_not_claimed_as_idempotent(self):
        ev = run(FILES)
        self.assertEqual(ev.idempotency["status"], "NO_PRIOR_RECORD_CHECKED")
        self.assertNotIn("IDEMPOTENT", json.dumps(ev.decision_document()).replace("NO_PRIOR_RECORD_CHECKED", ""))

    def test_malformed_prior_record_is_invalid(self):
        self.assertInvalid(run(FILES, prior=b"{"), "MALFORMED_JSON")
        self.assertInvalid(run(FILES, prior=jbytes({"idempotency_key": "k"})), "MISSING_FIELD")

    def test_invalid_beats_every_other_outcome(self):
        f = with_current(lambda c: c.update(observed_parent_revision="rev-2"))
        f = with_delta(lambda d: d.update(requested_effect_class="LIVE"), f)
        f = dict(f, **{"parent.txt": FILES["parent.txt"] + b"x"})
        ev = run(f)
        self.assertEqual(ev.decision, "INVALID_INPUT")
        self.assertTrue({"PARENT_HASH_MISMATCH", "STALE_PARENT_REVISION", "REQUESTED_EFFECT_CLASS_DIFFERS"} <= set(codes(ev)))


class FullTaskAndStale(unittest.TestCase):
    def assertDecision(self, ev, decision, code):
        self.assertEqual(ev.decision, decision, ev.reasons)
        self.assertIn(code, codes(ev))
        self.assertIsNone(ev.capsule)
        self.assertIsNone(ev.measurement)

    def test_third_repair_path(self):
        ev = run(with_delta(lambda d: d.update(repair_paths=["adapter.py", "test_adapter.py", "other.py"])))
        self.assertDecision(ev, "FULL_TASK_REQUIRED", "REPAIR_PATH_NOT_ALLOWED")
        self.assertIn("other.py", ev.reasons[0]["detail"])

    def test_repair_paths_subset_is_fine(self):
        self.assertEqual(run(with_delta(lambda d: d.update(repair_paths=["adapter.py"]))).decision, "LOCAL_CAPSULE_CANDIDATE")

    def test_every_scope_field_must_match_exactly_even_if_narrower(self):
        cases = {"requested_actor_ref": ("SYN-ACTOR-OTHER", "REQUESTED_ACTOR_REF_DIFFERS"),
                 "requested_effect_class": ("LIVE", "REQUESTED_EFFECT_CLASS_DIFFERS"),
                 "requested_privacy_class": ("SYNTHETIC_PRIVATE", "REQUESTED_PRIVACY_CLASS_DIFFERS"),
                 "requested_live_scope": ("SOME_SCOPE", "REQUESTED_LIVE_SCOPE_DIFFERS"),
                 "requested_authority_class": ("ELEVATED", "REQUESTED_AUTHORITY_CLASS_DIFFERS"),
                 "requested_return_target": ("SYN-RETURN-TARGET-2", "REQUESTED_RETURN_TARGET_DIFFERS")}
        for key, (val, code) in cases.items():
            self.assertDecision(run(with_delta(lambda d, k=key, v=val: d.update({k: v}))), "FULL_TASK_REQUIRED", code)
        # "narrower looking" change: LOCAL_ONLY -> READ_ONLY_LOCAL is still a change
        self.assertDecision(run(with_delta(lambda d: d.update(requested_effect_class="READ_ONLY_LOCAL"))),
                            "FULL_TASK_REQUIRED", "REQUESTED_EFFECT_CLASS_DIFFERS")

    def test_review_missing(self):
        for fn, code in ((lambda m: m["semantic_review"].update(status="UNREVIEWED"), "PARENT_REVIEW_MISSING"),
                         (lambda m: m["semantic_review"].update(receipt_ref=""), "PARENT_REVIEW_MISSING"),
                         (lambda m: m["semantic_review"].update(receipt_ref="   "), "PARENT_REVIEW_MISSING")):
            self.assertDecision(run(with_manifest(fn)), "FULL_TASK_REQUIRED", code)
        for fn in (lambda d: d["semantic_review"].update(status="UNREVIEWED"),
                   lambda d: d["semantic_review"].update(receipt_ref="")):
            self.assertDecision(run(with_delta(fn)), "FULL_TASK_REQUIRED", "VERIFIER_REVIEW_MISSING")

    def test_stale_parent_revision_and_source_head(self):
        self.assertDecision(run(with_current(lambda c: c.update(observed_parent_revision="rev-4"))),
                            "HOLD_STALE", "STALE_PARENT_REVISION")
        self.assertDecision(run(with_current(lambda c: c.update(observed_source_head="f" * 40))),
                            "HOLD_STALE", "STALE_SOURCE_HEAD")
        ev = run(with_current(lambda c: c.update(observed_parent_revision="rev-4", observed_source_head="f" * 40)))
        self.assertEqual(sorted(codes(ev)), ["STALE_PARENT_REVISION", "STALE_SOURCE_HEAD"])

    def test_stale_and_scope_expansion_are_typed_handoffs_without_capsule(self):
        stale = run(with_current(lambda c: c.update(observed_parent_revision="stale-revision")))
        self.assertEqual(stale.decision, "HOLD_STALE")
        self.assertIsNone(stale.capsule)
        self.assertEqual(stale.decision_document()["authority"], "NONE")
        expanded = run(with_delta(lambda d: d.update(requested_effect_class="LIVE")))
        self.assertEqual(expanded.decision, "FULL_TASK_REQUIRED")
        self.assertIsNone(expanded.capsule)
        self.assertEqual(expanded.decision_document()["authority"], "NONE")

    def test_stale_takes_precedence_over_full_task_and_keeps_both_reasons(self):
        f = with_current(lambda c: c.update(observed_source_head="f" * 40))
        f = with_delta(lambda d: d.update(requested_effect_class="LIVE"), f)
        ev = run(f)
        self.assertEqual(ev.decision, "HOLD_STALE")
        self.assertEqual(set(codes(ev)), {"STALE_SOURCE_HEAD", "REQUESTED_EFFECT_CLASS_DIFFERS"})

    def test_matching_currentness_strings_are_not_proof(self):
        ev = run(FILES)
        self.assertEqual(ev.decision_document()["currentness_basis"], "CALLER_SUPPLIED_NOT_LIVE_PROOF")
        self.assertEqual(ev.decision_document()["validation"], "STRUCTURAL_ONLY")


class Reconstruction(unittest.TestCase):
    def setUp(self):
        self.cap = core.capsule_file_bytes(run(FILES).capsule)

    def test_roundtrip_returns_exact_bytes(self):
        decision, reasons, payload = core.reconstruct(self.cap, FILES["parent.txt"], FILES["verifier.txt"])
        self.assertEqual((decision, reasons), ("RECONSTRUCTED_HASH_VERIFIED", []))
        self.assertEqual(payload["capsule"], run(FILES).capsule)
        self.assertEqual(payload["semantic_completeness"], "NOT_ASSERTED_OWNER_REVIEW_REQUIRED")

    def test_missing_parent_or_verifier(self):
        d, r, p = core.reconstruct(self.cap, None, FILES["verifier.txt"])
        self.assertEqual((d, p, [x["code"] for x in r]), ("INVALID_INPUT", None, ["MISSING_PARENT_BYTES"]))
        d, r, p = core.reconstruct(self.cap, FILES["parent.txt"], None)
        self.assertEqual([x["code"] for x in r], ["MISSING_VERIFIER_BYTES"])

    def test_changed_bytes_and_tampered_capsule(self):
        d, r, _ = core.reconstruct(self.cap, FILES["parent.txt"] + b" ", FILES["verifier.txt"])
        self.assertEqual([x["code"] for x in r], ["PARENT_HASH_MISMATCH"])
        d, r, _ = core.reconstruct(self.cap, FILES["parent.txt"], FILES["verifier.txt"][:-1])
        self.assertEqual([x["code"] for x in r], ["VERIFIER_HASH_MISMATCH"])
        tampered = json.loads(self.cap)
        tampered["finding"] = "tampered"
        d, r, _ = core.reconstruct(core.canonical_bytes(tampered), FILES["parent.txt"], FILES["verifier.txt"])
        self.assertEqual((d, [x["code"] for x in r]), ("INVALID_INPUT", ["CAPSULE_PAYLOAD_HASH_MISMATCH"]))

    def test_capsule_must_keep_its_constant_labels(self):
        for key, val in (("authority", "OWNER"), ("validation", "SEMANTIC"), ("status", "SENT")):
            cap = json.loads(self.cap)
            cap[key] = val
            cap["payload_sha256"] = core.payload_hash(cap)
            d, r, _ = core.reconstruct(core.canonical_bytes(cap), FILES["parent.txt"], FILES["verifier.txt"])
            self.assertEqual((d, [x["code"] for x in r]), ("INVALID_INPUT", ["CONSTANT_MISMATCH"]), key)
        cap = json.loads(self.cap)
        cap["reviews"]["parent"]["assertion"] = "AUTHENTICATED"
        cap["payload_sha256"] = core.payload_hash(cap)
        d, r, _ = core.reconstruct(core.canonical_bytes(cap), FILES["parent.txt"], FILES["verifier.txt"])
        self.assertEqual([x["code"] for x in r], ["CONSTANT_MISMATCH"])
        cap = json.loads(self.cap)
        cap["bind"] = True
        d, r, _ = core.reconstruct(core.canonical_bytes(cap), FILES["parent.txt"], FILES["verifier.txt"])
        self.assertIn("UNKNOWN_FIELD", [x["code"] for x in r])

    def test_cli_reconstruct_writes_exact_bytes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            for n, b in (("capsule.json", self.cap), ("parent.txt", FILES["parent.txt"]), ("verifier.txt", FILES["verifier.txt"])):
                with open(os.path.join(td, n), "wb") as fh:
                    fh.write(b)
            out = os.path.join(td, "rec")
            args = ["reconstruct", "--capsule", os.path.join(td, "capsule.json"), "--parent", os.path.join(td, "parent.txt"),
                    "--verifier", os.path.join(td, "verifier.txt"), "--out-dir", out]
            self.assertEqual(cli.main(args, io.BytesIO(), io.BytesIO()), 0)
            self.assertEqual(rb(os.path.join(out, "parent.txt")), FILES["parent.txt"])
            self.assertEqual(rb(os.path.join(out, "verifier.txt")), FILES["verifier.txt"])
            rec = json.loads(rb(os.path.join(out, "reconstruction.json")))
            self.assertEqual(rec["capsule"]["payload_sha256"], run(FILES).capsule["payload_sha256"])
            err = io.BytesIO()
            self.assertEqual(cli.main(args, io.BytesIO(), err), 2)
            self.assertEqual(json.loads(err.getvalue())["error_code"], "USAGE_OR_OUTPUT_REFUSED")

    def test_multibyte_text_roundtrips_exactly(self):
        parent = ("Overskrift \u00e6\u00f8\u00e5 \u2713 \U0001F600\r\nlinje2\n").encode("utf-8")
        f = refresh_hashes(fx(parent__txt=parent))
        ev = run(f)
        self.assertEqual(ev.decision, "LOCAL_CAPSULE_CANDIDATE")
        self.assertEqual(ev.capsule["parent"]["bytes"], len(parent))
        d, r, payload = core.reconstruct(core.capsule_file_bytes(ev.capsule), parent, f["verifier.txt"])
        self.assertEqual(d, "RECONSTRUCTED_HASH_VERIFIED")
        self.assertEqual(ev.measurement["sizes"]["parent_txt"], {"bytes": len(parent), "characters": len(parent.decode("utf-8"))})
        self.assertGreater(len(parent), len(parent.decode("utf-8")))


class Measurement(unittest.TestCase):
    def test_sizes_are_separate_and_correct(self):
        m = run(FILES).measurement
        self.assertEqual(m["sizes"]["parent_txt"], {"bytes": 3640, "characters": 3640})
        self.assertEqual(m["sizes"]["verifier_txt"], {"bytes": 1500, "characters": 1500})
        self.assertEqual(m["sizes"]["full_continuation_txt"], {"bytes": 2876, "characters": 2876})
        self.assertEqual(m["sizes"]["capsule_json"]["bytes"], len(core.capsule_file_bytes(run(FILES).capsule)))

    def test_reuse_assumption_scenarios_and_file_reads(self):
        m = run(FILES).measurement
        cap = m["sizes"]["capsule_json"]["bytes"]
        s = m["scenarios"]
        self.assertEqual(s["REREAD_ALL"]["combined"]["bytes"], cap + 3640 + 1500)
        self.assertEqual(s["PARENT_ALREADY_HELD"]["combined"]["bytes"], cap + 1500)
        self.assertEqual(s["BOTH_ALREADY_HELD"]["combined"]["bytes"], cap)
        self.assertEqual([s[k]["recipient_files_read"] for k in ("REREAD_ALL", "PARENT_ALREADY_HELD", "BOTH_ALREADY_HELD")], [3, 2, 1])
        self.assertEqual(s["REREAD_ALL"]["reread_parent_and_verifier"]["bytes"], 3640 + 1500)
        self.assertEqual(m["baseline"]["recipient_files_read"], 1)

    def test_no_observed_size_advantage_is_reported_and_does_not_change_validation(self):
        ev = run(FILES, reuse="REREAD_ALL")
        self.assertEqual(ev.measurement["comparison"]["result"], "NO_OBSERVED_SIZE_ADVANTAGE")
        self.assertGreater(ev.measurement["comparison"]["delta_bytes"], 0)
        self.assertEqual(ev.decision, "LOCAL_CAPSULE_CANDIDATE")
        self.assertEqual(run(FILES, reuse="PARENT_ALREADY_HELD").measurement["comparison"]["result"], "NO_OBSERVED_SIZE_ADVANTAGE")
        adv = run(FILES, reuse="BOTH_ALREADY_HELD")
        self.assertEqual(adv.measurement["comparison"]["result"], "OBSERVED_SIZE_ADVANTAGE")
        self.assertEqual(adv.decision, "LOCAL_CAPSULE_CANDIDATE")

    def test_missing_baseline_is_reported_not_guessed(self):
        m = run(FILES, full=False).measurement
        self.assertEqual(m["missing_inputs"], ["full_continuation.txt"])
        self.assertEqual(m["comparison"]["result"], "NOT_MEASURED_BASELINE_ABSENT")
        self.assertIsNone(m["sizes"]["full_continuation_txt"])
        self.assertIsNone(m["comparison"]["delta_bytes"])

    def test_token_cost_latency_effort_and_net_saving_stay_unknown(self):
        m = run(FILES).measurement
        self.assertEqual(m["unknown_until_externally_measured"],
                         {"token_count": "UNKNOWN", "provider_spend": "UNKNOWN", "latency": "UNKNOWN",
                          "human_effort": "UNKNOWN", "net_saving": "UNKNOWN"})
        text = json.dumps(m)
        self.assertIn("neither is a token count", text)
        self.assertNotIn("tokens_saved", text)

    def test_explicitly_named_but_unreadable_optional_inputs_are_invalid(self):
        ev = core.evaluate(FILES["parent.txt"], FILES["parent_manifest.json"], FILES["verifier.txt"],
                           FILES["verifier_delta.json"], FILES["currentness.json"], named_but_absent=("full_continuation.txt",))
        self.assertEqual((ev.decision, codes(ev)), ("INVALID_INPUT", ["ABSENT_INPUT_FILE"]))

    def test_unknown_reuse_assumption_is_rejected(self):
        with self.assertRaises(ValueError):
            run(FILES, reuse="MAGIC")


class CliExitCodes(unittest.TestCase):
    """Raw commands through the real entry point (python3 -B -m bk05_capsule ...)."""

    def cli(self, td, f, extra=()):
        d = os.path.join(td, "in")
        os.makedirs(d, exist_ok=True)
        for n, b in f.items():
            with open(os.path.join(d, n), "wb") as fh:
                fh.write(b)
        args = [sys.executable, "-B", "-m", "bk05_capsule", "build"]
        for flag, n in (("--parent", "parent.txt"), ("--parent-manifest", "parent_manifest.json"), ("--verifier", "verifier.txt"),
                        ("--verifier-delta", "verifier_delta.json"), ("--currentness", "currentness.json")):
            args += [flag, os.path.join(d, n)]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(ROOT, "src")
        p = subprocess.run(args + list(extra), capture_output=True, cwd=ROOT, env=env, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_exit_codes_per_outcome(self):
        cases = [
            (FILES, 0, "LOCAL_CAPSULE_CANDIDATE"),
            (with_delta(lambda d: d.update(repair_paths=["adapter.py", "x.py"])), 10, "FULL_TASK_REQUIRED"),
            (with_delta(lambda d: d.update(requested_effect_class="LIVE")), 10, "FULL_TASK_REQUIRED"),
            (with_manifest(lambda m: m["semantic_review"].update(status="UNREVIEWED")), 10, "FULL_TASK_REQUIRED"),
            (with_manifest(lambda m: m.update(return_target="SYN-RETURN-TARGET-2")), 10, "FULL_TASK_REQUIRED"),
            (with_current(lambda c: c.update(observed_parent_revision="rev-9")), 11, "HOLD_STALE"),
            (with_current(lambda c: c.update(observed_source_head="0" * 40)), 11, "HOLD_STALE"),
            (fx(parent__txt=FILES["parent.txt"] + b"x"), 12, "INVALID_INPUT"),
            (fx(verifier__txt=FILES["verifier.txt"] + b"x"), 12, "INVALID_INPUT"),
            (with_manifest(lambda m: m.update(stop_edges=[])), 12, "INVALID_INPUT"),
            (fx(parent_manifest__json=FILES["parent_manifest.json"].replace(b'"task_ref"', b'"task_ref": "x", "task_ref"', 1)), 12, "INVALID_INPUT"),
            (with_delta(lambda d: d.update(repair_paths=["../adapter.py"])), 12, "INVALID_INPUT"),
        ]
        for f, code, decision in cases:
            with tempfile.TemporaryDirectory() as td:
                rc, out, err = self.cli(td, f)
            self.assertEqual(rc, code, (decision, out))
            self.assertEqual(json.loads(out)["decision"], decision)
            self.assertEqual(err, b"")

    def test_collision_and_missing_files_exit_12(self):
        with tempfile.TemporaryDirectory() as td:
            prior = os.path.join(td, "prior.json")
            with open(prior, "wb") as fh:
                fh.write(jbytes({"idempotency_key": "SYN-IDEM-0001", "payload_sha256": "0" * 64}))
            rc, out, _ = self.cli(td, FILES, ["--prior-record", prior])
            self.assertEqual((rc, json.loads(out)["reasons"][0]["code"]), (12, "IDEMPOTENCY_COLLISION"))
            rc, out, _ = self.cli(td, FILES, ["--prior-record", os.path.join(td, "nope.json")])
            self.assertEqual((rc, json.loads(out)["reasons"][0]["code"]), (12, "ABSENT_INPUT_FILE"))
        with tempfile.TemporaryDirectory() as td:
            f = {k: v for k, v in FILES.items() if k != "parent.txt"}
            rc, out, _ = self.cli(td, f)
            self.assertEqual((rc, json.loads(out)["reasons"][0]["code"]), (12, "ABSENT_REFERENCED_BYTES"))

    def test_usage_and_internal_failure(self):
        for args in ([], ["frobnicate"], ["reconstruct"], ["build", "--reuse-assumption", "MAGIC"]):
            err = io.BytesIO()
            self.assertEqual(cli.main(args, io.BytesIO(), err), 2)
            self.assertEqual(json.loads(err.getvalue())["exit_code"], 2)
        orig = core.evaluate
        try:
            def boom(*a, **k):
                raise RuntimeError("synthetic")
            core.evaluate = boom
            out, err = io.BytesIO(), io.BytesIO()
            self.assertEqual(cli.main(["build"], out, err), 3)
            self.assertEqual((out.getvalue(), json.loads(err.getvalue())["error_code"]), (b"", "TOOL_FAILURE"))
        finally:
            core.evaluate = orig

    def test_non_candidate_writes_only_decision_json_and_never_a_capsule(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = os.path.join(td, "o")
            rc, _, _ = self.cli(td, with_current(lambda c: c.update(observed_parent_revision="rev-9")), ["--out-dir", out_dir])
            self.assertEqual((rc, os.listdir(out_dir)), (11, ["decision.json"]))

    def test_cli_does_not_write_anywhere_but_out_dir(self):
        with tempfile.TemporaryDirectory() as td:
            before = set(os.listdir(ROOT))
            rc, _, _ = self.cli(td, FILES)
            self.assertEqual(rc, 0)
            self.assertEqual(set(os.listdir(ROOT)), before)  # no __pycache__, no stray output beside the package
            self.assertEqual(os.listdir(td), ["in"])


class StaticBoundary(unittest.TestCase):
    def sources(self):
        for name in ("core.py", "cli.py", "synthetic.py", "__init__.py", "__main__.py"):
            yield name, rb(os.path.join(ROOT, "src", "bk05_capsule", name)).decode("utf-8")

    def test_only_stdlib_offline_imports(self):
        allowed = {"argparse", "datetime", "hashlib", "json", "os", "re", "sys", "unicodedata"}
        for name, src in self.sources():
            mods = set()
            for n in ast.walk(ast.parse(src)):
                if isinstance(n, ast.Import):
                    mods.update(a.name.split(".")[0] for a in n.names)
                elif isinstance(n, ast.ImportFrom) and n.level == 0:
                    mods.add(n.module.split(".")[0])
            self.assertLessEqual(mods, allowed, name)

    def test_no_network_process_clock_or_env_use(self):
        for name, src in self.sources():
            for needle in ("socket", "urllib", "http", "subprocess", "os.system", "environ", "getenv", "time.time",
                           "datetime.now", "utcnow", "random", "uuid"):
                self.assertNotIn(needle, src, "%s uses %s" % (name, needle))

    def test_no_policy_or_authority_minting_words_in_code_paths(self):
        for name, src in self.sources():
            self.assertNotRegex(src, r"(?i)\b(mint|auto.?bind|wake|schedule)\b", name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
