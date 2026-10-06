"""Explicit Source-host entry for one existing PM-ready receipt.

The host supplies already constructed, trusted runtime objects. This module
never reads credentials or discovers providers from configuration.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from .pm_x9 import (MODE_OFF, ConsumeResult, DepositRecord, HintSendResult,
                    PmX9Settings, PmX9Unbound, build_binding, load_ports)


@dataclass(frozen=True)
class HostRunResult:
    state: str
    send: HintSendResult | None = None
    deposit: DepositRecord | None = None
    consume: ConsumeResult | None = None
    error_type: str | None = None
    close_error_type: str | None = None


def compose(settings: PmX9Settings, bindings: Any, *, host_authorized: bool = False):
    """Compose only from the server's explicit LiveRuntimeBindings instance."""
    if host_authorized is not True:
        raise PmX9Unbound("PM_X9_HOST_CHOICE_REQUIRED")
    if not isinstance(settings, PmX9Settings) or settings.mode == MODE_OFF:
        raise PmX9Unbound("PM_X9_DEFAULT_OFF")
    from providers.pm_x9_live_host import LiveRuntimeBindings
    if type(bindings) is not LiveRuntimeBindings or bindings.enabled is not True:
        raise PmX9Unbound("EXPLICIT_CURRENT_HOST_RUNTIME_REQUIRED")
    ports = load_ports(settings, host_runtime=bindings)
    return build_binding(settings, ports)


def run_one_existing_ready_receipt(
    settings: PmX9Settings, bindings: Any, receipt_ref: str, *,
    host_authorized: bool = False, receiver_consume_authorized: bool = False,
    ingress_timeout_seconds: float = 5.0,
) -> HostRunResult:
    """One authorized receipt, bounded ingress, exact deposit, and fresh PM-backed consume.

    An uncertain send or receipt is returned as uncertain and is never retried.
    The caller owns the actual PM/X9 provider instances and durable receipts.
    """
    if host_authorized is not True or receiver_consume_authorized is not True:
        raise PmX9Unbound("PM_X9_HOST_CHOICE_REQUIRED")
    if not isinstance(receipt_ref, str) or not receipt_ref.strip():
        raise PmX9Unbound("PM_RECEIPT_REF_REQUIRED")
    if (type(ingress_timeout_seconds) not in (int, float)
            or not 0 < ingress_timeout_seconds <= 30):
        raise PmX9Unbound("INGRESS_TIMEOUT_INVALID")
    binding = compose(settings, bindings, host_authorized=True)
    result = HostRunResult("OPERATION_UNCONFIRMED")
    sent = None
    deposit = None
    try:
        binding.start_listener()
        binding.open_sender()
        sent = binding.send_hint(receipt_ref)
        if sent.state != "TRANSPORT_ACCEPTED" or sent.replay_refused or not sent.event_id:
            result = HostRunResult("SEND_UNCONFIRMED", send=sent)
        else:
            deadline = time.monotonic() + ingress_timeout_seconds
            while deposit is None:
                deposit = next((r for r in reversed(binding.sink.records)
                                if r.event_id == sent.event_id), None)
                if deposit is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not binding.wait_for_ingress(binding.ingress_count + 1, timeout=remaining):
                    break
            if deposit is None:
                result = HostRunResult("INGRESS_UNCONFIRMED", send=sent)
            elif deposit.state != "DEPOSITED_READBACK" or deposit.queued_for_pulse is not True:
                result = HostRunResult("DEPOSIT_UNCONFIRMED", send=sent, deposit=deposit)
            else:
                consumed = binding.consume_one(sent.event_id)
                proven = (consumed.event_id == sent.event_id
                          and consumed.state in {"DISPOSITION_READBACK", "ALREADY_DISPOSED"}
                          and consumed.record is not None)
                result = HostRunResult("DISPOSITION_READBACK" if proven else "CONSUME_UNCONFIRMED",
                                       send=sent, deposit=deposit, consume=consumed)
    except Exception as exc:
        result = HostRunResult("OPERATION_UNCONFIRMED", send=sent,
                               deposit=deposit,
                               error_type=type(exc).__name__)
    finally:
        try:
            binding.close()
        except Exception as exc:
            result = replace(result, state="CLOSE_UNCONFIRMED",
                             close_error_type=type(exc).__name__)
    return result
