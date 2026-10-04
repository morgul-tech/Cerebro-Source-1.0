"""A minimal NATS CLIENT-PROTOCOL STUB on loopback TCP. TEST DOUBLE ONLY: it is NOT a NATS server/broker and NOT
evidence of a disposable-broker roundtrip. It lets the REAL nats-py client run over a REAL socket in the unit suite
(INFO/CONNECT/PING/PONG/SUB/UNSUB/PUB/MSG, literal subjects only) with fault switches for outage/flush-timeout tests."""
from __future__ import annotations

import json
import socket
import threading


class ProtocolStub:
    def __init__(self) -> None:
        self.port = 0
        self.pubs: list[tuple[str, bytes]] = []
        self.connect_count = 0
        self.pong_enabled = True            # False: answer only each connection's first PING (handshake), so flush() times out
        self.swallow_pubs = False           # True: accept PUBs but route them nowhere (a lost hint)
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._subs: list[tuple[socket.socket, bytes, str]] = []
        self._srv: socket.socket | None = None
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # ---------------------------------------------------------------- lifecycle
    def start(self, port: int = 0) -> int:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        srv.settimeout(0.1)
        self._srv, self._stopping = srv, False
        self.port = srv.getsockname()[1]
        t = threading.Thread(target=self._accept_loop, name="stub-accept")
        t.start()
        self._threads.append(t)
        return self.port

    def drop_connections(self) -> None:
        with self._lock:
            conns, self._conns, self._subs = self._conns, [], []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def stop(self) -> None:
        self._stopping = True
        if self._srv is not None:
            self._srv.close()
            self._srv = None
        self.drop_connections()
        for t in self._threads:
            t.join(3)
        self._threads = []

    # ---------------------------------------------------------------- protocol
    def _accept_loop(self) -> None:
        while not self._stopping:
            try:
                conn, _ = self._srv.accept()           # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
                self.connect_count += 1
            t = threading.Thread(target=self._serve, args=(conn,), name="stub-conn")
            t.start()
            self._threads.append(t)

    def _send(self, conn: socket.socket, data: bytes) -> None:
        try:
            conn.sendall(data)
        except OSError:
            pass

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rb")
        info = {"server_id": "STUB", "server_name": "protocol-stub", "version": "0.0.0-stub", "proto": 1, "max_payload": 1048576,
                "headers": True, "host": "127.0.0.1", "port": self.port}
        self._send(conn, b"INFO " + json.dumps(info).encode() + b"\r\n")
        pings = 0
        try:
            while True:
                line = f.readline()
                if not line:
                    return
                parts = line.strip().split(b" ")
                op = parts[0].upper()
                if op == b"PING":
                    pings += 1
                    if pings == 1 or self.pong_enabled:
                        self._send(conn, b"PONG\r\n")
                elif op == b"SUB":
                    with self._lock:
                        self._subs.append((conn, parts[-1], parts[1].decode()))
                elif op == b"UNSUB":
                    with self._lock:
                        self._subs = [s for s in self._subs if not (s[0] is conn and s[1] == parts[1])]
                elif op == b"PUB":
                    size = int(parts[-1])
                    payload = f.read(size)
                    f.read(2)
                    subject = parts[1].decode()
                    with self._lock:
                        self.pubs.append((subject, payload))
                        targets = [] if self.swallow_pubs else [(c, sid) for c, sid, subj in self._subs if subj == subject]
                    for c, sid in targets:
                        self._send(c, b"MSG " + subject.encode() + b" " + sid + b" " + str(len(payload)).encode() + b"\r\n" + payload + b"\r\n")
        except (OSError, ValueError):
            return
        finally:
            f.close()
