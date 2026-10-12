"""Shared test support. Reference roots are passed explicitly (env or reference_roots.local.json), never guessed."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
if str(CANDIDATE) not in sys.path:
    sys.path.insert(0, str(CANDIDATE))

from d1js.config import D1Config  # noqa: E402
from d1js.faults import Faults  # noqa: E402


def reference_roots() -> tuple[Path, Path]:
    gw = os.environ.get("D1JS_GATEWAY_ROOT")
    pr = os.environ.get("D1JS_PROJECT_RETURN_ROOT")
    local = CANDIDATE / "reference_roots.local.json"
    if (not gw or not pr) and local.is_file():
        doc = json.loads(local.read_text(encoding="utf-8"))
        gw, pr = gw or doc.get("gateway_root"), pr or doc.get("project_return_root")
    if not gw or not pr:
        raise RuntimeError("REFERENCE_ROOT_NOT_CONFIGURED: set D1JS_GATEWAY_ROOT and D1JS_PROJECT_RETURN_ROOT "
                           "(see README) -- the historical gateway is imported from its pinned reference root")
    return Path(gw), Path(pr)


class FakeClock:
    """SYNTHETIC clock shared by owner TTL checks and the in-memory transport."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def epoch(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def unit_config(**over) -> D1Config:
    base = dict(enabled=True, server_url="nats://127.0.0.1:1", ack_wait_s=1.0, hold_nak_delay_s=1.0,
                pull_timeout_s=0.1)
    base.update(over)
    return D1Config(**base).validate()


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="d1js-unit-")
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
