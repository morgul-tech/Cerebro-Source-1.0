"""Default-off native Docs selection and protected one-attempt dispatch checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "mcp"))
from rom_a_google_docs_ports import FixedGoogleDocsNamedRangeReader  # noqa: E402
from control_resolution_host import BoundControlResolutionHost  # noqa: E402

DOC_ID = "1kg6GQi1UsMtmhMVibXIOKlPJ8_zZP36RcmRY7QBaNoo"
TAB_ID = "t.xr76a4ex96am"
NAME = "CURRENT_X8_DRIVE_MODERNIZATION_RETURN"
RANGE_ID = "kix.low9lkyhy00a"
TEXT = "Hei\nÅ\n"


class Connector:
    def __init__(self):
        self.credential = "credential:existing-google-drive"
        self.subject = "principal:A1"
        self.revision = "revision-one"
        self.first_paragraph = "Hei\n"
        self.allowed = True
        self.read_count = 0
        self.on_read = None

    def credential_ref(self):
        return self.credential

    def authenticated_subject_ref(self):
        return self.subject

    def get_file_metadata(self, *, fileId, fields):
        if not self.allowed:
            raise PermissionError("ACCESS_REVOKED")
        return {"id": fileId, "mime_type": "application/vnd.google-apps.document",
                "source_visibility_status": "permission_metadata_available"}

    def get_document(self, *, document_id):
        self.read_count += 1
        if self.on_read:
            self.on_read(self.read_count)
        if not self.allowed:
            raise PermissionError("ACCESS_REVOKED")
        return {"documentId": document_id, "revisionId": self.revision,
                "tabs": [{"tabId": TAB_ID,
                          "namedRanges": {NAME: {"namedRanges": [
                              {"name": NAME, "namedRangeId": RANGE_ID,
                               "ranges": [{"startIndex": 10, "endIndex": 16}]}]}},
                          "body": {"content": [
                              {"startIndex": 10, "endIndex": 14,
                               "paragraph": {"elements": [{"textRun": {"content": self.first_paragraph}}]}},
                              {"startIndex": 14, "endIndex": 16,
                               "paragraph": {"elements": [{"textRun": {"content": "Å\n"}}]}}]}}]}


class Sender:
    def __init__(self):
        self.calls = []
        self.checked = []

    def validate_selected(self, **binding):
        self.checked.append(binding)
        if binding["recipient_ref"] != "actor:A1" or \
                binding["selection"] != "DOCS_NAMED_RANGE_SELECTED" or \
                binding["task_ref"] != "task:X8" or \
                binding["task_revision"] != "revision-one" or \
                binding["selected_bytes"] != TEXT.encode("utf-8") or \
                binding["selected_sha256"] != hashlib.sha256(TEXT.encode("utf-8")).hexdigest():
            raise ValueError("ROMA_QUEUE_BINDING_MISMATCH")

    def send_selected(self, **binding):
        self.validate_selected(**binding)
        self.calls.append(binding)
        return {"state": "ACCEPTED", "recipient_ref": binding["recipient_ref"],
                "selected_sha256": binding["selected_sha256"],
                "delivery_ref": "queued-message-fixture", "recipient_use": "NOT_OBSERVED"}


class DocsPortTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connector = Connector()
        self.reader = FixedGoogleDocsNamedRangeReader(
            connector=self.connector, credential_ref=self.connector.credential,
            subject_ref=self.connector.subject, document_id=DOC_ID, tab_id=TAB_ID,
            range_name=NAME, named_range_id=RANGE_ID,
            expected_revision_id="revision-one")
        self.sender = Sender()
        self.out = str(Path(self.temp.name) / "return-one")
        self.host = BoundControlResolutionHost(
            persistence_verifier=SimpleNamespace(verify=lambda **_: {}),
            capability_resolver=SimpleNamespace(is_available=lambda **_: False,
                                                executor=lambda **_: None),
            bounded_content_provider=self.reader,
            rom_a_selected_dispatcher=self.sender,
            rom_a_docs_out_dir=Path(self.out))
        self.target = {"content_ref": "gdocs:" + DOC_ID,
                       "selector_kind": "SECTION", "selector": NAME}

    def dispatch(self, **changes):
        args = {"target": self.target,
                "recipient_ref": "actor:A1", "task_ref": "task:X8",
                "task_revision": "revision-one"}
        args.update(changes)
        return self.host.dispatch_rom_a_docs_selection(**args)

    def test_fresh_exact_selector_and_payload_bytes(self):
        selected = self.host.select_bounded_content(self.target)
        self.assertEqual(selected["selected_text"], TEXT)
        self.assertEqual(selected["selected_sha256"], hashlib.sha256(TEXT.encode("utf-8")).hexdigest())
        self.assertEqual(selected["access_scope_ref"], "authenticated:principal:A1")
        self.assertEqual(selected["model_part_count"], 1)
        result = self.dispatch()
        self.assertEqual(result["dispatch"], "SENT_ACCEPTED")
        self.assertEqual(result["recipient_use"], "NOT_OBSERVED")
        self.assertEqual(Path(result["selected"]).read_bytes(), TEXT.encode("utf-8"))
        self.assertEqual(len(self.sender.calls), 1)
        with self.assertRaisesRegex(ValueError, "OUTPUT_TARGET_INVALID"):
            self.dispatch()
        self.assertEqual(len(self.sender.calls), 1)

    def test_wrong_selector_subject_and_ambiguous_range_refuse(self):
        with self.assertRaisesRegex(ValueError, "UNREGISTERED_SELECTOR"):
            self.reader.read_current("gdocs:" + DOC_ID, "SECTION", "other")
        self.connector.subject = "principal:other"
        with self.assertRaisesRegex(ValueError, "AUTHENTICATED_SCOPE_DRIFT"):
            self.host.select_bounded_content(self.target)
        self.connector.subject = "principal:A1"
        original = self.connector.get_document
        def ambiguous(**kwargs):
            doc = original(**kwargs)
            doc["tabs"][0]["namedRanges"][NAME]["namedRanges"].append(
                dict(doc["tabs"][0]["namedRanges"][NAME]["namedRanges"][0]))
            return doc
        self.connector.get_document = ambiguous
        with self.assertRaisesRegex(ValueError, "NAMED_RANGE_AMBIGUOUS"):
            self.host.select_bounded_content(self.target)

    def test_absent_authenticated_connector_and_wrong_recipient_refuse_before_intent(self):
        with self.assertRaisesRegex(ValueError, "AUTHENTICATED_CONNECTOR_REQUIRED"):
            FixedGoogleDocsNamedRangeReader(
                connector=object(), credential_ref="credential:existing-google-drive",
                subject_ref="principal:A1", document_id=DOC_ID, tab_id=TAB_ID,
                range_name=NAME, named_range_id=RANGE_ID,
                expected_revision_id="revision-one")
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
            self.dispatch(recipient_ref="actor:1B")
        self.assertFalse(Path(self.out).exists())
        self.assertEqual(self.sender.calls, [])
        unbound = BoundControlResolutionHost(
            persistence_verifier=SimpleNamespace(verify=lambda **_: {}),
            capability_resolver=SimpleNamespace(is_available=lambda **_: False,
                                                executor=lambda **_: None),
            bounded_content_provider=self.reader,
            rom_a_selected_dispatcher=self.sender)
        with self.assertRaisesRegex(ValueError, "fixed-output-unbound"):
            unbound.dispatch_rom_a_docs_selection(target=self.target,
                recipient_ref="actor:A1", task_ref="task:X8",
                task_revision="revision-one")

    def test_stale_revision_or_revoked_access_after_intent_no_send(self):
        self.connector.on_read = lambda n: setattr(self.connector, "revision", "revision-two") if n == 2 else None
        with self.assertRaisesRegex(ValueError, "REVISION_UNVERIFIED"):
            self.dispatch()
        self.assertEqual(self.sender.calls, [])
        receipt = json.loads((Path(self.out) / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["dispatch"]["state"], "UNKNOWN_NO_RETRY")
        self.assertTrue((Path(self.out) / ".receipt-dispatch-intent.json").exists())
        self.connector.on_read = None
        self.out = str(Path(self.temp.name) / "return-two")
        self.host._rom_a_docs_out_dir = Path(self.out)
        self.connector.revision = "revision-one"
        self.connector.on_read = lambda n: setattr(self.connector, "allowed", False) if n == 4 else None
        with self.assertRaises(PermissionError):
            self.dispatch()
        self.assertEqual(self.sender.calls, [])

    def test_same_revision_changed_bytes_hold_no_retry(self):
        self.connector.on_read = lambda n: setattr(self.connector, "first_paragraph", "Bye\n") if n == 2 else None
        with self.assertRaisesRegex(ValueError, "CURRENT_ACCESS_OR_REVISION_DRIFT_NO_RETRY"):
            self.dispatch()
        self.assertEqual(self.sender.calls, [])
        receipt = json.loads((Path(self.out) / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["dispatch"]["state"], "UNKNOWN_NO_RETRY")


if __name__ == "__main__":
    unittest.main()
