"""CAP26 test support (test doubles only). Synthetic fixtures: no real private content anywhere."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from signalvev_sensing.context_slice import Selector, SliceLimits, SliceRequest, parse_request  # noqa: E402
from signalvev_sensing.context_slice_provider import (ProviderUnavailable, ReadDescriptor,  # noqa: E402
                                                      LocalPinnedSnapshotProvider)

FIX = Path(__file__).resolve().parent / "context_slice_fixtures"
ORIGINAL = (FIX / "original.txt").read_bytes()
EXPECTED_SLICE = (FIX / "expected_slice.txt").read_bytes()
FULL_SHA = hashlib.sha256(ORIGINAL).hexdigest()
SLICE_SHA = hashlib.sha256(EXPECTED_SLICE).hexdigest()
SECRET = "PRIVATE-CANARY-7f3a"            # synthetic marker; must never appear in any output


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def binding(**over) -> dict:
    b = {"content_ref": "fixture:public-report", "path": "original.txt", "owner_ref": "owner:fixture",
         "revision": "r1", "sha256": FULL_SHA, "authorized_subjects": ["actor:fixture-x2"], "head_relation": "SAME",
         "semantic_currentness": "UNKNOWN", "owner_seq": 1, "way_home": "fixture:canonical-home"}
    b.update(over)
    return b


def request(**over) -> SliceRequest:
    doc = json.loads((FIX / "request.json").read_bytes())
    sel = over.pop("selector", None)
    lim = over.pop("limits", None)
    if sel:
        doc["selector"].update(sel)
    if lim:
        doc["limits"].update(lim)
    doc.update(over)
    return parse_request(json.dumps(doc).encode())


class TmpRoot(unittest.TestCase):
    """A private temp root holding a COPY of the supplied original + a registry file."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="cap26-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "root"
        self.root.mkdir()
        (self.root / "original.txt").write_bytes(ORIGINAL)
        self.registry_path = self.dir / "registry.json"
        self.bindings = [binding()]
        self.write_registry()
        self.clock = FakeClock()
        self.provider = LocalPinnedSnapshotProvider(self.root, self.registry_path, clock=self.clock)

    def resolver(self):
        from signalvev_sensing.context_slice import ContextSliceResolver
        return ContextSliceResolver(self.provider, clock=self.clock)      # ONE shared clock: deadlines are absolute

    def write_registry(self, bindings=None) -> None:
        self.registry_path.write_text(json.dumps({"note": "SYNTHETIC", "bindings": bindings or self.bindings}))

    def add(self, name: str, data: bytes, **over) -> dict:
        (self.root / name).write_bytes(data)
        b = binding(path=name, sha256=sha(data), **over)
        self.bindings.append(b)
        self.write_registry()
        return b


class ScriptedSnapshot:
    def __init__(self, chunks, confirm=True, fail_at=None):
        self._chunks, self._confirm, self._fail_at = chunks, confirm, fail_at

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def chunks(self):
        for i, c in enumerate(self._chunks):
            if self._fail_at == i:
                raise OSError("synthetic interrupted read")
            yield c

    def confirm(self):
        return self._confirm


class ScriptedProvider:
    """Fake provider whose metadata/chunks can lie: proves the resolver does not trust the provider."""

    def __init__(self, *, size=len(ORIGINAL), sha256=FULL_SHA, chunks=None, confirm=True, fail_at=None,
                 clock=None, advance_on_open=0.0, unavailable=False, relation="SAME") -> None:
        self.size, self.sha256 = size, sha256
        self.chunks = chunks if chunks is not None else [ORIGINAL[i:i + 65536] for i in range(0, len(ORIGINAL), 65536)]
        self.confirm, self.fail_at, self.clock = confirm, fail_at, clock
        self.advance_on_open, self.unavailable, self.relation = advance_on_open, unavailable, relation
        self.describes = self.opens = 0

    def describe_authorized(self, subject_ref, content_ref, owner_ref, *, deadline):
        self.describes += 1
        if self.unavailable:
            raise ProviderUnavailable("SYNTHETIC_PROVIDER_OFFLINE")
        return ReadDescriptor(canonical_ref=content_ref, owner_ref=owner_ref, revision="r1", size=self.size,
                              sha256=self.sha256, way_home="fixture:canonical-home", head_relation=self.relation,
                              superseded_revisions=(), owner_seq=None, semantic_currentness="UNKNOWN",
                              authorization_basis="SCRIPTED", assertion="SYNTHETIC_OWNER_ASSERTION",
                              provider="SCRIPTED", snapshot_identity="0" * 64)

    def open_verified_snapshot(self, descriptor, *, max_bytes, chunk_bytes, deadline):
        self.opens += 1
        if self.clock is not None and self.advance_on_open:
            self.clock.advance(self.advance_on_open)
        return ScriptedSnapshot(self.chunks, self.confirm, self.fail_at)
