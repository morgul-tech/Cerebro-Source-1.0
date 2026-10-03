"""Trusted owner-commit receipt adapter for A7-01.

A raw payload saying COMMITTED_READBACK is never authentication.  This adapter
accepts only a constructor-bound reader that returns a verified owner receipt,
then materialises the existing OwnerEvent shape from that verified evidence.

Candidate only: authority NONE, no network/provider access, no live effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .model import HEX64, SensingError, is_id
from .owner_event import COMMITTED, OwnerEvent, accept_owner_event


@dataclass(frozen=True)
class VerifiedOwnerCommitReceipt:
    receipt_ref: str
    receipt_fingerprint: str
    owner_ref: str
    provider_revision: int
    readback_ref: str
    observed_at: str
    event_payload: Mapping[str, Any]
    verified: bool = True
    readback_verified: bool = True


@dataclass(frozen=True)
class TrustedOwnerEvent:
    event: OwnerEvent
    receipt_ref: str
    receipt_fingerprint: str
    provider_revision: int


class TrustedOwnerCommitReader(Protocol):
    def read_verified(
        self, receipt_ref: str, *, expected_owner_ref: str
    ) -> VerifiedOwnerCommitReceipt: ...


def _need(cond: bool, code: str, detail: str = "") -> None:
    if not cond:
        raise SensingError(code, detail)


def read_trusted_owner_event(
    reader: TrustedOwnerCommitReader,
    *,
    receipt_ref: str,
    expected_owner_ref: str,
) -> TrustedOwnerEvent:
    """Read and validate an owner-committed event through a trusted receipt reader.

    The returned OwnerEvent's commit evidence is constructed from the verified
    receipt.  It is never accepted from caller/event text.
    """
    _need(is_id(receipt_ref), "OWNER_RECEIPT_REF_INVALID")
    _need(is_id(expected_owner_ref), "OWNER_RECEIPT_OWNER_INVALID")

    receipt = reader.read_verified(receipt_ref, expected_owner_ref=expected_owner_ref)
    _need(
        isinstance(receipt, VerifiedOwnerCommitReceipt),
        "OWNER_RECEIPT_READER_RESULT_INVALID",
    )
    _need(
        receipt.verified is True and receipt.readback_verified is True,
        "OWNER_RECEIPT_NOT_VERIFIED",
    )
    _need(
        receipt.owner_ref == expected_owner_ref,
        "OWNER_RECEIPT_OWNER_MISMATCH",
    )
    _need(
        receipt.receipt_ref == receipt_ref and is_id(receipt.receipt_ref),
        "OWNER_RECEIPT_REF_MISMATCH",
    )
    _need(
        isinstance(receipt.receipt_fingerprint, str)
        and bool(HEX64.match(receipt.receipt_fingerprint)),
        "OWNER_RECEIPT_FINGERPRINT_INVALID",
    )
    _need(
        type(receipt.provider_revision) is int and receipt.provider_revision >= 0,
        "OWNER_RECEIPT_PROVIDER_REVISION_INVALID",
    )
    _need(is_id(receipt.readback_ref), "OWNER_RECEIPT_READBACK_REF_INVALID")
    _need(
        isinstance(receipt.observed_at, str) and bool(receipt.observed_at),
        "OWNER_RECEIPT_OBSERVED_AT_INVALID",
    )
    _need(
        isinstance(receipt.event_payload, Mapping),
        "OWNER_RECEIPT_EVENT_INVALID",
    )
    _need(
        "commit" not in receipt.event_payload,
        "OWNER_RECEIPT_EVENT_MUST_NOT_SELF_ATTEST",
    )

    raw = dict(receipt.event_payload)
    _need(
        raw.get("owner_ref") == expected_owner_ref,
        "OWNER_RECEIPT_EVENT_OWNER_MISMATCH",
    )
    raw["commit"] = {
        "state": COMMITTED,
        "readback_ref": receipt.readback_ref,
        "observed_at": receipt.observed_at,
    }
    event = accept_owner_event(raw)
    return TrustedOwnerEvent(
        event=event,
        receipt_ref=receipt.receipt_ref,
        receipt_fingerprint=receipt.receipt_fingerprint,
        provider_revision=receipt.provider_revision,
    )
