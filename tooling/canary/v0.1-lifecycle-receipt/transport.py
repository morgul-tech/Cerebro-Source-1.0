#!/usr/bin/env python3
"""Transport abstraction for the v0.1 bounded canary.

STATUS: canary preparation artifact. authority: NONE. Not yet executed
against a real NATS server.

Two implementations:

  InMemoryTransport -- a plain in-process queue. Used by
  offline_selftest.py to prove producer.py/consumer.py's message
  construction, validation, hashing and evidence logic is correct
  WITHOUT any real network or broker.

  NatsTransport -- a thin wrapper over nats_stdlib_client.py, a
  hand-rolled, stdlib-only (socket + json) core NATS client. nats-py
  could not be installed in either execution environment tried for this
  canary (this project's cloud sandbox, and the isolated Linux VM
  device_bash runs in on the linked Windows machine) -- both proxy
  PyPI access with 403 Forbidden. Writing a minimal client instead also
  matches this project's own established convention (see
  signalvev_reference_v01_validation.py's docstring): "pure Python, no
  third-party dependencies." The client's wire-level framing is
  verified over a real (loopback) TCP socket by protocol_selftest.py;
  it has never connected to an actual NATS server.

  NatsTransport carries no default server, no embedded credentials, and
  no JetStream context -- core NATS pub/sub only, matching PRINCIPAL
  DECISION scope ("Ingen JetStream, varig lagring"). Importing this
  module never opens a socket; the client is only constructed inside
  NatsTransport.connect().
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
    """Real NATS transport, using the hand-rolled, dependency-free
    nats_stdlib_client.py. Requires an explicit `host:port` url (no
    scheme); never guesses or defaults to a server. Core pub/sub only
    -- no JetStream, no TLS, no clustering. One subscription (sid "1")
    at a time, which is all this bounded canary needs."""

    def __init__(self, url: str, *, creds_path: str | None = None,
                 user: str | None = None, password: str | None = None,
                 auth_token: str | None = None) -> None:
        if not url:
            raise ValueError("NatsTransport requires a non-empty url (no implicit default)")
        if creds_path is not None:
            raise NotImplementedError(
                ".creds (decentralized JWT) auth is not implemented in the stdlib "
                "client -- use user/password or auth_token, or deliberately extend "
                "nats_stdlib_client.py before relying on this."
            )
        host, _, port_str = url.partition(":")
        if not host or not port_str:
            raise ValueError(f"NatsTransport url must be 'host:port', got {url!r}")
        self._host = host
        self._port = int(port_str)
        self._user = user
        self._password = password
        self._auth_token = auth_token
        self._client = None

    def connect(self) -> None:
        import socket as _socket
        from nats_stdlib_client import NatsClient

        sock = _socket.create_connection((self._host, self._port), timeout=10.0)
        self._client = NatsClient(
            sock, timeout_seconds=10.0, name="cerebro-canary-v0.1",
            user=self._user, password=self._password, auth_token=self._auth_token,
        )

    def publish(self, subject: str, payload: bytes) -> None:
        assert self._client is not None, "publish() before connect()"
        self._client.publish(subject, payload)

    def next_message(self, subject: str, timeout_seconds: float) -> bytes | None:
        assert self._client is not None, "next_message() before connect()"
        if getattr(self, "_subscribed", None) != subject:
            self._client.subscribe(subject, sid="1")
            self._subscribed = subject
        return self._client.next_msg(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
