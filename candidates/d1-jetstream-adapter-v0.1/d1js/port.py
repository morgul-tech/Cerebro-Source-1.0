"""JetStream PullAckPort for the historical D1HybridGateway (pull_one / ack / noack).

Contract kept from the gateway: one pointer per call, explicit ACK/NOACK, nothing re-materialised on ACK loss.
Added here (the missing transit delta):
  * delivery_id is bound to the actual stream/consumer delivery metadata
    ``<stream>/<consumer>/<receiver>/s<stream_seq>/c<consumer_seq>/d<num_delivered>`` (no collision across
    stream/receiver/restart: stream_seq is unique in the stream, consumer_seq and num_delivered per delivery);
  * strict pointer validation (schema/identity/digest/TTL/receiver) before the gateway sees anything;
  * every terminal or negative transit ACK first needs a durable disposition write + independent exact readback
    bound to the pointer bytes and stream sequence; a failed write/readback leaves the message un-ACKed;
  * a recorded terminal disposition for the same stream sequence is honoured on redelivery (crash after disposition
    before ACK) without re-entering the gateway or re-materialising;
  * per-drain coalescing of compatible HINTs only, from a fresh owner reread in the same lease epoch;
  * the safe-boundary lease is re-checked before pull, after pull, and before ACK.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .boundary import Lease, LeaseLost
from .faults import Faults
from .journal import JournalUnproven, ReceiverJournal
from .pointer import COALESCIBLE_KINDS, PointerReject, is_expired, parse_pointer
from .transport import RawDelivery, StreamTransport

D1_MAP = {"SELECTED_RETURN_CLOSED": "CONSUMED_SELECTED_RETURN", "STALE_SUPERSEDED": "STALE",
          "READ_NOT_MATERIAL": "NO_MATERIAL_DELTA", "NOT_APPLICABLE": "NOT_APPLICABLE"}
SETTLING = frozenset({"CONSUMED_SELECTED_RETURN", "STALE", "NO_MATERIAL_DELTA", "NOT_APPLICABLE"})


def d1_disposition(gateway_disposition: str) -> str:
    return D1_MAP.get(gateway_disposition, gateway_disposition)


@dataclass
class PortEvent:
    """What the port actually did for the last delivery (the composition reports it next to GatewayResult)."""
    kind: str                         # GATEWAY | SIDE_TERMINAL | SIDE_HOLD | RECOVERED | DEFER | NONE
    d1_disposition: str | None = None
    reason: str | None = None
    transit_state: str = "NOT_TOUCHED"
    delivery_id: str | None = None
    stream_seq: int | None = None
    num_delivered: int | None = None
    message_id: str | None = None
    receipt_ref: str | None = None


@dataclass
class _Inflight:
    raw: RawDelivery
    pointer: Any
    delivery_id: str


class JetStreamPullAckPort:
    def __init__(self, *, gw: Any, transport: StreamTransport, journal: ReceiverJournal, owner_reader: Any,
                 return_mapping: Any, config: Any, receiver_ref: str, clock: Callable[[], datetime],
                 faults: Faults, host: Any = None) -> None:
        self._gw, self._t, self._j = gw, transport, journal
        self._owner, self._return, self._cfg = owner_reader, return_mapping, config
        self._receiver, self._clock, self.faults, self._host = receiver_ref, clock, faults, host
        self.lease: Lease | None = None
        self.inflight: dict[str, _Inflight] = {}
        self.events: list[PortEvent] = []

    # ------------------------------------------------------------------ helpers
    def _delivery_id(self, raw: RawDelivery) -> str:
        return (f"{raw.stream}/{self._cfg.consumer}/{self._receiver}/s{raw.stream_seq}/c{raw.consumer_seq}"
                f"/d{raw.num_delivered}")

    def _require_lease(self, seam: str) -> None:
        if self.lease is None:
            raise LeaseLost(f"{seam}:no-lease")
        self.lease.require(seam)

    def _terminal(self, raw: RawDelivery, delivery_id: str, *, pointer: Any, d1: str, gateway: str | None,
                  reason: str, receipt_ref: str | None, side: bool) -> PortEvent:
        """Durable disposition + independent readback, THEN the ACK. Any failure => no ACK."""
        ev = PortEvent("SIDE_TERMINAL" if side else "GATEWAY", d1, reason, "PENDING_NOACK", delivery_id,
                       raw.stream_seq, raw.num_delivered, pointer.message_id if pointer else None, receipt_ref)
        try:
            row = self._j.write_terminal(
                stream=raw.stream, stream_seq=raw.stream_seq, delivery_id=delivery_id,
                pointer_sha256=pointer.raw_sha256 if pointer else _sha(raw.data),
                message_id=pointer.message_id if pointer else raw.headers.get("Nats-Msg-Id"),
                owner_event_key=pointer.owner_event_key if pointer else None,
                owner_revision=pointer.owner_revision if pointer else None,
                d1_disposition=d1, gateway_disposition=gateway, reason=reason, receipt_ref=receipt_ref)
        except JournalUnproven as exc:
            ev.transit_state, ev.reason = "NOACK_DISPOSITION_UNPROVEN", f"{reason}|{exc}"
            self._j.bump("journal_unproven")
            self.events.append(ev)
            if side:
                return ev
            raise
        if row["d1_disposition"] != d1 or row["receipt_ref"] != receipt_ref:
            # An earlier lawful disposition for this exact stream/seq + bytes stands; honour it, never overwrite.
            ev.d1_disposition, ev.receipt_ref, ev.kind = row["d1_disposition"], row["receipt_ref"], "RECOVERED"
        return self._send_ack(raw, ev, side=side)

    def _send_ack(self, raw: RawDelivery, ev: PortEvent, *, side: bool) -> PortEvent:
        try:
            self._require_lease("ack")
            self.faults.hit("ack.before_send")
            self._t.ack(raw)
        except Exception as exc:
            ev.transit_state = "ACK_UNKNOWN" if not isinstance(exc, LeaseLost) else "NOACK_LEASE_LOST"
            self._j.bump("ack_unknown" if not isinstance(exc, LeaseLost) else "ack_withheld_lease_lost")
            self.events.append(ev)
            if side:
                return ev
            raise
        try:
            self.faults.hit("ack.after_send")              # server got the ACK, confirmation lost to us
        except Exception:
            ev.transit_state = "ACK_UNKNOWN"
            self._j.bump("ack_unknown")
            self.events.append(ev)
            if side:
                return ev
            raise
        ev.transit_state = "ACKED"
        self.events.append(ev)
        return ev

    def _hold(self, raw: RawDelivery, delivery_id: str, pointer: Any, reason: str, *, side: bool) -> PortEvent:
        self._j.record_hold(delivery_id=delivery_id, stream=raw.stream, stream_seq=raw.stream_seq,
                            message_id=pointer.message_id if pointer else raw.headers.get("Nats-Msg-Id"),
                            reason=reason)
        ev = PortEvent("SIDE_HOLD" if side else "GATEWAY", reason, reason, "PENDING_NOACK", delivery_id,
                       raw.stream_seq, raw.num_delivered, pointer.message_id if pointer else None)
        try:
            self._t.nak(raw, self._cfg.hold_nak_delay_s)
        except Exception:
            ev.transit_state = "PENDING_NOACK_UNKNOWN"
        self._j.bump("holds")
        self.events.append(ev)
        return ev

    # ------------------------------------------------------------------ PullAckPort
    def pull_one(self):
        self._require_lease("pull")
        if self._host is not None:
            self._host.fire("before_pull")                 # race hook: a Human turn may start right here
        self.faults.hit("lease.before_pull")
        self._require_lease("pull")
        raw = self._t.fetch_one(self._cfg.pull_timeout_s)
        if raw is None:
            return None
        delivery_id = self._delivery_id(raw)
        msg_id = raw.headers.get("Nats-Msg-Id")
        self._j.record_delivery(delivery_id=delivery_id, stream=raw.stream, stream_seq=raw.stream_seq,
                                consumer_seq=raw.consumer_seq, num_delivered=raw.num_delivered, message_id=msg_id,
                                pointer_sha256=_sha(raw.data))
        if raw.num_delivered > 1:
            self._j.bump("redeliveries")
        if self.lease is None or not self.lease.valid():  # lost between fetch and processing: defer, NOACK
            try:
                self._t.nak(raw, self._cfg.hold_nak_delay_s)
            except Exception:
                pass
            self.events.append(PortEvent("DEFER", "DEFER_LEASE_LOST", "lease lost after pull", "PENDING_NOACK",
                                         delivery_id, raw.stream_seq, raw.num_delivered, msg_id))
            self._j.bump("active_turn_deferrals")
            raise LeaseLost("after-pull")
        prior = self._j.terminal(raw.stream, raw.stream_seq)
        if prior is not None:
            if prior["pointer_sha256"] == _sha(raw.data):
                ev = PortEvent("RECOVERED", prior["d1_disposition"], "RECORDED_DISPOSITION_HONOURED",
                               "PENDING_NOACK", delivery_id, raw.stream_seq, raw.num_delivered, prior["message_id"],
                               prior["receipt_ref"])
                self._j.bump("recovered_recorded_disposition")
                self._send_ack(raw, ev, side=True)
                return None
            self._hold(raw, delivery_id, None, "CONFLICT_HOLD_RECORDED_OTHER_BYTES", side=True)
            return None
        try:
            pointer = parse_pointer(raw.data, max_ttl_seconds=self._cfg.pointer_ttl_s)
        except PointerReject as rej:
            self._j.bump("rejects_" + rej.code.lower())
            self._terminal(raw, delivery_id, pointer=None, d1=rej.code, gateway=None, reason=rej.detail or rej.code,
                           receipt_ref=None, side=True)
            return None
        if msg_id != pointer.message_id:
            self._j.bump("rejects_reject_identity")
            self._terminal(raw, delivery_id, pointer=pointer, d1="REJECT_IDENTITY", gateway=None,
                           reason="Nats-Msg-Id differs from pointer.message_id", receipt_ref=None, side=True)
            return None
        if is_expired(pointer, self._clock()):
            self._j.bump("expired")
            self._terminal(raw, delivery_id, pointer=pointer, d1="EXPIRED", gateway=None, reason="pointer TTL elapsed",
                           receipt_ref=None, side=True)
            return None
        if pointer.receiver_ref != self._receiver:
            try:
                verified = self._owner.verify_full_pointer(pointer) is True
            except Exception:
                verified = False
            if not verified:
                self._hold(raw, delivery_id, pointer, "HOLD_ROUTE_UNVERIFIED", side=True)
                return None
            self._terminal(raw, delivery_id, pointer=pointer, d1="NOT_APPLICABLE", gateway=None,
                           reason="owner-verified route for another receiver", receipt_ref=None, side=True)
            return None
        if pointer.kind in COALESCIBLE_KINDS:
            seen = self._owner.memo.get((pointer.tenant_ref, pointer.workspace_ref, pointer.project_ref,
                                         pointer.owner_event_key))
            if seen is not None and seen[1] == pointer.referent_id and seen[0] > pointer.owner_revision:
                self._j.bump("coalesced")
                self._terminal(raw, delivery_id, pointer=pointer, d1="STALE", gateway=None,
                               reason=f"COALESCED_BY_OWNER_REVISION_{seen[0]}_READ_THIS_DRAIN", receipt_ref=None,
                               side=True)
                return None
        self.inflight[delivery_id] = _Inflight(raw, pointer, delivery_id)
        return self._gw.OwnerPointer(delivery_id=delivery_id, tenant_ref=pointer.tenant_ref,
                                     workspace_ref=pointer.workspace_ref, project_ref=pointer.project_ref,
                                     owner_ref=pointer.owner_ref, owner_event_key=pointer.owner_event_key,
                                     owner_revision=pointer.owner_revision,
                                     event_fingerprint=pointer.event_fingerprint)

    def ack(self, delivery_id: str, disposition: str, receipt_ref: str | None) -> None:
        item = self.inflight.pop(delivery_id)
        d1 = d1_disposition(disposition)
        if d1 == "CONSUMED_SELECTED_RETURN":
            row = self._return.verify_receipt(receipt_ref) if receipt_ref else None
            if row is None or row["owner_event_key"] != item.pointer.owner_event_key \
                    or row["owner_event_fingerprint"] != item.pointer.event_fingerprint:
                self.inflight[delivery_id] = item
                raise JournalUnproven("return-receipt-not-independently-readable")
        self._terminal(item.raw, delivery_id, pointer=item.pointer, d1=d1, gateway=disposition, reason=disposition,
                       receipt_ref=receipt_ref, side=False)

    def noack(self, delivery_id: str, reason: str) -> None:
        item = self.inflight.pop(delivery_id)
        ev = self._hold(item.raw, delivery_id, item.pointer, reason, side=False)
        if ev.transit_state != "PENDING_NOACK":
            raise RuntimeError("NAK_UNCONFIRMED")


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()
