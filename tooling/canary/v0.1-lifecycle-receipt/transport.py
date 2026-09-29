#!/usr/bin/env python3
"""Transport abstraction for the v0.1 bounded canary.

STATUS: canary preparation artifact. authority: NONE. Not yet executed.

Two implementations:

  InMemoryTransport -- a plain in-process queue. Used by
  offline_selftest.py to prove producer.py/consumer.py's message
  construction, validation, hashing and evidence logic is correct
  WITHOUT any real network or broker. This is what "prepared and
  self-tested now" means for this canary.

  NatsTransport -- a thin wrapper over the real nats-py client. It is
  ONLY ever instantiated when NATS_URL is explicitly set in the
  environment; nothing in this module or in producer.py/consumer.py
  connects anywhere by default. Importing this module never opens a
  socket; `nats` itself is imported lazily inside NatsTransport.connect()
  so a machine without nats-py installed (this sandbox, notably -- see
  RUNBOOK.md) can still import and unit-test everything else here.

  NatsTransport carries no default server URL, no embedded credentials,
  and no JetStream context -- core NATS pub/sub only, matching PRINCIPAL
  DECISION scope ("Ingen JetStream, varig lagring").
"""
from __future__ import annotations

import queue
from typing import Protocol


class Transport(Protocol):
    def connect(self) -> None: ...
    def publish(self, subject: str, payload: bytes) -> None: ...
    def next_message(self, subject: str, timeout_seconds: float) -> bytes | None: ...
    def close(self) -> None: ...


class InMemoryTransport:
    """Fake transport for offline self-test. Two instances sharing the
    same `shared_queue` stand in for one producer and one consumer on
    one subject."""

    def __init__(self, shared_queue: "queue.Queue[bytes]") -> None:
        self._queue = shared_queue
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def publish(self, subject: str, payload: bytes) -> None:
        assert self._connected, "publish() before connect()"
        # subject is accepted for interface parity; InMemoryTransport is
        # single-subject by construction in offline_selftest.py.
        self._queue.put(payload)

    def next_message(self, subject: str, timeout_seconds: float) -> bytes | None:
        assert self._connected, "next_message() before connect()"
        try:
            return self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._connected = False


class NatsTransport:
    """Real NATS transport. Not exercised in this sandbox (nats-py is
    not installable here -- confirmed 2026-09-29, see RUNBOOK.md).
    Requires an explicit, scoped NATS_URL; never guesses or defaults to
    a server. Core pub/sub only -- no JetStream context is created."""

    def __init__(self, url: str, *, creds_path: str | None = None) -> None:
        if not url:
            raise ValueError("NatsTransport requires a non-empty url (no implicit default)")
        self._url = url
        self._creds_path = creds_path
        self._nc = None
        self._sub = None

    def connect(self) -> None:
        import nats  # lazy import: only required when actually connecting
        import asyncio

        async def _connect():
            kwargs = {"servers": [self._url]}
            if self._creds_path:
                kwargs["user_credentials"] = self._creds_path
            return await nats.connect(**kwargs)

        self._loop = asyncio.new_event_loop()
        self._nc = self._loop.run_until_complete(_connect())

    def publish(self, subject: str, payload: bytes) -> None:
        assert self._nc is not None, "publish() before connect()"

        async def _pub():
            await self._nc.publish(subject, payload)
            await self._nc.flush()

        self._loop.run_until_complete(_pub())

    def next_message(self, subject: str, timeout_seconds: float):
        assert self._nc is not None, "next_message() before connect()"

        async def _sub_once():
            sub = await self._nc.subscribe(subject)
            try:
                msg = await sub.next_msg(timeout=timeout_seconds)
                return msg.data
            except TimeoutError:
                return None
            finally:
                await sub.unsubscribe()

        return self._loop.run_until_complete(_sub_once())

    def close(self) -> None:
        if self._nc is not None:
            self._loop.run_until_complete(self._nc.close())
        self._nc = None
