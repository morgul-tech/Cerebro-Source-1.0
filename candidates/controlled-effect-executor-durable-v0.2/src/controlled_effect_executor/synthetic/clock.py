"""Deterministic fake clock for tests."""
from __future__ import annotations


class FakeClock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds
