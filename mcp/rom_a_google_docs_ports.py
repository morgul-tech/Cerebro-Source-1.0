"""Fixed, host-owned Google Docs named-range reader for one ROM-A episode.

No OAuth client, credential, endpoint, or recipient is constructed here.  The
normal host must inject its already-authenticated connector and exact binding.
Without that binding this port is unavailable, not a caller-supplied fixture.
"""

from __future__ import annotations

import re
from typing import Any


_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


class FixedGoogleDocsNamedRangeReader:
    """Read only one fixed file/tab/named-range through a trusted connector.

    Connector methods must be backed by the host's existing authenticated
    credential reference, never by request data: credential_ref(),
    authenticated_subject_ref(), get_file_metadata(fileId=..., fields=...),
    get_document(document_id=...).  A connector error is a refusal.
    """

    def __init__(self, *, connector: Any, credential_ref: str,
                 subject_ref: str, document_id: str, tab_id: str,
                 range_name: str, named_range_id: str,
                 expected_revision_id: str):
        _require(all(callable(getattr(connector, name, None)) for name in
                     ("credential_ref", "authenticated_subject_ref",
                      "get_file_metadata", "get_document")),
                 "ROMA_DOCS_AUTHENTICATED_CONNECTOR_REQUIRED")
        _require(all(isinstance(value, str) and _REF.fullmatch(value) for value in
                     (credential_ref, subject_ref, tab_id, range_name, named_range_id)) and
                 isinstance(document_id, str) and _ID.fullmatch(document_id) and
                 isinstance(expected_revision_id, str) and bool(expected_revision_id.strip()),
                 "ROMA_DOCS_FIXED_BINDING_REQUIRED")
        self._connector = connector
        self._credential_ref = credential_ref
        self._subject_ref = subject_ref
        self._document_id = document_id
        self._tab_id = tab_id
        self._range_name = range_name
        self._named_range_id = named_range_id
        self._expected_revision_id = expected_revision_id
        self._content_ref = "gdocs:" + document_id

    def _identity(self) -> None:
        _require(self._connector.credential_ref() == self._credential_ref and
                 self._connector.authenticated_subject_ref() == self._subject_ref,
                 "ROMA_DOCS_AUTHENTICATED_SCOPE_DRIFT")

    def read_current(self, content_ref: str, kind: str, selector: str) -> dict[str, Any]:
        _require((content_ref, kind, selector) ==
                 (self._content_ref, "SECTION", self._range_name),
                 "ROMA_DOCS_UNREGISTERED_SELECTOR")
        self._identity()
        meta = self._connector.get_file_metadata(
            fileId=self._document_id, fields="id,mimeType,permissions")
        _require(isinstance(meta, dict) and meta.get("id") == self._document_id and
                 meta.get("mime_type") == "application/vnd.google-apps.document" and
                 meta.get("source_visibility_status") == "permission_metadata_available",
                 "ROMA_DOCS_CURRENT_ACCESS_UNVERIFIED")
        doc = self._connector.get_document(document_id=self._document_id)
        _require(isinstance(doc, dict) and doc.get("documentId") == self._document_id and
                 doc.get("revisionId") == self._expected_revision_id,
                 "ROMA_DOCS_REVISION_UNVERIFIED")
        tabs = [tab for tab in doc.get("tabs", []) if tab.get("tabId") == self._tab_id]
        _require(len(tabs) == 1, "ROMA_DOCS_TAB_AMBIGUOUS")
        tab = tabs[0]
        group = tab.get("namedRanges", {}).get(self._range_name, {})
        names = group.get("namedRanges", [])
        _require(len(names) == 1 and names[0].get("namedRangeId") == self._named_range_id and
                 names[0].get("name") == self._range_name and
                 len(names[0].get("ranges", [])) == 1,
                 "ROMA_DOCS_NAMED_RANGE_AMBIGUOUS")
        bounds = names[0]["ranges"][0]
        start, end = bounds.get("startIndex"), bounds.get("endIndex")
        _require(type(start) is int and type(end) is int and 0 <= start < end,
                 "ROMA_DOCS_RANGE_INVALID")
        position = start
        selected: list[str] = []
        for item in tab.get("body", {}).get("content", []):
            left, right = item.get("startIndex"), item.get("endIndex")
            if type(left) is not int or type(right) is not int or right <= start or left >= end:
                continue
            _require(left == position and right <= end and isinstance(item.get("paragraph"), dict),
                     "ROMA_DOCS_RANGE_NOT_WHOLE_PARAGRAPHS")
            elements = item["paragraph"].get("elements", [])
            _require(bool(elements) and all(isinstance(part.get("textRun", {}).get("content"), str)
                                            for part in elements),
                     "ROMA_DOCS_NON_TEXT_IN_SELECTION")
            selected.append("".join(part["textRun"]["content"] for part in elements))
            position = right
        text = "".join(selected)
        _require(position == end and bool(text.strip()) and
                 len(text.encode("utf-16-le")) // 2 == end - start and
                 len(text.encode("utf-8")) <= 16_384,
                 "ROMA_DOCS_SELECTION_INCOMPLETE")
        self._identity()
        return {"content_ref": self._content_ref, "selector_kind": kind,
                "selector": selector, "revision": doc["revisionId"],
                "authority_ref": "provider:google-docs", "currentness": "CURRENT",
                "provider_readback_verified": True, "access_verified": True,
                "access_scope_ref": "authenticated:" + self._subject_ref,
                "provenance_refs": [self._content_ref, "tab:" + self._tab_id,
                                    "named-range:" + self._named_range_id],
                "parts": {selector: text}}
