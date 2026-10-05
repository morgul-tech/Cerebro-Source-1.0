"""Synthetic offline vectors; no historical PM data or live authority."""
import copy
import unittest

import bk04_verify as verifier
from bk04_rev3 import classify_rev3_case


def source(sheet, row, cut, message):
    return {"sheet": sheet, "row_hint": row, "message_id": message,
            "source_cut": cut}


def episode(role, n, base):
    task, claim, packet, queue = "P%d" % n, "C%d" % n, str(n + 100), "Q%d" % (n + 200)
    cut = "PM_PRINCIPAL_CHANNEL:%d" % (base + 1)
    return {"episode_id": task, "role": role, "task_id": task, "claim_id": claim,
            "claim_row": n + 300, "packet_id": packet, "packet_row": n + 400,
            "queue_id": queue, "queue_row": n + 500, "task_sha256": "a" * 64,
            "source_cut": cut,
            "terminal": {"source_ref": source("PM_PRINCIPAL_CHANNEL", base, cut,
                                           "SYNTHETIC-TERMINAL-%s" % task),
                         "status": "LOCAL_REVIEW", "scope": "LOCAL_ONLY"},
            "admission": {"source_ref": source("PM_PRINCIPAL_CHANNEL", base + 1, cut,
                                            "SYNTHETIC-ADMISSION-%s" % task),
                          "status": "ADMITTED", "scope": "LOCAL_ONLY", "release_count": 1}}


def links(ep):
    task, claim, packet, queue, cut = (ep[k] for k in
                                       ("task_id", "claim_id", "packet_id", "queue_id", "source_cut"))
    facts = (
        ("TASK_UNDER_CLAIM", claim, "WORK_CLAIMS", ep["claim_row"], task + "-" + claim),
        ("PACKET_FOR_TASK", packet, "WORK_PACKETS", ep["packet_row"], task + "-PACKET"),
        ("QUEUE_FOR_PACKET", queue, "READY_QUEUE", ep["queue_row"], queue[1:]),
        ("TERMINAL_FOR_TASK", task, "PM_PRINCIPAL_CHANNEL",
         ep["terminal"]["source_ref"]["row_hint"], "SYNTHETIC-TERMINAL-" + task),
        ("ADMISSION_FOR_TERMINAL", task, "PM_PRINCIPAL_CHANNEL",
         ep["admission"]["source_ref"]["row_hint"], "SYNTHETIC-ADMISSION-" + task),
    )
    return [{"relation": relation, "subject_episode": task,
             "object_episode_or_artifact": target,
             "source_ref": source(sheet, row, cut, message)}
            for relation, target, sheet, row, message in facts]


def boundary(predicate, value, row, cut, label, scope):
    return {"predicate": predicate, "value": value, "scope": scope,
            "as_of_cut": cut,
            "source_ref": source("PM_PRINCIPAL_CHANNEL", row, cut, label)}


def fixture(kind):
    if kind == "B":
        episodes = [episode("builder", 101, 701),
                    episode("distinct_verifier", 102, 703)]
        row, cut, scope = 705, "PM_PRINCIPAL_CHANNEL:706", "LOCAL_B"
        assertions = [boundary("CALLABLE_VERIFIER_PORT", "HOLD", row, cut,
                               "SYNTHETIC-B-HOLD", scope),
                      boundary("PRODUCTION_CAPABILITY", "NOT_PROVEN", row, cut,
                               "SYNTHETIC-B-NOT-PROVEN", scope)]
        relation = "REVIEWED_ARTIFACT"
    else:
        episodes = [episode("author", 201, 801),
                    episode("distinct_verifier", 202, 803)]
        row, cut, scope = 805, "PM_PRINCIPAL_CHANNEL:806", "LOCAL_C"
        assertions = [boundary("DEPLOYED_EFFECT", "NO_EFFECT", row, cut,
                               "SYNTHETIC-C-NO-DEPLOY", scope),
                      boundary("LIVE_EFFECT", "NO_EFFECT", row, cut,
                               "SYNTHETIC-C-NO-LIVE", scope)]
        relation = "REVIEWED_ORIGINAL_AUTHOR_ARTIFACT"
    reviewer, author = episodes[1], episodes[0]
    artifact = {"relation": relation, "source_episode": reviewer["episode_id"],
                "target_episode": author["episode_id"], "artifact_kind": "COMMIT",
                "exact_head_or_digest": "b" * 40,
                "evidence_ref": source("PM_PRINCIPAL_CHANNEL", row - 1, cut,
                                       "SYNTHETIC-REVIEW"), "scope": scope}
    case = {"case_id": kind,
            "shape": "LOCAL_WORK_PLUS_OWNER_HOLD" if kind == "B"
            else "DISTINCT_AUTHOR_VERIFIER",
            "episodes": episodes,
            "evidence_links": [item for ep in episodes for item in links(ep)],
            "boundary_assertions": assertions, "artifact_relations": [artifact]}
    return {"snapshot_id": "BK04-SYNTHETIC-REV3", "revision": "3.0",
            "source_cut": cut, "owner_projection_cut": cut, "supersedes": "SYNTHETIC-REV2",
            "cases": [case]}


