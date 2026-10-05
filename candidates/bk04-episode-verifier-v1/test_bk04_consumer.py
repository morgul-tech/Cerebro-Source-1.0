"""C1162 focused consumer delta. Synthetic evidence never proves PM authority."""
import hashlib
import json
import unittest
import copy
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import bk04_verify as v
from test_bk04_rev3 import fixture, episode, links, boundary


def complete_carry():
    doc = fixture("B")
    ep = episode("author", 301, 901)
    cut = ep["source_cut"]
    ref = ep["admission"]["source_ref"]
    doc.update(source_cut=cut, owner_projection_cut=cut)
    doc["cases"] = [{"case_id": "SYNTHETIC-CARRY", "shape": "CI_PLUS_LOCAL_LIMIT",
        "episodes": [ep], "evidence_links": links(ep),
        "boundary_assertions": [boundary("LIVE_EFFECT", "NO_EFFECT", 902, cut, "SYNTHETIC", "LOCAL")],
        "ci_evidence": {"carried_to": [{"target": ep["episode_id"], "causal_basis_equal": True, "source_ref": ref}]},
        "publication_orders": [{"head": "b" * 40, "source_ref": ref}],
        "coverage_assertions": [{"predicate": "PUBLICATION_ORDER", "complete_for_scope": True,
             "query_or_owner_read_ref": "SYNTHETIC-OWNER-READ", "cut": cut, "source_ref": ref}]}]
    return doc


class ConsumerDelta(unittest.TestCase):
    def test_complete_typed_carry_is_structural_only_without_qualified_owner_basis(self):
        doc = complete_carry()
        result = v.classify_document(doc, v.REV3_INPUT_SHA256)
        self.assertEqual(result["structural_overall_status"], "PASS")
        self.assertEqual(result["overall_status"], "UNKNOWN")
        self.assertEqual(result["authority"], "NONE")
        self.assertEqual(result["input_acceptance"], "UNQUALIFIED_INPUT")

    def test_raw_pin_verified_before_parse_and_no_caller_approval(self):
        raw = json.dumps(complete_carry()).encode()
        digest = hashlib.sha256(raw).hexdigest()
        with patch.object(v, "REV3_INPUT_SHA256", digest):
            doc, sha = v.load_consumer_input(raw)
            self.assertEqual(v.classify_document(doc, sha)["input_acceptance"], "HASH_BOUND_X2_DRAFT")
            doc["owner_approved_rev3_digest"] = sha
            out = v.classify_document(doc, sha)
            self.assertEqual(out["input_acceptance"], "UNQUALIFIED_INPUT")
            self.assertEqual(out["overall_status"], "UNKNOWN")
            with self.assertRaises(v.InputError):
                v.load_consumer_input(raw + b" ")

    def test_missing_forged_admission_and_carry_never_qualify(self):
        for mutation in ("admission", "carry", "cut", "target"):
            doc = complete_carry(); c = doc["cases"][0]
            if mutation == "admission":
                c["episodes"][0]["admission"]["status"] = "OWNER_APPROVED_PROSE"
            elif mutation == "carry":
                c["ci_evidence"]["carried_to"] = []
            elif mutation == "cut":
                c["coverage_assertions"][0]["cut"] = "PM_PRINCIPAL_CHANNEL:9999"
            else:
                c["ci_evidence"]["carried_to"][0]["target"] = {"fake": "target"}
            result = v.classify_document(doc, v.REV3_INPUT_SHA256)
            self.assertNotEqual(result["overall_status"], "PASS")
            self.assertEqual(result["authority"], "NONE")
        doc = complete_carry(); del doc["cases"][0]["episodes"][0]["admission"]
        self.assertNotEqual(v.classify_document(doc, "synthetic")["overall_status"], "PASS")

    def test_open_case_cannot_claim_empty_publication_complete(self):
        doc = complete_carry(); c = doc["cases"][0]
        c.update(episodes=[], evidence_links=[], publication_orders=[], coverage_assertions=[])
        result = v.classify_document(doc, "synthetic")
        self.assertEqual(result["structural_overall_status"], "UNKNOWN")
        self.assertEqual(result["overall_status"], "UNKNOWN")

    def test_rev2_original_gate_and_cli_route_preserved(self):
        raw = b'{"snapshot_id":"SYNTHETIC", "revision":"2.0", "source_cut":"PM_PRINCIPAL_CHANNEL:1", "cases":[{}]}'
        doc, sha = v.load_verified(raw, hashlib.sha256(raw).hexdigest(), len(raw))
        result = v.classify_document(doc, sha)
        self.assertEqual(result["input_revision"], "2.0")
        self.assertNotIn("qualification", result)
        with patch.object(v, "load_verified", return_value=(doc, sha)) as gate:
            self.assertEqual(v.load_consumer_input(raw), (doc, sha))
            gate.assert_called_once_with(raw)
        with self.assertRaises(v.InputError):
            v.load_consumer_input(b"{}")


