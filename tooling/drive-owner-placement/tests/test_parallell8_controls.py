"""R2 controls for E3 findings F01-F05; standard library and local fixtures only."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


COMPONENT = Path(__file__).resolve().parents[1]
CANDIDATE = COMPONENT.parents[1]
FIXTURES = COMPONENT / "tests" / "fixtures"
sys.path.insert(0, str(COMPONENT))
import normalwriter as nw  # noqa: E402


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


FIXTURE_CONTRACT = load_json(FIXTURES / "fixture-contract.json")
FIXTURE_INPUT = load_json(FIXTURES / "plan-input.json")
OUTBOX = FIXTURE_CONTRACT["placements"]["HUMAN_HANDOFF"]["parent_id"]
COORDINATOR = FIXTURE_CONTRACT["coordinator_parent_id"]
ORIGINAL = FIXTURE_CONTRACT["owner_home"]["url"]


def run_cli(operation, input_path, contract_path=None):
    command = [sys.executable, "-I", "-B", str(COMPONENT / "normalwriter.py"), operation]
    if contract_path is not None:
        command += ["--contract", str(contract_path)]
    command += ["--input", str(input_path)]
    return subprocess.run(command, cwd=CANDIDATE, capture_output=True, text=True, check=False)


def parent_metadata():
    return load_json(FIXTURES / "outbox-parent-metadata.json")


def plan_args(**updates):
    result = dict(FIXTURE_INPUT)
    result["parent_metadata"] = parent_metadata()
    result.update(updates)
    return result


class R2FocusedControls(unittest.TestCase):
    def test_recipe_cli_plan_runs_from_candidate_package(self):
        result = run_cli(
            "plan",
            FIXTURES / "plan-input.json",
            FIXTURES / "fixture-contract.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["status"], "PLAN_ONLY")
        self.assertEqual(plan["authority"], "NONE")
        self.assertEqual(plan["arguments"]["parent_folder_id"], OUTBOX)
        self.assertEqual(plan["arguments"]["mime_type"], "text/markdown")
        self.assertEqual(plan["original_home_pointer"], ORIGINAL)
        self.assertEqual(plan["unknown_outcome"], "HOLD_RECONCILE_SAME_LOGICAL_ARTIFACT_NO_RETRY")

    def test_current_contract_retains_exact_case_and_outbox_binding(self):
        current = load_json(COMPONENT / "placement-contract.json")
        self.assertEqual(nw._contract(current)["parent_id"], "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc")
        self.assertEqual(current["coordinator_parent_id"], "165VAUNZ9bdUuI9JOgeUr34Mrg0OY7-A8")
        self.assertEqual(current["placements"]["HUMAN_HANDOFF"]["mime_type"], "text/markdown")

    def test_current_contract_cli_uses_exact_case_binding_without_provider_read(self):
        current = load_json(COMPONENT / "placement-contract.json")
        placement = current["placements"]["HUMAN_HANDOFF"]
        with tempfile.TemporaryDirectory() as temp:
            payload = Path(temp) / "current-case.md"
            payload.write_text(f"Original: {current['owner_home']['url']}\n", encoding="utf-8")
            args = {
                "owner": current["owner"],
                "case": current["case_id"],
                "fileclass": "HUMAN_HANDOFF",
                "local_path": str(payload),
                "parent_metadata": {
                    "id": placement["parent_id"],
                    "mime_type": "application/vnd.google-apps.folder",
                    "parent_ids": [current["coordinator_parent_id"]],
                    "permissions": [{
                        "type": "user",
                        "role": "owner",
                        "emailAddress": current["owner_basis"]["expected_owner_email"],
                    }],
                    "source_visibility_status": "permission_metadata_available",
                },
                "original_home_pointer": current["owner_home"]["url"],
            }
            input_path = Path(temp) / "current-case-input.json"
            input_path.write_text(json.dumps(args), encoding="utf-8")
            result = run_cli("plan", input_path, COMPONENT / "placement-contract.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["arguments"]["parent_folder_id"], "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc")
        self.assertEqual(plan["arguments"]["mime_type"], "text/markdown")

    def test_both_matching_metadata_dialects_are_accepted(self):
        normalized = parent_metadata()
        native = {
            "id": normalized["id"],
            "mimeType": normalized["mime_type"],
            "parents": normalized["parent_ids"],
            "permissions": normalized["permissions"],
            "source_visibility_status": normalized["source_visibility_status"],
        }
        self.assertEqual(nw.plan(**plan_args(contract=FIXTURE_CONTRACT))["status"], "PLAN_ONLY")
        self.assertEqual(nw.plan(**plan_args(contract=FIXTURE_CONTRACT, parent_metadata=native))["status"], "PLAN_ONLY")

    def test_matching_dual_spellings_are_accepted(self):
        metadata = parent_metadata()
        metadata.update({
            "mimeType": metadata["mime_type"],
            "parents": list(reversed(metadata["parent_ids"])),
            "url": "https://drive.google.com/drive/folders/14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc",
            "webViewLink": "https://drive.google.com/drive/folders/14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc",
        })
        metadata["permissions"][0]["email_address"] = "TEST-OWNER@example.test"
        self.assertEqual(nw.plan(**plan_args(contract=FIXTURE_CONTRACT, parent_metadata=metadata))["status"], "PLAN_ONLY")

    def test_conflicting_parent_dialects_are_typed_hold(self):
        metadata = parent_metadata()
        metadata["parents"] = ["different-parent-12345"]
        with self.assertRaisesRegex(nw.PlacementGap, "HOLD_METADATA_ALIAS_CONFLICT"):
            nw.plan(**plan_args(contract=FIXTURE_CONTRACT, parent_metadata=metadata))

    def test_conflicting_mime_dialects_are_typed_hold(self):
        metadata = parent_metadata()
        metadata["mimeType"] = "text/plain"
        with self.assertRaisesRegex(nw.PlacementGap, "HOLD_METADATA_ALIAS_CONFLICT"):
            nw.plan(**plan_args(contract=FIXTURE_CONTRACT, parent_metadata=metadata))

    def test_conflicting_link_dialects_are_typed_hold(self):
        file_id = "review-file-id-12345"
        metadata = {
            "id": file_id,
            "mime_type": "text/markdown",
            "parent_ids": [OUTBOX],
            "url": f"https://drive.google.com/file/d/{file_id}/view",
            "webViewLink": "https://docs.google.com/document/d/another-id/edit",
        }
        with self.assertRaisesRegex(nw.PlacementGap, "HOLD_METADATA_ALIAS_CONFLICT"):
            nw.verify(file_id, OUTBOX, metadata, "text/markdown")

    def test_conflicting_permission_email_dialects_are_typed_hold(self):
        metadata = parent_metadata()
        metadata["permissions"][0]["email_address"] = "different-owner@example.test"
        with self.assertRaisesRegex(nw.PlacementGap, "HOLD_METADATA_ALIAS_CONFLICT"):
            nw.plan(**plan_args(contract=FIXTURE_CONTRACT, parent_metadata=metadata))

    def test_malformed_contract_url_cli_returns_typed_hold_and_exit_two(self):
        with tempfile.TemporaryDirectory() as temp:
            contract = copy.deepcopy(FIXTURE_CONTRACT)
            contract["owner_home"]["url"] = "https://[malformed"
            contract_path = Path(temp) / "bad-contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            result = run_cli("plan", FIXTURES / "plan-input.json", contract_path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout), {"status": "HOLD_MALFORMED_CONTRACT", "effect": "NONE"})
        self.assertEqual(result.stderr, "")

    def test_missing_owner_visibility_cli_returns_typed_hold_and_exit_two(self):
        with tempfile.TemporaryDirectory() as temp:
            args = plan_args(contract=FIXTURE_CONTRACT)
            args["parent_metadata"]["source_visibility_status"] = "permission_metadata_unavailable"
            input_path = Path(temp) / "unknown-owner.json"
            input_path.write_text(json.dumps(args), encoding="utf-8")
            result = run_cli("plan", input_path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout), {"status": "HOLD_OWNER_BASIS_UNKNOWN", "effect": "NONE"})
        self.assertEqual(result.stderr, "")

    def test_same_id_recovery_is_plan_only_and_never_retries(self):
        file_id = "review-file-id-12345"
        wrong_parent = "wrong-parent-12345"
        observed = {
            "id": file_id,
            "mime_type": "text/markdown",
            "parent_ids": [wrong_parent],
        }
        basis = {
            "status": "OWNER_APPROVED_CURRENT_PLACEMENT",
            "owner": FIXTURE_CONTRACT["owner"],
            "case_id": FIXTURE_CONTRACT["case_id"],
            "file_id": file_id,
            "add_parent_id": OUTBOX,
            "remove_parent_ids": [wrong_parent],
        }
        result = nw.repair(
            file_id, OUTBOX, observed, FIXTURE_CONTRACT,
            FIXTURE_CONTRACT["owner"], FIXTURE_CONTRACT["case_id"], "HUMAN_HANDOFF",
            parent_metadata(), basis,
        )
        self.assertEqual(result["status"], "PLAN_ONLY")
        self.assertEqual(result["arguments"]["fileId"], file_id)
        self.assertEqual(result["unknown_outcome"], "HOLD_RECONCILE_SAME_FILE_ID_NO_RETRY")

    def test_recipe_and_every_source_file_are_inside_mapped_component(self):
        path_map = load_json(COMPONENT / "SOURCE-PATH-MAP.json")
        listed = {entry["candidate_relative"].removeprefix("candidate/") for entry in path_map["mapping"]}
        actual = {path.relative_to(CANDIDATE).as_posix() for path in COMPONENT.rglob("*") if path.is_file()}
        self.assertEqual(actual, listed)
        self.assertTrue((COMPONENT / "NORMAL-CONSUMER-RECIPE.md").is_file())
        recipe = (COMPONENT / "NORMAL-CONSUMER-RECIPE.md").read_text(encoding="utf-8")
        for required in (
            "--contract", "--input", "14SGi2OgU1DwzhzgsvgIm3cb-Z6ZWtRDc", "text/markdown",
            "HOLD", "exit code 2", "unknown", "same file ID", "content readback",
        ):
            self.assertIn(required, recipe)


if __name__ == "__main__":
    unittest.main()
