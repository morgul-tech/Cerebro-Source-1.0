"""9. MODEL-ACTIVATION DECISION. A pure, typed function. It does not call a model, hold a client, or bind a wake.

DETERMINISTIC_DISPOSITION_COMPLETE: the typed disposition is the whole result; nothing needs a model.
JUDGMENT_REQUIRED: meaning must be judged by someone else. This decision lists WHAT is open (sorted, unranked) and
deliberately has no field in which an answer could be put. A future owner binds any actual wake.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .model import ACK_READ, DETERMINISTIC, JUDGMENT_REQUIRED, SEMANTIC

MAX_CANDIDATES = 8


@dataclass(frozen=True)
class ActivationDecision:
    decision: str
    reasons: tuple[str, ...]
    candidates: tuple[str, ...] = ()
    wake_bound: bool = False         # always False here: no wake mechanism exists in this package

    def as_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "reasons": list(self.reasons), "candidates": list(self.candidates),
                "wake_bound": self.wake_bound}


def decide_activation(disposition: str, change_class: str, candidates: Sequence[str] = ()) -> ActivationDecision:
    if disposition != ACK_READ:
        return ActivationDecision(DETERMINISTIC, (f"TYPED_{disposition}_IS_COMPLETE",))
    uniq = tuple(sorted(set(candidates)))                     # neutral order: no preference is encoded
    if len(uniq) >= 2:
        return ActivationDecision(JUDGMENT_REQUIRED, ("AMBIGUOUS_OWNER_GROUNDING",), uniq)
    if change_class == SEMANTIC:
        return ActivationDecision(JUDGMENT_REQUIRED, ("SEMANTIC_CHANGE_NEEDS_MEANING_JUDGMENT",))
    return ActivationDecision(DETERMINISTIC, ("MECHANICAL_CHANGE_CONFIRMED_AT_OWNER",))