def structural(doc):
    return classify_rev3_case(doc["cases"][0], 0, int(doc["source_cut"].split(":", 1)[1]))


class Rev3Rules(unittest.TestCase):
    def test_typed_b_c_are_structural_pass_with_no_authority(self):
        for kind in ("B", "C"):
            result = structural(fixture(kind))
            self.assertEqual(result["status"], "PASS", result)
            self.assertIn("authority NONE", result["scope_limit"])

    def test_b_and_c_contradictions_win_over_missing_prose(self):
        for kind, predicate in (("B", "PRODUCTION_CAPABILITY"),
                                ("C", "LIVE_EFFECT"), ("C", "DEPLOYED_EFFECT")):
            doc = fixture(kind)
            case = doc["cases"][0]
            case["remaining_boundary"] = "unmapped prose"
            case["boundary_assertions"].append(boundary(predicate, "PROVEN",
                case["boundary_assertions"][0]["source_ref"]["row_hint"],
                doc["source_cut"], "SYNTHETIC-CONTRADICTION",
                case["boundary_assertions"][0]["scope"]))
            self.assertEqual(structural(doc)["status"], "CONFLICT")

    def test_unmapped_prose_and_missing_link_are_unknown(self):
        for kind in ("B", "C"):
            doc = fixture(kind)
            doc["cases"][0]["remaining_boundary"] = "production deployed"
            self.assertEqual(structural(doc)["status"], "UNKNOWN")
            doc = fixture(kind)
            doc["cases"][0]["evidence_links"].pop(0)
            self.assertEqual(structural(doc)["status"], "UNKNOWN")

    def test_wrong_claim_or_source_row_is_conflict(self):
        for kind in ("B", "C"):
            doc = fixture(kind)
            case = doc["cases"][0]
            case["episodes"][0]["claim_id"] = "C999"
            case["evidence_links"][0]["object_episode_or_artifact"] = "C999"
            self.assertEqual(structural(doc)["status"], "CONFLICT")
            doc = fixture(kind)
            doc["cases"][0]["evidence_links"][0]["source_ref"]["row_hint"] += 1
            self.assertEqual(structural(doc)["status"], "CONFLICT")

    def test_a_without_carry_and_publication_is_unknown(self):
        doc = fixture("B")
        doc["cases"][0]["shape"] = "CI_PLUS_LOCAL_LIMIT"
        result = structural(doc)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIn("$.cases[0].ci_evidence.carried_to", result["missing_fields"])
        self.assertIn("$.cases[0].publication_orders", result["missing_fields"])

    def test_unapproved_rev3_cannot_return_pass(self):
        result = verifier.classify_document(fixture("B"), "synthetic-only")
        self.assertEqual(result["cases"][0]["status"], "UNKNOWN")
        self.assertEqual(result["authority"], "NONE")

    def test_rev2_digest_gate_stays_closed(self):
        with self.assertRaises(verifier.InputError):
            verifier.load_verified(b"{}")


if __name__ == "__main__":
    unittest.main()
