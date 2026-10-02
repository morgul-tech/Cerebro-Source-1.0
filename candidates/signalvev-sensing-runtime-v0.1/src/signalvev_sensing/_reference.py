"""Import bridge to the Source Signalvev reference modules (v0.1-v0.18). IMPORT, never copy.

Everything the runtime reuses comes through here, so the reuse surface is one readable file.
No reference module is modified. The validator directory is found relative to the repo, or via
SIGNALVEV_VALIDATOR_DIR (used by tests that run against a different checkout).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parents[4] / "tooling" / "validator"
VALIDATOR_DIR = Path(os.environ.get("SIGNALVEV_VALIDATOR_DIR", _DEFAULT))
if not (VALIDATOR_DIR / "signalvev_reference_v01_validation.py").exists():
    raise ImportError(f"Signalvev reference validators not found in {VALIDATOR_DIR}")
if str(VALIDATOR_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATOR_DIR))

from signalvev_reference_v01_validation import (  # noqa: E402  v0.1: envelope, registry, receipt partial order
    STAGE_PREDECESSORS,
    ReceiptError,
    ReceiptTrail,
    SchemaError,
    load_registry,
    validate_envelope,
)
from signalvev_reference_v09_validation import FirstBrokenEdgeRecorder  # noqa: E402  v0.9: flight recorder
from signalvev_reference_v15_validation import evaluate_schema_compatibility  # noqa: E402  v0.15: version gate
from signalvev_reference_v16_validation import resolve_state_delta_truth  # noqa: E402  v0.16: EVENT != STATE_TRUTH

__all__ = [
    "STAGE_PREDECESSORS", "ReceiptError", "ReceiptTrail", "SchemaError", "load_registry", "validate_envelope",
    "FirstBrokenEdgeRecorder", "evaluate_schema_compatibility", "resolve_state_delta_truth", "VALIDATOR_DIR",
]
