"""Shared test fixtures. Test doubles only: nothing here is a runtime component."""
from __future__ import annotations

import os
import sys
import unittest
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from signalvev_sensing import (  # noqa: E402
    DedupeCursor, FakeTransport, FlightRecorder, Interest, InterestTable, InMemoryReturnSink, ResolverResult,
    ResolverUnavailable, SendLedger, SensingReceiver, SensingSender, sha256_hex, canonical,
)

NOW = 1_790_000_000.0          # fixed test clock (2026-09-21)
OWNER = "owner:drive-reader-v2"
INLINE_SHA = sha256_hex(canonical({"status": "READY", "n": 3}))
POINTER_SHA = sha256_hex(b"section-bytes-at-owner")


def raw_event(**over):
    ev = {
        "event_id": "evt-0001",
        "owner_ref": OWNER,
        "source_ref": "src:doc-a",
        "referent": {"type": "doc", "id": "doc-a"},
        "owner_seq": 5,
        "revision_basis": {"before": "rev-4", "after": "rev-5"},
        "change_class": "MECHANICAL",
        "delta": {"kind": "INLINE", "expected_sha256": INLINE_SHA, "fields": {"status": "READY", "n": 3}},
        "commit": {"state": "COMMITTED_READBACK", "readback_ref": "rb:doc-a:rev-5", "observed_at": "2026-09-21T00:00:00Z"},
        "way_home": ["owner:doc-a#section-3"],
    }
    ev.update(over)
    return ev


def pointer_event(**over):
    base = raw_event(event_id="evt-0002", owner_seq=6, revision_basis={"before": "rev-5", "after": "rev-6"},
                     delta={"kind": "POINTER", "expected_sha256": POINTER_SHA, "ref": "owner:doc-a#section-3"},
                     commit={"state": "COMMITTED_READBACK", "readback_ref": "rb:doc-a:rev-6",
                             "observed_at": "2026-09-21T00:00:01Z"})
    base.update(over)
    return base


class FakeResolver:
    """Counts calls. Default answer: owner agrees (SAME revision, sha matches)."""

    def __init__(self, *, mode="OK", sha=None, relation="SAME", candidates=(), source_ref=None, grounding="DEFAULT", seq="ECHO"):
        self.calls = []
        self.mode, self.sha, self.relation = mode, sha, relation
        self.candidates, self.source_ref, self.grounding, self.seq = tuple(candidates), source_ref, grounding, seq

    def resolve(self, request):
        self.calls.append(request)
        if self.mode == "UNAVAILABLE":
            raise ResolverUnavailable("owner offline")
        if self.mode == "BOOM":
            raise RuntimeError("unexpected resolver bug")
        grounding = None
        if request.depth == "POINTER_GROUND":
            grounding = {"section": "text-that-stays-home-is-bounded"} if self.grounding == "DEFAULT" else self.grounding
        return ResolverResult(
            source_ref=self.source_ref or request.owner_ref, referent_type=request.referent_type,
            referent_id=request.referent_id, current_revision="rev-x", revision_relation=self.relation,
            observed_sha256=self.sha or request.expected_sha256, grounding=grounding, candidates=self.candidates,
            owner_seq=request.owner_seq if self.seq == "ECHO" else self.seq)


_RIGS = []


class RigTestCase(unittest.TestCase):
    def tearDown(self):
        while _RIGS:
            _RIGS.pop().close()


class Rig:
    """One sender + one receiver on a loopback transport; temp-dir persistence unless in_memory."""

    def __init__(self, *, resolver=None, interests=None, in_memory=False, tmp=None, transport=None, clock=None):
        self.now = [NOW]
        self.clock = clock or (lambda: self.now[0])
        self.tmp = Path(tmp or tempfile.mkdtemp(prefix="sensing-test-"))
        import atexit, shutil
        atexit.register(shutil.rmtree, self.tmp, True)
        self.in_memory = in_memory
        self.transport = transport or FakeTransport()
        self.resolver = resolver or FakeResolver()
        self.interests = interests or InterestTable([Interest(OWNER, "doc", None)])
        self.sink = InMemoryReturnSink()
        _RIGS.append(self)
        self.boot()

    def paths(self):
        return (None, None, None) if self.in_memory else (self.tmp / "cursor.jsonl", self.tmp / "recorder.jsonl",
                                                          self.tmp / "sender.jsonl")

    def boot(self):
        """(Re)create all runtime objects from persisted files = a process restart."""
        self.close()
        cur, rec, snd = self.paths()
        self.cursor = DedupeCursor(cur)
        self.recorder = FlightRecorder(rec)
        self.ledger = SendLedger(snd)
        self.receiver = SensingReceiver(interests=self.interests, resolver=self.resolver, cursor=self.cursor,
                                        recorder=self.recorder, sink=self.sink, clock=self.clock)
        self.sender = SensingSender(transport=self.transport, ledger=self.ledger, clock=self.clock)
        getattr(self.transport, "reset_subscribers", lambda: None)()
        self.receiver.attach(self.transport)

    def close(self):
        for name in ("cursor", "recorder", "ledger"):
            obj = getattr(self, name, None)
            if obj is not None:
                obj.close()

    def frame_for(self, raw):
        from signalvev_sensing import accept_owner_event, build_frame
        return build_frame(accept_owner_event(raw), now_epoch=self.now[0], ttl_seconds=30).data
