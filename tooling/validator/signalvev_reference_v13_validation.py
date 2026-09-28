#!/usr/bin/env python3
"""Offline validator + falsifier suite for
candidates/signalvev-reference-v0.13 (artifact pointer bytes-and-custody
consumption guard: the ARTIFACT POINTER / FILE TRANSFER primitive X4
runtime-fagpass's own crosswalk names as a material L5 gap).

STATUS: isolated implementation candidate. authority: NONE.
No import in this module opens any socket, subprocess, or file outside
the candidate directories, other than Python's own stdlib `hashlib`. No
NATS/WireGuard/VPS/Drive/JetStream code, and no file-transport or
download implementation, exists here.

This module has no dependency on any other signalvev-reference
candidate -- it is a pure, stateless guard over caller-supplied bytes
and declarations. See component.yaml explicitly_no_dependency_on.

Source provenance for the scenario this exercises:
  candidates/signalvev-reference-v0.13/artifact-pointer-consumption-guard-candidate.yaml
(carries its own upstream Drive citation in its own provenance block)
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

TOOLING_VALIDATOR_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLING_VALIDATOR_DIR))


# ---------------------------------------------------------------------------
# 1. The artifact pointer consumption guard
# ---------------------------------------------------------------------------

class ArtifactPointerError(ValueError):
    pass


_EXPECTED_KEYS = frozenset({"status", "reasons"})


def verify_and_consume(
    *,
    declared_hash: str,
    declared_size: int,
    actual_bytes: bytes,
    custody_verified: bool,
) -> dict:
    """Pure, stateless verify-before-consume guard. Never partially
    consumes -- any violation of hash, size, or custody blocks
    consumption entirely, and every violation found is reported."""
    if not isinstance(actual_bytes, (bytes, bytearray)):
        raise ArtifactPointerError("actual_bytes must be bytes-like")
    if not isinstance(custody_verified, bool):
        raise ArtifactPointerError("custody_verified must be a bool")

    actual_hash = hashlib.sha256(bytes(actual_bytes)).hexdigest()
    actual_size = len(actual_bytes)

    reasons: list[str] = []
    if actual_hash != declared_hash:
        reasons.append("HASH_MISMATCH")
    if actual_size != declared_size:
        reasons.append("SIZE_MISMATCH")
    if not custody_verified:
        reasons.append("CUSTODY_NOT_VERIFIED")

    if reasons:
        return {"status": "REJECTED", "reasons": tuple(reasons)}
    return {"status": "CONSUMED", "reasons": ()}


# ---------------------------------------------------------------------------
# 2. Golden path + falsifiers AP1-AP8
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_ap0_golden_path_matching_bytes_and_verified_custody_is_consumed() -> None:
    payload = b"the exact bytes the pointer declares"
    report = verify_and_consume(
        declared_hash=_sha256(payload),
        declared_size=len(payload),
        actual_bytes=payload,
        custody_verified=True,
    )
    assert report["status"] == "CONSUMED"
    assert report["reasons"] == ()


def test_ap1_hash_mismatch_alone_blocks_consumption() -> None:
    # This is the core falsifier: an artifact pointer with wrong bytes/hash
    # must never be consumed.
    payload = b"the actual bytes on disk"
    wrong_hash = _sha256(b"a completely different declared payload")
    report = verify_and_consume(
        declared_hash=wrong_hash,
        declared_size=len(payload),
        actual_bytes=payload,
        custody_verified=True,
    )
    assert report["status"] == "REJECTED"
    assert "HASH_MISMATCH" in report["reasons"]


def test_ap2_size_mismatch_alone_blocks_consumption() -> None:
    payload = b"twenty-six byte payload!!"
    report = verify_and_consume(
        declared_hash=_sha256(payload),
        declared_size=len(payload) + 1,  # wrong declared size, correct hash
        actual_bytes=payload,
        custody_verified=True,
    )
    assert report["status"] == "REJECTED"
    assert "SIZE_MISMATCH" in report["reasons"]


def test_ap3_byte_perfect_content_with_unverified_custody_is_still_rejected() -> None:
    # X4's own text: "Receiver verifies bytes AND current access before
    # use" -- byte-perfect content is not sufficient on its own.
    payload = b"perfectly correct bytes and hash and size"
    report = verify_and_consume(
        declared_hash=_sha256(payload),
        declared_size=len(payload),
        actual_bytes=payload,
        custody_verified=False,
    )
    assert report["status"] == "REJECTED", (
        "byte-perfect content with unverified custody must still be rejected"
    )
    assert report["reasons"] == ("CUSTODY_NOT_VERIFIED",)


def test_ap4_multiple_simultaneous_violations_are_all_reported() -> None:
    payload = b"some bytes"
    report = verify_and_consume(
        declared_hash=_sha256(b"different bytes entirely"),
        declared_size=len(payload) + 5,
        actual_bytes=payload,
        custody_verified=False,
    )
    assert report["status"] == "REJECTED"
    assert set(report["reasons"]) == {"HASH_MISMATCH", "SIZE_MISMATCH", "CUSTODY_NOT_VERIFIED"}, (
        "every violation found must be reported, not just the first one"
    )


def test_ap5_empty_payload_edge_case_is_handled_correctly() -> None:
    payload = b""
    report = verify_and_consume(
        declared_hash=_sha256(payload),
        declared_size=0,
        actual_bytes=payload,
        custody_verified=True,
    )
    assert report["status"] == "CONSUMED"


def test_ap6_single_byte_content_difference_still_blocks_consumption() -> None:
    correct = b"the correct byte string ends in A"
    altered = correct[:-1] + b"B"  # one byte different
    report = verify_and_consume(
        declared_hash=_sha256(correct),
        declared_size=len(correct),
        actual_bytes=altered,
        custody_verified=True,
    )
    assert report["status"] == "REJECTED"
    assert "HASH_MISMATCH" in report["reasons"]


def test_ap7_pure_function_no_hidden_state_and_input_not_mutated() -> None:
    payload = bytearray(b"mutable-looking payload")
    frozen_copy = bytes(payload)

    first = verify_and_consume(
        declared_hash=_sha256(bytes(payload)),
        declared_size=len(payload),
        actual_bytes=payload,
        custody_verified=True,
    )
    second = verify_and_consume(
        declared_hash=_sha256(b"unrelated"),
        declared_size=999,
        actual_bytes=payload,
        custody_verified=False,
    )
    assert first["status"] == "CONSUMED"
    assert second["status"] == "REJECTED", (
        "a prior CONSUMED result must never leak into an unrelated later call"
    )
    assert bytes(payload) == frozen_copy, "verify_and_consume must never mutate the caller's bytes"


def test_ap8_output_shape_and_status_reason_coupling_is_consistent() -> None:
    payload = b"shape-check payload"
    consumed = verify_and_consume(
        declared_hash=_sha256(payload),
        declared_size=len(payload),
        actual_bytes=payload,
        custody_verified=True,
    )
    rejected = verify_and_consume(
        declared_hash=_sha256(b"wrong"),
        declared_size=len(payload),
        actual_bytes=payload,
        custody_verified=True,
    )
    assert set(consumed.keys()) == _EXPECTED_KEYS
    assert set(rejected.keys()) == _EXPECTED_KEYS
    assert (consumed["status"] == "CONSUMED") == (consumed["reasons"] == ())
    assert (rejected["status"] == "REJECTED") == (len(rejected["reasons"]) > 0)


ARTIFACT_POINTER_CONSUMPTION_GUARD_TESTS = [
    test_ap0_golden_path_matching_bytes_and_verified_custody_is_consumed,
    test_ap1_hash_mismatch_alone_blocks_consumption,
    test_ap2_size_mismatch_alone_blocks_consumption,
    test_ap3_byte_perfect_content_with_unverified_custody_is_still_rejected,
    test_ap4_multiple_simultaneous_violations_are_all_reported,
    test_ap5_empty_payload_edge_case_is_handled_correctly,
    test_ap6_single_byte_content_difference_still_blocks_consumption,
    test_ap7_pure_function_no_hidden_state_and_input_not_mutated,
    test_ap8_output_shape_and_status_reason_coupling_is_consistent,
]


def selftest() -> int:
    failures: list[tuple[str, str]] = []
    for test in ARTIFACT_POINTER_CONSUMPTION_GUARD_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report, don't hide
            failures.append((test.__name__, f"{type(exc).__name__}: {exc}"))

    print(f"signalvev_reference_v13_validation selftest: "
          f"{len(ARTIFACT_POINTER_CONSUMPTION_GUARD_TESTS) - len(failures)}/"
          f"{len(ARTIFACT_POINTER_CONSUMPTION_GUARD_TESTS)} PASS")
    for name, err in failures:
        print(f"  FAIL {name}: {err}")

    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selftest"])
    args = parser.parse_args()
    if args.command == "selftest":
        return selftest()
    return 2


if __name__ == "__main__":
    sys.exit(main())