def owner_fixture(revision="1.0", row=100):
    def ref(n):
        return {"sheet": "PM_PRINCIPAL_CHANNEL", "row": n,
                "message_id": "SYNTHETIC-%s" % n, "record_sha256": "%064x" % n}
    cut = ref(row)
    records = []
    for i, active in enumerate((True, False)):
        records.append({"task_id": "P%d" % (10+i), "claim_id": "C%d" % (20+i),
            "actor": "SYNTHETIC-X%d" % i, "original_sha256": "%064x" % (30+i), "original_bytes": 10,
            "original_source": ref(row-10), "actor_start": ref(row-9+i),
            "terminal": None if active else ref(row-5), "admission": None if active else ref(row-4),
            "owner_corroboration": cut,
            "record_status": "ACTIVE_SAME_CLAIM_SAME_ORIGINAL" if active else "READ_COMPLETE_ADMITTED_NO_EFFECT",
            "typed_carry": {"kind": "CONTINUE_SAME_TASK_PENDING_OAUTH_AND_X1_TERMINAL" if active else "SEPARATE_PROVIDER_ACTION_PENDING",
                "causal_equality": "SAME_CLAIM_TASK_AND_ORIGINAL_HASH_CORROBORATED_BY_OWNER" if active else "READ_RECEIPT_IS_NOT_PROVIDER_EXECUTION",
                "provider_effect": "UNKNOWN", "publication_status": "NOT_EXECUTED_AT_OWNER_CUT",
                "global_publication_completeness": "UNKNOWN",
                "provider_action_source": ref(row-3) if active else {"sheet": "PM_PRINCIPAL_CHANNEL",
                    "message_id": "SYNTHETIC-ACTION", "corroborated_at": cut}}})
    return {"schema": "x2-owner-carry-basis-v1", "artifact_id": "SYNTHETIC-"+revision,
            "revision": revision, "owner_source_cut": cut, "integration_source": ref(row+1),
            "closed_prior_basis": {"immutable_sha256": "f"*64}, "records": records,
            "coverage": {"semantic_source_approval": "NOT_GRANTED", "complete_for_global_publication_scope": "UNKNOWN"}}


