"""Stream transport seam used by the PullAckPort and the publisher.

``NatsJetStreamTransport`` (nats_transport.py) is the real nats-py binding used by the fixture harness.
``InMemoryStreamTransport`` below is a SYNTHETIC_TEST_ONLY double for unit tests only; it is never reported as broker
evidence. Both expose the same small surface so the composition is exercised identically.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class TransportError(RuntimeError):
    pass


class CapacityRejected(TransportError):
    """Broker refused the publication (DiscardNew limits / message size). Nothing old was evicted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PublishUnknown(TransportError):
    """Publication outcome unknown (timeout / lost PubAck). Reconcile before any retry."""


class LookupUnavailable(TransportError):
    """Stream lookup could not be completed: no proof either way."""


class AckUnconfirmed(TransportError):
    """The ACK was not confirmed by the server."""


@dataclass(frozen=True)
class PubAckInfo:
    stream: str
    seq: int
    duplicate: bool


@dataclass
class RawDelivery:
    data: bytes
    headers: dict[str, str]
    stream: str
    stream_seq: int
    consumer_seq: int
    num_delivered: int
    published_epoch: float
    handle: Any = None


class StreamTransport(Protocol):
    def ensure(self, config: Any) -> dict[str, Any]: ...
    def publish(self, subject: str, payload: bytes, msg_id: str, timeout: float) -> PubAckInfo: ...
    def fetch_one(self, timeout: float) -> RawDelivery | None: ...
    def ack(self, delivery: RawDelivery) -> None: ...
    def nak(self, delivery: RawDelivery, delay: float) -> None: ...
    def stream_state(self, timeout: float | None = None) -> dict[str, int]: ...
    def get_msg(self, seq: int, timeout: float | None = None) -> tuple[bytes, dict[str, str]] | None: ...
    def consumer_state(self) -> dict[str, int]: ...
    def poll_advisories(self) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...


@dataclass
class _Stored:
    seq: int
    data: bytes
    headers: dict[str, str]
    at: float
    deliveries: int = 0
    acked: bool = False
    redeliver_at: float = 0.0
    exhausted: bool = False


