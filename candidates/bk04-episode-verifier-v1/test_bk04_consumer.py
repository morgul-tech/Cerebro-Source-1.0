"""C1162 focused consumer delta. Synthetic evidence never proves PM authority."""
import hashlib
import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
