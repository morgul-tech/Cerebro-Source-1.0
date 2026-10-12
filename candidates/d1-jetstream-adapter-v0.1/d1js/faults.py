"""Deterministic SYNTHETIC fault injection for fixture tests. Never part of a production path.

A fault point is a name checked at a seam. Modes: ``raise_once`` / ``raise_always`` (OSError), ``exit`` (hard
process exit, os._exit(86), used only by child adapter processes to model a crash at that exact point), and
``call:<name>`` handled by a registered callback (e.g. the owner moves its head between read and append).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Callable

CRASH_EXIT_CODE = 86


class InjectedFault(OSError):
    pass


class Faults:
    SYNTHETIC = "SYNTHETIC_TEST_ONLY"

    def __init__(self, spec: dict[str, str] | None = None) -> None:
        self._spec = dict(spec or {})
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._lock = threading.Lock()
        self.fired: list[str] = []

    @classmethod
    def from_json(cls, text: str | None) -> "Faults":
        return cls(json.loads(text) if text else {})

    def set(self, point: str, mode: str) -> None:
        with self._lock:
            self._spec[point] = mode

    def clear(self, point: str | None = None) -> None:
        with self._lock:
            if point is None:
                self._spec.clear()
            else:
                self._spec.pop(point, None)

    def on(self, name: str, fn: Callable[[], None]) -> None:
        self._callbacks[name] = fn

    def hit(self, point: str) -> None:
        with self._lock:
            mode = self._spec.get(point)
            if mode is None:
                return
            if mode == "raise_once":
                self._spec.pop(point)
            self.fired.append(point)
        if mode in ("raise_once", "raise_always"):
            raise InjectedFault(f"INJECTED:{point}")
        if mode == "exit":
            os._exit(CRASH_EXIT_CODE)
        if mode.startswith("call:"):
            self._callbacks[mode[5:]]()
            with self._lock:
                self._spec.pop(point, None)
            return
        raise ValueError(f"unknown fault mode {mode!r}")