class InMemoryStreamTransport:
    """SYNTHETIC_TEST_ONLY double: limits/DiscardNew, Nats-Msg-Id duplicate window, AckExplicit with ack_wait
    redelivery, max_deliver + advisory, MaxAge expiry. Driven by an injected clock. Not broker evidence."""

    SYNTHETIC_TEST_ONLY = True

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self.cfg: Any = None
        self.msgs: dict[int, _Stored] = {}
        self.last_seq = 0
        self.consumer_seq = 0
        self.dupe_ids: dict[str, tuple[int, float]] = {}
        self.advisories: list[dict[str, Any]] = []
        self.inflight: int | None = None
        self.fail: dict[str, str] = {}          # op -> "raise"
        self.lose_puback = False

    def ensure(self, config: Any) -> dict[str, Any]:
        self.cfg = config
        return {"stream": config.stream, "consumer": config.consumer, "synthetic": True}

    def _expire(self) -> None:
        now = self.clock()
        for seq in [s for s, m in self.msgs.items() if now - m.at >= self.cfg.max_age_s]:
            del self.msgs[seq]
            if self.inflight == seq:
                self.inflight = None

    def publish(self, subject: str, payload: bytes, msg_id: str, timeout: float) -> PubAckInfo:
        with self._lock:
            if self.fail.get("publish") == "raise":
                raise PublishUnknown("synthetic publish failure")
            self._expire()
            now = self.clock()
            prior = self.dupe_ids.get(msg_id)
            if prior is not None and now - prior[1] < self.cfg.duplicate_window_s:
                return PubAckInfo(self.cfg.stream, prior[0], True)
            if len(payload) > self.cfg.max_msg_size:
                raise CapacityRejected("message size exceeds maximum allowed")
            if len(self.msgs) >= self.cfg.max_msgs:
                raise CapacityRejected("maximum messages exceeded")
            if sum(len(m.data) for m in self.msgs.values()) + len(payload) > self.cfg.max_bytes:
                raise CapacityRejected("maximum bytes exceeded")
            self.last_seq += 1
            self.msgs[self.last_seq] = _Stored(self.last_seq, payload, {"Nats-Msg-Id": msg_id}, now)
            self.dupe_ids[msg_id] = (self.last_seq, now)
            ack = PubAckInfo(self.cfg.stream, self.last_seq, False)
            if self.lose_puback:
                raise PublishUnknown("synthetic PubAck lost after write")
            return ack

    def fetch_one(self, timeout: float) -> RawDelivery | None:
        with self._lock:
            self._expire()
            now = self.clock()
            if self.inflight is not None:
                m = self.msgs.get(self.inflight)
                if m is None or m.acked:
                    self.inflight = None
                elif now < m.redeliver_at:
                    return None                         # max_ack_pending=1: one outstanding at a time
                else:
                    self.inflight = None
            for seq in sorted(self.msgs):
                m = self.msgs[seq]
                if m.acked or m.exhausted or now < m.redeliver_at:
                    continue
                if m.deliveries >= self.cfg.max_deliver:
                    m.exhausted = True
                    self.advisories.append({"type": "io.nats.jetstream.advisory.v1.max_deliver",
                                            "id": f"synthetic-{seq}-{m.deliveries}", "stream": self.cfg.stream,
                                            "consumer": self.cfg.consumer, "stream_seq": seq,
                                            "deliveries": m.deliveries})
                    continue
                m.deliveries += 1
                m.redeliver_at = now + self.cfg.ack_wait_s
                self.consumer_seq += 1
                self.inflight = seq
                return RawDelivery(m.data, dict(m.headers), self.cfg.stream, seq, self.consumer_seq, m.deliveries,
                                   m.at, handle=seq)
            return None

    def ack(self, delivery: RawDelivery) -> None:
        with self._lock:
            if self.fail.get("ack") == "raise":
                raise AckUnconfirmed("synthetic ack failure")
            m = self.msgs.get(delivery.handle)
            if m is not None:
                m.acked = True
            if self.inflight == delivery.handle:
                self.inflight = None

    def nak(self, delivery: RawDelivery, delay: float) -> None:
        with self._lock:
            m = self.msgs.get(delivery.handle)
            if m is not None:
                m.redeliver_at = self.clock() + delay
            if self.inflight == delivery.handle:
                self.inflight = None

    def stream_state(self, timeout: float | None = None) -> dict[str, int]:
        with self._lock:
            if self.fail.get("lookup") == "raise":
                raise LookupUnavailable("synthetic lookup failure")
            self._expire()
            seqs = sorted(self.msgs)
            return {"messages": len(seqs), "first_seq": seqs[0] if seqs else self.last_seq + 1,
                    "last_seq": self.last_seq, "bytes": sum(len(m.data) for m in self.msgs.values())}

    def get_msg(self, seq: int, timeout: float | None = None) -> tuple[bytes, dict[str, str]] | None:
        with self._lock:
            if self.fail.get("lookup") == "raise":
                raise LookupUnavailable("synthetic lookup failure")
            m = self.msgs.get(seq)
            return None if m is None else (m.data, dict(m.headers))

    def consumer_state(self) -> dict[str, int]:
        with self._lock:
            live = [m for m in self.msgs.values() if not m.acked and not m.exhausted]
            return {"num_pending": len([m for m in live if m.deliveries == 0]),
                    "num_ack_pending": len([m for m in live if m.deliveries > 0]),
                    "num_redelivered": len([m for m in self.msgs.values() if m.deliveries > 1])}

    def poll_advisories(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self.advisories = self.advisories, []
            return out

    def close(self) -> None:
        return None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
