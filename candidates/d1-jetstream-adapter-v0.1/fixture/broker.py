"""Own exactly one nats-server subprocess on loopback with a private store; never touch any other process/path."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


class FixtureUnavailable(RuntimeError):
    """The real broker could not be started: the run is UNRUN, never a stub PASS."""


def check_new_private_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if path.exists():
        raise SystemExit(f"WORK_DIRECTORY_ALREADY_EXISTS: {path}")
    if len(path.parts) < 4 or path == Path.home() or path.parent == path:
        raise SystemExit(f"WORK_DIRECTORY_TOO_BROAD: {path}")
    if not path.parent.is_dir():
        raise SystemExit(f"WORK_PARENT_MISSING: {path.parent}")
    path.mkdir(mode=0o700)
    return path


def remove_checked(target: Path, work: Path) -> None:
    """Delete only a directory this run created INSIDE its own work directory."""
    target, work = Path(target).resolve(), Path(work).resolve()
    if work not in target.parents or target == work:
        raise RuntimeError(f"REFUSING_CLEANUP_OUTSIDE_WORK: {target}")
    if target.exists():
        shutil.rmtree(target)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrokerProcess:
    def __init__(self, binary: Path, work: Path) -> None:
        self.binary = Path(binary).resolve()
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise FixtureUnavailable(f"NATS_SERVER_BINARY_MISSING_OR_NOT_EXECUTABLE: {self.binary}")
        self.work = Path(work)
        self.store = self.work / "store"
        self.log_path = self.work / "logs" / "nats-server.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.starts = 0

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    def version(self) -> str:
        out = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            raise FixtureUnavailable(f"NATS_SERVER_VERSION_FAILED: {out.stderr.strip()[:200]}")
        return out.stdout.strip()

    def start(self) -> None:
        if self.proc is not None:
            raise RuntimeError("ALREADY_RUNNING")
        self.store.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "ab")
        log.write(f"\n=== fixture start #{self.starts + 1} port {self.port} store {self.store} ===\n".encode())
        log.flush()
        try:
            self.proc = subprocess.Popen([str(self.binary), "-a", "127.0.0.1", "-p", str(self.port), "-js",
                                          "-sd", str(self.store)], stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
        finally:
            log.close()
        self.starts += 1
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise FixtureUnavailable(f"NATS_SERVER_EXITED rc={self.proc.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    pass
                if b"Server is ready" in self.log_path.read_bytes().split(b"=== fixture start")[-1]:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.stop()
        raise FixtureUnavailable("NATS_SERVER_NOT_READY_IN_15S")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()                       # only our own PID
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(10)

    def restart(self) -> None:
        self.stop()
        self.start()
