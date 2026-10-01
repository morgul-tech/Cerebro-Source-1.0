#!/usr/bin/env python3
"""Minimal fake NATS server -- TEST-ONLY, stdlib only, loopback-only.

STATUS: canary preparation / test artifact. authority: NONE. Never used
in the live canary run; used only by protocol_selftest.py to exercise
nats_stdlib_client.py's real wire framing over a real (but strictly
127.0.0.1, ephemeral-port) TCP socket, closing the gap flagged in
RUNBOOK.md: "NatsTransport is unverified against a real server."

This is NOT a NATS reimplementation for any purpose beyond that test.
It implements exact-subject PUB/SUB routing and CONNECT/+OK handshaking
only -- no wildcards, no queue groups, no auth beyond echoing whatever
was sent, no JetStream, no clustering. It exists purely so the client's
byte-level protocol handling can be verified without depending on a
real `nats-server` binary or external network access, neither of which
this sandbox currently has.
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any


class _Connection:
    def __init__(self, sock: socket.socket, server: "FakeNatsServer") -> None:
        self.sock = sock
        self.server = server
        self.buf = b""
        self.subs: dict[str, str] = {}  # subject -> sid
        self.alive = True

    def _readline(self) -> bytes | None:
        while b"\r\n" not in self.buf:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return None
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("client closed mid-payload")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def run(self) -> None:
        try:
            self.send(b'INFO {"server_id":"fake","version":"0.0.0-fake",'
                      b'"proto":1,"max_payload":1048576}\r\n')
            connect_line = self._readline()
            if connect_line is None or not connect_line.startswith(b"CONNECT "):
                return
            self.send(b"+OK\r\n")

            while True:
                line = self._readline()
                if line is None:
                    break
                if line.startswith(b"PUB "):
                    parts = line.decode("utf-8").split()
                    subject, nbytes = parts[1], int(parts[-1])
                    payload = self._read_exact(nbytes)
                    self._read_exact(2)  # trailing CRLF
                    self.send(b"+OK\r\n")
                    self.server.route(subject, payload)
                elif line.startswith(b"SUB "):
                    parts = line.decode("utf-8").split()
                    subject, sid = parts[1], parts[2]
                    self.subs[subject] = sid
                    self.server.register(self)
                    self.send(b"+OK\r\n")
                elif line.startswith(b"UNSUB "):
                    self.send(b"+OK\r\n")
                elif line.startswith(b"PING"):
                    self.send(b"PONG\r\n")
                elif line.strip() == b"":
                    continue
                else:
                    self.send(b"-ERR 'unknown protocol operation'\r\n")
        except OSError:
            pass
        finally:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass


class FakeNatsServer:
    """Binds to 127.0.0.1:0 (OS-assigned ephemeral port), accepts
    connections in a background thread. Loopback-only by construction."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.host, self.port = self._listener.getsockname()
        self._connections: list[_Connection] = []
        self._lock = threading.Lock()
        self._stop = False
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        self._listener.settimeout(0.5)
        while not self._stop:
            try:
                sock, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn = _Connection(sock, self)
            threading.Thread(target=conn.run, daemon=True).start()

    def register(self, conn: _Connection) -> None:
        with self._lock:
            if conn not in self._connections:
                self._connections.append(conn)

    def route(self, subject: str, payload: bytes) -> None:
        with self._lock:
            targets = list(self._connections)
        for conn in targets:
            sid = conn.subs.get(subject)
            if sid is None or not conn.alive:
                continue
            header = f"MSG {subject} {sid} {len(payload)}\r\n".encode("utf-8")
            try:
                conn.send(header + payload + b"\r\n")
            except OSError:
                pass

    def stop(self) -> None:
        self._stop = True
        try:
            self._listener.close()
        except OSError:
            pass
