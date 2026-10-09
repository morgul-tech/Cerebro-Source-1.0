"""SYNTHETIC safe-boundary host + a small lease/epoch guard around the existing gateway calls.

Not a host service. The host (here a labelled fake) owns the truth about active Human turns and local effects. A
receiver drain may only proceed under a lease; starting a Human turn or a local effect bumps the epoch, which revokes
any lease at once (the Human is never preempted -- the ingress yields). Every seam the composition touches re-checks
the lease: before pull, before return append, before ACK. Missing, stale or released lease => DEFER / NOACK.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable


class LeaseLost(RuntimeError):
    """The safe-boundary lease is missing, stale or released."""


@dataclass
class Lease:
    host: "SyntheticSafeBoundaryHost"
    epoch: int
    released: bool = False

    def valid(self) -> bool:
        return not self.released and self.host._lease_still_valid(self)

    def require(self, seam: str) -> None:
        if not self.valid():
            self.host.lease_losses.append(seam)
            raise LeaseLost(seam)

    @contextmanager
    def critical(self, seam: str):
        """Hold the host lock for one short atomic step (e.g. the closure-ledger insert): a Human turn or local
        effect cannot begin in between; it starts right after (milliseconds). The lease must be valid on entry."""
        with self.host._lock:
            if self.released or not self.host._valid_locked(self):
                self.host.lease_losses.append(seam)
                raise LeaseLost(seam)
            yield

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.host._release(self)


class SyntheticSafeBoundaryHost:
    SYNTHETIC_TEST_ONLY = True

    def __init__(self, *, capacity_slots: int = 1) -> None:
        self._lock = threading.RLock()
        self.active_human_turn = False
        self.local_effect = False
        self.capacity_slots = capacity_slots
        self.epoch = 0
        self._holder: Lease | None = None
        self.deferrals = 0
        self.lease_losses: list[str] = []
        self.hooks: dict[str, Callable[[], None]] = {}

    # host-side transitions (a Human turn always wins; it is never blocked by ingress)
    def begin_human_turn(self) -> None:
        with self._lock:
            self.active_human_turn = True
            self.epoch += 1

    def end_human_turn(self) -> None:
        with self._lock:
            self.active_human_turn = False

    def begin_local_effect(self) -> None:
        with self._lock:
            self.local_effect = True
            self.epoch += 1

    def end_local_effect(self) -> None:
        with self._lock:
            self.local_effect = False

    def revoke(self) -> None:
        with self._lock:
            self.epoch += 1

    # receiver side
    def acquire(self) -> Lease | None:
        with self._lock:
            if self.active_human_turn or self.local_effect or self.capacity_slots <= 0 or self._holder is not None:
                self.deferrals += 1
                return None
            lease = Lease(self, self.epoch)
            self._holder = lease
            return lease

    def _valid_locked(self, lease: Lease) -> bool:
        return (self._holder is lease and lease.epoch == self.epoch and not self.active_human_turn
                and not self.local_effect)

    def _lease_still_valid(self, lease: Lease) -> bool:
        with self._lock:
            return self._valid_locked(lease)

    def _release(self, lease: Lease) -> None:
        with self._lock:
            if self._holder is lease:
                self._holder = None

    def fire(self, name: str) -> None:
        fn = self.hooks.get(name)
        if fn is not None:
            fn()


class LeasedBoundaryReader:
    """gateway.SafeBoundaryReader bound to one lease: reports an unsafe boundary once the lease is not valid."""

    def __init__(self, gw: Any, host: SyntheticSafeBoundaryHost, lease: Lease) -> None:
        self._gw, self._host, self._lease = gw, host, lease

    def read_current(self):
        valid = self._lease.valid()
        return self._gw.SafeBoundary(active_turn=(not valid) or self._host.active_human_turn,
                                     local_effect_in_progress=self._host.local_effect,
                                     capacity_slots=self._host.capacity_slots)