def owner_binding(doc):
    raw = json.dumps(doc).encode()
    return raw, {"schema": "bk04-owner-input-binding-v1",
        "publication_ref": {**doc["integration_source"], "row": doc["integration_source"]["row"]+1},
        "source": {"file_id": "SYNTHETIC", "provider_revision": "SYNTHETIC-"+doc["revision"]},
        "expected": {"schema": doc["schema"], "artifact_id": doc["artifact_id"], "revision": doc["revision"],
                     "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw)},
        "currentness": {k: doc[k] for k in ("owner_source_cut", "integration_source")},
        "provenance": v.owner_projection(doc)}


class NormalOwnerContract(unittest.TestCase):
    def test_code_complete_admitted_retains_matching_active_owner_proof_carry(self):
        # C1169: genuine rev1.1 shape; synthetic refs do not authenticate PM.
        doc = owner_fixture("1.1")
        doc["integration_source"] = doc["owner_source_cut"]
        active, code = doc["records"]
        code.update(actor="X1_SYNTHETIC", record_status="CODE_COMPLETE_ADMITTED_NO_EFFECT")
        code["typed_carry"].update(causal_equality="CODE_RECEIPT_IS_NOT_PROVIDER_EXECUTION",
            live_host_read="UNKNOWN", code_integration={"status": "MERGED_DEFAULT_OFF",
            "head": "a"*40, "merge": "b"*40, "source_ref": doc["integration_source"]})
        active["typed_carry"].update(kind="CONTINUE_SAME_TASK_PENDING_OAUTH_AND_OWNER_PROOF",
            x1_dependency_status="CODE_COMPLETE_ADMITTED", x1_terminal=code["terminal"],
            x1_admission=code["admission"])
        raw, binding = owner_binding(doc)
        parsed, digest = v.load_consumer_input(raw, binding)
        result = v.classify_document(parsed, digest)
        self.assertEqual(result["input_acceptance"], "HASH_BOUND_OWNER_FACTS")
        self.assertEqual(result["structural_overall_status"], "PASS")
        self.assertEqual((result["overall_status"], result["authority"]), ("UNKNOWN", "NONE"))
        self.assertEqual(result["records"][1]["record_status"], "CODE_COMPLETE_ADMITTED_NO_EFFECT")
        self.assertIsNone(result["records"][0]["terminal"])
        self.assertIsNone(result["records"][0]["admission"])
        self.assertTrue(all(r["provider_action_status"] == "OPEN" for r in result["records"]))
        for change in ("missing_admission", "wrong_order", "mismatched_carry", "stale_carry", "missing_dependency",
                       "false_read_label", "live_read", "live_merge", "wrong_integration", "false_closed_active",
                       "provider_effect", "original_binding"):
            altered = copy.deepcopy(doc); a, c = altered["records"]
            if change == "missing_admission": c["admission"] = None
            elif change == "wrong_order": c["admission"] = c["actor_start"]
            elif change == "mismatched_carry": a["typed_carry"]["x1_admission"] = a["actor_start"]
            elif change == "stale_carry": a["typed_carry"]["kind"] = "CONTINUE_SAME_TASK_PENDING_OAUTH_AND_X1_TERMINAL"
            elif change == "missing_dependency": altered["records"].pop()
            elif change == "false_read_label": c["typed_carry"]["causal_equality"] = "READ_RECEIPT_IS_NOT_PROVIDER_EXECUTION"
            elif change == "live_read": c["typed_carry"]["live_host_read"] = "PASS"
            elif change == "live_merge": c["typed_carry"]["code_integration"]["status"] = "MERGED_LIVE"
            elif change == "wrong_integration": c["typed_carry"]["code_integration"]["source_ref"] = c["admission"]
            elif change == "false_closed_active": a["admission"] = c["admission"]
            elif change == "provider_effect": a["typed_carry"]["provider_effect"] = "PASS"
            else: c["original_sha256"] = "e"*64
            changed_raw, changed_binding = owner_binding(altered)
            if change == "original_binding": changed_binding["provenance"] = binding["provenance"]
            with self.subTest(change=change), self.assertRaises(v.InputError):
                v.load_consumer_input(changed_raw, changed_binding)

    def test_two_successive_snapshots_same_contract_no_digest_code_change(self):
        digests = []
        for doc in (owner_fixture(), owner_fixture("2.0", 200)):
            raw, binding = owner_binding(doc)
            parsed, digest = v.load_consumer_input(raw, binding)
            result = v.classify_document(parsed, digest)
            digests.append(digest)
            self.assertEqual(result["input_acceptance"], "HASH_BOUND_OWNER_FACTS")
            self.assertEqual(result["structural_overall_status"], "PASS")
            self.assertEqual(result["overall_status"], "UNKNOWN")
            self.assertEqual(result["authority"], "NONE")
            self.assertTrue(all(r["provider_action_status"] == "OPEN" for r in result["records"]))
            self.assertIsNone(result["records"][0]["terminal"])
            self.assertNotIn("episodes", result)
        self.assertNotEqual(*digests)

    def test_wrong_bytes_rejected_before_parse(self):
        raw, binding = owner_binding(owner_fixture())
        with patch.object(v.json, "loads", side_effect=AssertionError("must not parse")):
            with self.assertRaises(v.InputError) as caught:
                v.load_consumer_input(raw+b" ", binding)
        self.assertEqual(caught.exception.code, "DIGEST_MISMATCH")

    def test_wrong_revision_original_or_provenance_rejected(self):
        raw, binding = owner_binding(owner_fixture())
        for key in ("revision", "original", "carry", "cut"):
            b = copy.deepcopy(binding)
            if key == "revision": b["expected"]["revision"] = "STALE"
            elif key == "original": b["provenance"]["records"][0]["original_sha256"] = "a"*64
            elif key == "carry": b["provenance"]["records"][0]["typed_carry"]["kind"] = "CLOSED"
            else: b["currentness"]["owner_source_cut"]["row"] += 1
            with self.subTest(key=key), self.assertRaises(v.InputError):
                v.load_consumer_input(raw, b)

    def test_invalid_facts_even_matching_hash_cannot_close_pending(self):
        for change in ("terminal", "order", "action", "effect", "original", "duplicate", "approval"):
            doc = owner_fixture()
            if change == "terminal": doc["records"][0]["terminal"] = doc["owner_source_cut"]
            elif change == "order": doc["records"][1]["admission"] = doc["records"][1]["actor_start"]
            elif change == "action": doc["records"][0]["typed_carry"]["provider_action_source"]["row"] = 999
            elif change == "effect": doc["records"][0]["typed_carry"]["provider_effect"] = "PASS"
            elif change == "original": doc["records"][0]["original_bytes"] = True
            elif change == "duplicate": doc["records"][1]["claim_id"] = doc["records"][0]["claim_id"]
            else: doc["coverage"]["semantic_source_approval"] = "APPROVED"
            raw, binding = owner_binding(doc)
            with self.subTest(change=change), self.assertRaises(v.InputError):
                v.load_consumer_input(raw, binding)

    def test_parsed_or_binding_mutation_loses_acceptance(self):
        for target in ("document", "binding"):
            raw, binding = owner_binding(owner_fixture())
            doc, digest = v.load_consumer_input(raw, binding)
            if target == "document": doc["owner_approved"] = True
            else: doc.binding["source"]["provider_revision"] = "FAKE"
            result = v.classify_document(doc, digest)
            self.assertEqual(result["input_acceptance"], "UNQUALIFIED_INPUT")
            self.assertEqual(result["overall_status"], "UNKNOWN")

    def test_plain_document_hash_and_approval_flags_do_not_bind(self):
        doc = owner_fixture(); doc.update(owner_approved=True, authority="PM")
        raw, binding = owner_binding(doc)
        self.assertEqual(v.classify_document(doc, binding["expected"]["raw_sha256"])["input_acceptance"], "UNQUALIFIED_INPUT")
        parsed, sha = v.load_consumer_input(raw, {**binding, "owner_approved": True})
        result = v.classify_document(parsed, sha)
        self.assertEqual(result["authority"], "NONE")
        self.assertEqual(result["overall_status"], "UNKNOWN")
        with self.assertRaises(v.InputError): v.load_consumer_input(b"{}", binding)

    def test_generic_cli_binding_and_duplicate_keys(self):
        raw, binding = owner_binding(owner_fixture())
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder); (p/"input.json").write_bytes(raw)
            (p/"binding.json").write_text(json.dumps(binding), encoding="utf-8")
            out, err = io.BytesIO(), io.BytesIO()
            self.assertEqual(v.main(["--binding", str(p/"binding.json"), str(p/"input.json")], out, err), 0)
            self.assertEqual(json.loads(out.getvalue())["overall_status"], "UNKNOWN")
        bad = raw[:-1]+b',"revision":"duplicate"}'
        binding["expected"].update(raw_sha256=hashlib.sha256(bad).hexdigest(), raw_bytes=len(bad))
        with self.assertRaises(v.InputError): v.load_consumer_input(bad, binding)


if __name__ == "__main__":
    unittest.main()
