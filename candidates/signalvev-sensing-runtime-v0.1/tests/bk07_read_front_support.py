"""BK07 read-front test support (test doubles only, SYNTHETIC_TEST_ONLY fixtures copied from the frozen package)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from signalvev_sensing.bk07_read_front import (AccessContext, FrontRequest, ReadFrontPorts,  # noqa: E402
                                               load_dni_orient, read_front)
from signalvev_sensing.context_slice import ContextSliceResolver  # noqa: E402
from signalvev_sensing.context_slice_provider import LocalPinnedSnapshotProvider  # noqa: E402
from signalvev_sensing.read_front_state import ReadFrontState  # noqa: E402

FIX = Path(__file__).resolve().parent / "bk07_read_front_fixtures"
ORIGINAL = (FIX / "bk07-r1.txt").read_bytes()
FULL_SHA = hashlib.sha256(ORIGINAL).hexdigest()
SLICE = ORIGINAL[33:126]
SLICE_SHA = hashlib.sha256(SLICE).hexdigest()
KNAGG = "BK07.READ_FRONT"
SCOPE = {"scope": "fixture-known-BK07-rows", "message_ids": ["fixture-bk07-message", "fixture-bk11-message"],
         "max_rows": 128}
FAR_FUTURE = 4_102_444_800.0


def dni_root() -> Path:
    root = os.environ.get("BK07_DNI_IMPORT_ROOT")
    if not root:
        raise RuntimeError("BK07_DNI_IMPORT_ROOT must name the explicit DNI reader-v2 reference import root "
                           "(<work>/_reference_dni from bootstrap_inputs.py)")
    return Path(root)


def load(name: str):
    return json.loads((FIX / name).read_bytes())


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class CountingProvider:
    """Wraps the real LocalPinnedSnapshotProvider; hooks fire after the n-th describe (test seam only)."""

    def __init__(self, inner: LocalPinnedSnapshotProvider) -> None:
        self.inner, self.describes, self.after_describe = inner, 0, {}

    def describe_authorized(self, *a, **kw):
        d = self.inner.describe_authorized(*a, **kw)
        self.describes += 1
        hook = self.after_describe.get(self.describes)
        if hook:
            hook()
        return d

    def open_verified_snapshot(self, *a, **kw):
        return self.inner.open_verified_snapshot(*a, **kw)


class FrontCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="bk07rf-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = self.dir / "root"
        self.root.mkdir()
        (self.root / "bk07-r1.txt").write_bytes(ORIGINAL)
        self.cap26_registry = load("cap26_registry.json")
        self.nav = load("navigation_registry.json")
        self.manifest = load("channel_manifest_r1.json")
        self.find, self.paragraph = load("dni_find.json"), load("dni_paragraph.json")
        self.access = AccessContext("principal:fixture-x2", "scope:bk07-fixture", "provider:local-pinned",
                                    "SYNTHETIC_TEST_ONLY", FAR_FUTURE)
        self.human = False
        self.clock = Clock()
        self.wall = Clock(1_790_000_000.0)
        self.state_path = self.dir / "state.json"
        self.orient = load_dni_orient(dni_root())
        self.write_cap26()

    # -------------------------------------------------------------- fixture mutation helpers
    def write_cap26(self) -> None:
        (self.root / "cap26_registry.json").write_text(json.dumps(self.cap26_registry))

    def nav_entry(self, i: int = 0) -> dict:
        return self.nav["entries"][i]

    def add_content(self, name: str, data: bytes, content_ref: str, **over) -> None:
        (self.root / name).write_bytes(data)
        b = {**copy.deepcopy(self.cap26_registry["bindings"][0]), "content_ref": content_ref, "path": name,
             "sha256": sha(data), **over}
        self.cap26_registry["bindings"].append(b)
        self.write_cap26()

    # -------------------------------------------------------------- ports / call
    def ports(self, *, cap26=True, dni=True, manifest=True, access=True, wrap=None) -> ReadFrontPorts:
        self.provider = LocalPinnedSnapshotProvider(self.root, self.root / "cap26_registry.json", clock=self.clock)
        self.wrapped = CountingProvider(self.provider)
        if wrap:
            wrap(self.wrapped)
        p = ReadFrontPorts(clock=self.clock, wall_clock=self.wall, provider_stats=self.provider.stats)
        p.registry = lambda: json.dumps(self.nav).encode()
        if cap26:
            p.cap26 = ContextSliceResolver(self.wrapped, clock=self.clock)
        if manifest:
            p.manifest = lambda: copy.deepcopy(self.manifest)
        if dni:
            p.dni_orient = self.orient
            p.dni = lambda doc: (copy.deepcopy(self.find), copy.deepcopy(self.paragraph)) \
                if doc == self.find.get("documentId") else None
        if access:
            p.access = lambda: self.access
        p.human_active = lambda: self.human
        return p

    def request(self, **over) -> FrontRequest:
        kw = dict(question="BK07", knagg=KNAGG, subject_ref="actor:fixture-x2", scope=dict(SCOPE))
        kw.update(over)
        return FrontRequest(**kw)

    def call(self, *, request=None, **port_kw):
        state = ReadFrontState.load(self.state_path)          # every call = a fresh process (restart)
        res = read_front(request or self.request(), self.ports(**port_kw), state)
        res.to_json()                                          # every result must fit its cap
        return res

    def assert_no_content(self, res) -> None:
        blob = json.dumps(res.to_dict())
        self.assertIsNone(res.content)
        for marker in ("STATE=CODE_PASS", "BK07 synthetic orientation", "BK07 source binding", "BK11 independent"):
            self.assertNotIn(marker, blob)
        self.assertNotIn("principal:fixture-x2", blob)                 # no private access metadata
        self.assertNotIn("principal:fixture-x2", res.receipt().decode())

    def stored(self, knagg=KNAGG):
        return ReadFrontState.load(self.state_path).entry(knagg)
