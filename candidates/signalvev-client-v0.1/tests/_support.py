"""Shared test support. Test doubles only; nothing here is a runtime component.

SIGNALVEV_CLIENT_TEST_TARGET=source (default): put the two source trees on sys.path (repo checkout).
SIGNALVEV_CLIENT_TEST_TARGET=installed: touch NO path; the tests must find the INSTALLED distribution.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

TARGET = os.environ.get("SIGNALVEV_CLIENT_TEST_TARGET", "source")
HERE = Path(__file__).resolve().parent
if TARGET == "source":
    for src in (HERE.parent / "src", HERE.parents[1] / "signalvev-sensing-runtime-v0.1" / "src"):
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))

import signalvev_client  # noqa: E402,F401  (binds bundled references first)
from signalvev_sensing import canonical, sha256_hex  # noqa: E402

NOW = 1_790_000_000.0
OWNER = "owner:test-synthetic"
INLINE_SHA = sha256_hex(canonical({"status": "READY", "n": 3}))
POINTER_SHA = sha256_hex(b"section-bytes-at-owner")


def raw_event(**over):
    ev = {
        "event_id": "evt-0001", "owner_ref": OWNER, "source_ref": "src:doc-a",
        "referent": {"type": "doc", "id": "doc-a"}, "owner_seq": 5,
        "revision_basis": {"before": "rev-4", "after": "rev-5"}, "change_class": "MECHANICAL",
        "delta": {"kind": "INLINE", "expected_sha256": INLINE_SHA, "fields": {"status": "READY", "n": 3}},
        "commit": {"state": "COMMITTED_READBACK", "readback_ref": "rb:doc-a:rev-5", "observed_at": "2026-09-21T00:00:00Z"},
        "way_home": ["owner:doc-a#section-3"],
    }
    ev.update(over)
    return ev


def config_doc(tmp: Path, **over):
    doc = {
        "node": {"id": "node-test"},
        "nats": {"server": "nats://127.0.0.1:4222", "connect_timeout_seconds": 2.0, "flush_timeout_seconds": 1.0,
                 "max_reconnect_attempts": 2, "reconnect_wait_seconds": 0.1},
        "evidence": {"dir": str(tmp / "evidence")},
        "send": {"ttl_seconds": 30},
        "listen": {"interest": [{"owner_ref": OWNER, "referent_type": "doc"}], "max_queue": 8},
        "resolver": {"kind": "synthetic_fixture"},
    }
    for k, v in over.items():
        doc[k] = {**doc.get(k, {}), **v} if isinstance(v, dict) else v
    return doc


def synthetic_fixture_doc(*entries):
    return {"label": "SYNTHETIC_OWNER_FIXTURE_NOT_PRODUCTION", "entries": list(entries)}


def fixture_entry(**over):
    e = {"owner_ref": OWNER, "referent_type": "doc", "referent_id": "doc-a", "current_revision": "rev-5",
         "owner_seq": 5, "sha256": INLINE_SHA}
    e.update(over)
    return e


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="signalvev-client-test-")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)


# ----------------------------------------------------------------- fake nats-py (async, same shape as the real client)
class _Err(Exception):
    pass


def _nats_error(name):
    cls = type(name, (_Err,), {})
    cls.__module__ = "nats.errors"          # the binding recognises the real library's refusals by name + module
    return cls


ConnectionClosedError = _nats_error("ConnectionClosedError")
OutboundBufferLimitError = _nats_error("OutboundBufferLimitError")
MaxPayloadError = _nats_error("MaxPayloadError")


class _Msg:
    def __init__(self, subject, data):
        self.subject, self.data = subject, data


class FakeNC:
    def __init__(self, broker, kwargs):
        self.broker, self.kwargs = broker, kwargs
        self.is_connected, self.is_closed, self.is_reconnecting = True, False, False
        self.connected_url = types.SimpleNamespace(netloc="127.0.0.1:4222")
        self.connected_server_version = "fake-2.x"
        self.subs = []

    async def publish(self, subject, payload):
        self.broker.publish_calls.append((subject, payload))
        behaviour = self.broker.next_publish()
        if behaviour == "refuse_closed":
            raise ConnectionClosedError()
        if behaviour == "refuse_buffer":
            raise OutboundBufferLimitError()
        if behaviour == "oserror":
            raise OSError("socket write failed mid-publish")
        if behaviour == "hang":
            await asyncio.sleep(60)
        if behaviour == "write_then_drop":      # the bytes reach the broker, then the client sees an error
            self.broker.deliver(subject, payload)
            raise OSError("connection reset after write")
        if behaviour != "lose":
            self.broker.deliver(subject, payload)

    async def flush(self, timeout=10):
        if self.broker.flush_error:
            raise asyncio.TimeoutError()

    async def subscribe(self, subject, cb=None, **kw):
        self.broker.subs.setdefault(subject, []).append((asyncio.get_running_loop(), cb, self))
        self.subs.append(subject)

    async def drain(self):
        self.is_closed, self.is_connected = True, False

    async def close(self):
        self.is_closed, self.is_connected = True, False


class FakeBroker:
    """In-process stand-in for the nats-py module: connect_fn + a tiny pub/sub switchboard. NOT a NATS server."""

    def __init__(self):
        self.subs, self.publish_calls, self.delivered, self.connects = {}, [], [], []
        self.script, self.flush_error, self.refuse_connect = [], False, False
        self.lock = threading.Lock()

    def next_publish(self):
        with self.lock:
            return self.script.pop(0) if self.script else "ok"

    def deliver(self, subject, payload):
        self.delivered.append((subject, payload))
        for loop, cb, nc in list(self.subs.get(subject, [])):
            if not nc.is_closed:
                asyncio.run_coroutine_threadsafe(cb(_Msg(subject, payload)), loop)

    async def connect(self, **kwargs):
        if self.refuse_connect:
            raise ConnectionRefusedError("refused by test broker")
        nc = FakeNC(self, kwargs)
        self.connects.append(nc)
        return nc

    def make_nc_drop(self):
        for nc in self.connects:
            nc.is_connected = False
