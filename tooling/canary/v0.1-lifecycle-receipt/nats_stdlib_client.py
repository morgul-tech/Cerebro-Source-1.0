#!/usr/bin/env python3
"""Minimal, dependency-free NATS core client -- stdlib only.

STATUS: canary preparation artifact. authority: NONE.

WHY THIS EXISTS: nats-py could not be installed in either execution
environment tried for this canary (this project's cloud sandbox, and
the isolated Linux VM device_bash runs in on the linked Windows
machine) -- both proxy PyPI access with a 403 Forbidden. Rather than
block on that, this implements just enough of the NATS core protocol
(no JetStream, no TLS, no clustering) to PUB, SUB and receive MSG
frames over a plain TCP socket. This also matches the project's own
established convention (see signalvev_reference_v01_validation.py's
own docstring): "pure Python, no third-party dependencies."

Protocol reference (core NATS, unauthenticated or user/pass/token auth
only): on connect the server sends one INFO line; the client replies
CONNECT with its options; thereafter PUB/SUB/UNSUB/MSG/PING/PONG frames
are newline-terminated ASCII headers optionally followed by a raw byte
payload and a trailing CRLF. With `verbose: true` (used here) the
server sends +OK after each client command that isn't PUB/SUB but
before a payload is not expected; here it is used to get a positive,
observable "the server accepted this exact PUB" signal for evidence
purposes, which is otherwise not available in non-verbose mode.

This file is exercised by two tests, both without any external network
dependency:
  - fake_nats_server.py + this file, driven over a real
    socket.socketpair() (an actual OS-level connected TCP pair, just
    local -- exercises the real wire framing/parsing, not a mock).
  - transport.py's NatsTransport, which uses this client and is in
    turn covered by offline_selftest.py via InMemoryTransport for the
    higher-level producer/consumer logic (this client itself is not
    used in that path -- see protocol_selftest.py for this file's own
    dedicated test).
"""
from __future__ import annotations

import json
import socket
from typing import Any


class NatsProtocolError(RuntimeError):
    pass


class NatsClient:
    """Synchronous, blocking, single-threaded core NATS client.
    One subscription at a time is all this canary needs."""

    def __init__(self, sock: socket.socket, *, timeout_seconds: float = 30.0,
                 name: str = "cerebro-canary", user: str | None = None,
                 password: str | None = None, auth_token: str | None = None) -> None:
        self._sock = sock
        self._sock.settimeout(timeout_seconds)
        self._buf = b""
        self._default_timeout = timeout_seconds
        self.server_info: dict[str, Any] = {}
        self._handshake(name=name, user=user, password=password, auth_token=auth_token)

    # -- low-level framing ------------------------------------------------

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise NatsProtocolError("connection closed by server")
        self._buf += chunk

    def _readline(self) -> bytes:
        while b"\r\n" not in self._buf:
            self._fill()
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _send(self, data: bytes) -> None:
        self._sock.sendall(data)

    # -- handshake ----------------------------------------------------------

    def _handshake(self, *, name: str, user: str | None, password: str | None,
                    auth_token: str | None) -> None:
        info_line = self._readline()
        if not info_line.startswith(b"INFO "):
            raise NatsProtocolError(f"expected INFO, got: {info_line!r}")
        self.server_info = json.loads(info_line[len(b"INFO "):])

        connect_opts: dict[str, Any] = {
            "verbose": True, "pedantic": False, "tls_required": False,
            "name": name, "lang": "python-stdlib", "version": "0.1.0",
            "protocol": 1, "headers": False,
        }
        if user is not None:
            connect_opts["user"] = user
        if password is not None:
            connect_opts["pass"] = password
        if auth_token is not None:
            connect_opts["auth_token"] = auth_token

        self._send(b"CONNECT " + json.dumps(connect_opts).encode("utf-8") + b"\r\n")
        self._expect_ok()

    def _expect_ok(self) -> None:
        line = self._read_control_line()
        if line.startswith(b"-ERR"):
            raise NatsProtocolError(f"server error: {line.decode('utf-8', 'replace')}")
        if not line.startswith(b"+OK"):
            raise NatsProtocolError(f"expected +OK, got: {line!r}")

    def _read_control_line(self) -> bytes:
        """Read one line, transparently answering any PING with PONG
        (the server may interleave keepalive pings at any point)."""
        line = self._readline()
        while line.startswith(b"PING"):
            self._send(b"PONG\r\n")
            line = self._readline()
        return line

    # -- public API -----------------------------------------------------

    def publish(self, subject: str, payload: bytes) -> None:
        header = f"PUB {subject} {len(payload)}\r\n".encode("utf-8")
        self._send(header + payload + b"\r\n")
        self._expect_ok()

    def subscribe(self, subject: str, sid: str = "1") -> None:
        self._send(f"SUB {subject} {sid}\r\n".encode("utf-8"))
        self._expect_ok()

    def unsubscribe(self, sid: str = "1") -> None:
        self._send(f"UNSUB {sid}\r\n".encode("utf-8"))
        self._expect_ok()

    def next_msg(self, timeout_seconds: float | None = None) -> bytes | None:
        """Blocks until one MSG payload is received, the given timeout
        elapses (returns None), or a protocol error occurs (raises)."""
        self._sock.settimeout(timeout_seconds if timeout_seconds is not None
                               else self._default_timeout)
        try:
            line = self._read_control_line()
        except socket.timeout:
            return None
        except NatsProtocolError:
            raise

        if line.startswith(b"MSG "):
            parts = line.decode("utf-8").split()
            # MSG <subject> <sid> [reply-to] <#bytes>
            nbytes = int(parts[-1])
            payload = self._read_exact(nbytes)
            self._read_exact(2)  # trailing CRLF after payload
            return payload
        if line.startswith(b"+OK"):
            # A stray +OK (e.g. from a prior command); keep waiting once.
            return self.next_msg(timeout_seconds=timeout_seconds)
        if line.startswith(b"-ERR"):
            raise NatsProtocolError(f"server error: {line.decode('utf-8', 'replace')}")
        raise NatsProtocolError(f"unexpected control line: {line!r}")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
