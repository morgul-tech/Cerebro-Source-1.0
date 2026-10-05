"""SYNTHETIC owner-resolver fixture for development. NOT owner truth, NOT the production adapter.

The production adapter port is `signalvev_sensing.OwnerResolver` (resolve(ResolveRequest) -> ResolverResult); a real
owner reader is wired by a later, separately gated edge (`resolver.kind = "factory"` or the Python API). This fixture
answers from a small local JSON file that MUST carry the label below, so it cannot be mistaken for a real owner:

  {"label": "SYNTHETIC_OWNER_FIXTURE_NOT_PRODUCTION",
   "entries": [{"owner_ref": "...", "referent_type": "...", "referent_id": "...",
                "current_revision": "...", "owner_seq": 5, "sha256": "<64 hex>"}]}

Owner judgement is the fixture's, never the runtime's: same revision => SAME; the fixture's own owner_seq newer than the
event's => SUPERSEDED (cannot be overridden by an old/delayed/reordered hint); otherwise UNKNOWN (=> typed HOLD).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import _bootstrap  # noqa: F401
from signalvev_sensing import ResolveRequest, ResolverResult, ResolverUnavailable
from signalvev_sensing.model import HEX64, ID_RE

from .errors import ConfigError

LABEL = "SYNTHETIC_OWNER_FIXTURE_NOT_PRODUCTION"
MAX_FIXTURE_BYTES = 64 * 1024
MAX_ENTRIES = 512


class SyntheticOwnerResolver:
    is_synthetic = True
    description = "SYNTHETIC_FIXTURE_NOT_OWNER_TRUTH"

    def __init__(self, entries: dict[tuple[str, str, str], dict[str, Any]]) -> None:
        self._entries = entries
        self.calls = 0

    @classmethod
    def from_dict(cls, doc: Any) -> "SyntheticOwnerResolver":
        if not isinstance(doc, dict) or doc.get("label") != LABEL or not isinstance(doc.get("entries"), list):
            raise ConfigError("SYNTHETIC_FIXTURE_UNLABELLED", f"fixture must carry label {LABEL!r} and an entries list")
        if len(doc["entries"]) > MAX_ENTRIES:
            raise ConfigError("SYNTHETIC_FIXTURE_TOO_LARGE")
        entries: dict[tuple[str, str, str], dict[str, Any]] = {}
        for i, e in enumerate(doc["entries"]):
            ok = (isinstance(e, dict) and set(e) == {"owner_ref", "referent_type", "referent_id", "current_revision",
                                                     "owner_seq", "sha256"}
                  and all(isinstance(e[k], str) and ID_RE.match(e[k]) for k in
                          ("owner_ref", "referent_type", "referent_id", "current_revision"))
                  and type(e["owner_seq"]) is int and e["owner_seq"] >= 1
                  and isinstance(e["sha256"], str) and HEX64.match(e["sha256"]))
            if not ok:
                raise ConfigError("SYNTHETIC_FIXTURE_ENTRY_INVALID", f"entries[{i}]")
            entries[(e["owner_ref"], e["referent_type"], e["referent_id"])] = e
        return cls(entries)

    @classmethod
    def from_file(cls, path: Path | str) -> "SyntheticOwnerResolver":
        p = Path(path)
        try:
            if p.stat().st_size > MAX_FIXTURE_BYTES:
                raise ConfigError("SYNTHETIC_FIXTURE_TOO_LARGE")
            doc = json.loads(p.read_text(encoding="utf-8"))
        except OSError:
            raise ConfigError("SYNTHETIC_FIXTURE_UNREADABLE", p.name) from None
        except ValueError:
            raise ConfigError("SYNTHETIC_FIXTURE_NOT_JSON", p.name) from None
        return cls.from_dict(doc)

    def resolve(self, request: ResolveRequest) -> ResolverResult:
        self.calls += 1
        entry = self._entries.get((request.owner_ref, request.referent_type, request.referent_id))
        if entry is None:
            raise ResolverUnavailable("synthetic fixture has no entry for this referent")
        if entry["current_revision"] == request.expected_revision:
            relation = "SAME"
        elif entry["owner_seq"] > request.owner_seq:
            relation = "SUPERSEDED"
        else:
            relation = "UNKNOWN"
        grounding = {"synthetic_fixture": True, "pointer_ref": request.pointer_ref} if request.depth == "POINTER_GROUND" else None
        return ResolverResult(
            source_ref=request.owner_ref, referent_type=request.referent_type, referent_id=request.referent_id,
            current_revision=entry["current_revision"], revision_relation=relation, observed_sha256=entry["sha256"],
            grounding=grounding, owner_seq=entry["owner_seq"])
