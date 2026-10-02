"""5. APPLICABILITY: cheap, purely local, typed. It never touches the owner and never wakes anything."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .model import APPLICABILITY_HOLD, APPLICABILITY_NOT_APPLICABLE, APPLIES


@dataclass(frozen=True)
class Interest:
    owner_ref: str
    referent_type: str
    referent_id: str | None = None        # None = every referent of that type under that owner


class ApplicabilityPolicy(Protocol):
    def evaluate(self, owner_ref: str, referent_type: str, referent_id: str) -> str: ...


class InterestTable:
    """The receiver's own local subscriptions. `available=False` models an unreadable local config => HOLD."""

    def __init__(self, interests: Iterable[Interest], *, available: bool = True) -> None:
        self._interests, self.available = tuple(interests), available

    def evaluate(self, owner_ref: str, referent_type: str, referent_id: str) -> str:
        if not self.available:
            return APPLICABILITY_HOLD
        for i in self._interests:
            if i.owner_ref == owner_ref and i.referent_type == referent_type and i.referent_id in (None, referent_id):
                return APPLIES
        return APPLICABILITY_NOT_APPLICABLE
