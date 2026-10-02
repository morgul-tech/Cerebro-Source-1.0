"""3. TRANSPORT ABSTRACTION. A narrow interface + an offline FakeTransport + a Core NATS ADAPTER over an INJECTED client.

No nats import, no socket, no credentials, no server. The adapter only maps an injected client's outcome into the
three honest sender states. Any failure that is not PROVEN not-written is UNKNOWN_SEND (=> NO_REPLAY upstream).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

ACCEPTED = "ACCEPTED"
NOT_SENT = "NOT_SENT"
UNKNOWN_SEND = "UNKNOWN_SEND"
Handler = Callable[[bytes], object]


class NotSentError(Exception):
    """An injected client raises this ONLY when it can prove nothing was written to the wire."""


@dataclass(frozen=True)
class TransportResult:
    state: str          # ACCEPTED | NOT_SENT | UNKNOWN_SEND
    reason: str


class Transport(Protocol):
    def publish(self, subject: str, data: bytes) -> TransportResult: ...
    def subscribe(self, subject: str, handler: Handler) -> None: ...


class CoreNatsAdapter:
    """Core NATS only (no JetStream). `client` is any object exposing publish(subject, data), flush(timeout) and
    subscribe(subject, handler) synchronously; a real binding (future owner) wraps nats-py behind this shape.
    ACCEPTED means the server acknowledged the flush: TRANSPORT_ACCEPTED, never DELIVERED, READ or WORK."""

    def __init__(self, client, *, flush_timeout: float = 2.0) -> None:
        self._client, self._flush_timeout = client, flush_timeout

    def publish(self, subject: str, data: bytes) -> TransportResult:
        try:
            self._client.publish(subject, data)
        except NotSentError:
            return TransportResult(NOT_SENT, "CLIENT_PROVED_NOT_WRITTEN")
        except Exception:  # noqa: BLE001 - deliberately broad: anything else may have been written
            return TransportResult(UNKNOWN_SEND, "PUBLISH_FAILED_WRITE_STATE_UNPROVEN")
        try:
            self._client.flush(self._flush_timeout)
        except Exception:  # noqa: BLE001
            return TransportResult(UNKNOWN_SEND, "FLUSH_FAILED_SERVER_RECEIPT_UNPROVEN")
        return TransportResult(ACCEPTED, "SERVER_FLUSH_OK")

    def subscribe(self, subject: str, handler: Handler) -> None:
        self._client.subscribe(subject, handler)


class FakeTransport:
    """Offline loopback bus for tests. mode: OK | UNKNOWN | NOT_SENT. deliver_on_unknown simulates 'it did arrive'."""

    def __init__(self, *, mode: str = "OK", deliver_on_unknown: bool = False) -> None:
        self.mode, self.deliver_on_unknown = mode, deliver_on_unknown
        self.published: list[tuple[str, bytes]] = []
        self._subs: dict[str, list[Handler]] = {}

    def reset_subscribers(self) -> None:
        self._subs = {}

    def subscribe(self, subject: str, handler: Handler) -> None:
        self._subs.setdefault(subject, []).append(handler)

    def publish(self, subject: str, data: bytes) -> TransportResult:
        if self.mode == "NOT_SENT":
            return TransportResult(NOT_SENT, "FAKE_NOT_SENT")
        self.published.append((subject, data))
        if self.mode == "OK" or self.deliver_on_unknown:
            for handler in list(self._subs.get(subject, [])):
                handler(data)
        if self.mode == "UNKNOWN":
            return TransportResult(UNKNOWN_SEND, "FAKE_UNKNOWN")
        return TransportResult(ACCEPTED, "FAKE_ACCEPTED")
