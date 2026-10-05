"""Self-created SYNTHETIC fixture for the positive acceptance case.  Nothing here is real PM/claim/queue data.

Sizes mirror the character counts quoted in the work order only so the measurement path is exercised with realistic
magnitudes; they say nothing about any real episode's outcome.
"""
import json

from .core import sha256_hex

PARENT_CHARS = 3640
VERIFIER_CHARS = 1500
FULL_CONTINUATION_CHARS = 2876
SOURCE_HEAD = "0123456789abcdef0123456789abcdef01234567"


def _pad(text, target):
    if len(text) > target:
        raise ValueError("synthetic text longer than target")
    fill = "\nSYNTHETIC FILLER LINE. " * (target // 20 + 2)
    return text + fill[: target - len(text)]


def _canon(obj):
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def positive_fixture():
    """-> dict file name -> exact bytes, for the positive synthetic case (all five inputs + optional baseline)."""
    parent = _pad(
        "SYNTHETIC PARENT TASK SYN-TASK-0001 revision rev-3\n"
        "Goal: adjust the synthetic adapter. Allowed paths: adapter.py and test_adapter.py only.\n"
        "Invariants: no network; no writes outside the two allowed paths; keep the public function signature.\n"
        "Stop edges: stop if a third path is needed; stop if any live effect would be required; stop if the "
        "return target changes.\nReturn to SYN-RETURN-TARGET-1.", PARENT_CHARS).encode("utf-8")
    verifier = _pad(
        "SYNTHETIC VERIFIER RETURN SYN-VERIFIER-0001: REFINE\n"
        "Finding: adapter.py mishandles empty input. Needed: fix in adapter.py and a new negative test in "
        "test_adapter.py. Evidence: SYN-EVID-1, SYN-EVID-2.", VERIFIER_CHARS).encode("utf-8")
    full = _pad(
        "SYNTHETIC FULL CONTINUATION TASK for SYN-TASK-0001: repair adapter.py (empty input) and add a negative test "
        "in test_adapter.py. Restates all limits, stop edges and the return target in full.", FULL_CONTINUATION_CHARS
    ).encode("utf-8")
    manifest = {
        "task_ref": "SYN-TASK-0001", "parent_revision": "rev-3", "parent_sha256": sha256_hex(parent),
        "actor_ref": "SYN-ACTOR-WRITER", "effect_class": "LOCAL_ONLY", "privacy_class": "SYNTHETIC_PUBLIC",
        "live_scope": "NONE", "authority_class": "NONE", "allowed_paths": ["adapter.py", "test_adapter.py"],
        "required_invariants": ["no network", "no writes outside allowed paths", "keep public signature"],
        "stop_edges": ["third path needed", "live effect required", "return target changes"],
        "return_target": "SYN-RETURN-TARGET-1", "way_home": "Return to SYN-OWNER through the original task thread.",
        "source_head": SOURCE_HEAD,
        "semantic_review": {"status": "OWNER_REVIEWED", "receipt_ref": "SYN-RECEIPT-OWNER-1"},
    }
    delta = {
        "verifier_ref": "SYN-VERIFIER-0001", "verifier_sha256": sha256_hex(verifier),
        "finding": "adapter.py mishandles empty input", "repair_paths": ["adapter.py", "test_adapter.py"],
        "requested_actor_ref": "SYN-ACTOR-WRITER", "requested_effect_class": "LOCAL_ONLY",
        "requested_privacy_class": "SYNTHETIC_PUBLIC", "requested_live_scope": "NONE",
        "requested_authority_class": "NONE", "requested_return_target": "SYN-RETURN-TARGET-1",
        "required_test_delta": ["add a negative test for empty input in test_adapter.py"],
        "evidence_refs": ["SYN-EVID-1", "SYN-EVID-2"], "idempotency_key": "SYN-IDEM-0001",
        "semantic_review": {"status": "VERIFIER_REVIEWED", "receipt_ref": "SYN-RECEIPT-VER-1"},
    }
    currentness = {
        "observed_source_head": SOURCE_HEAD, "observed_parent_revision": "rev-3",
        "observed_at": "2026-10-05T00:00:00+00:00", "provenance": "SYNTHETIC_FIXTURE_NOT_A_PROVIDER_READ",
    }
    return {
        "parent.txt": parent, "parent_manifest.json": _canon(manifest), "verifier.txt": verifier,
        "verifier_delta.json": _canon(delta), "currentness.json": _canon(currentness),
        "full_continuation.txt": full,
    }
