"""Liv-owned BK07 session lifetime; no OAuth, PM/X9, NATS or profile activation.

The lease is a local host fact, not an authenticated Context binding. A new
process always receives a new reference and must be bound again by Context's
provider-owned operator before the read-only resolver can be enabled.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

SCHEMA = "cerebro-bk07-local-runtime-session/v1"
PROFILE_SCHEMA = "cerebro-bk07-normal-use-profile/v1"
MAX_HEARTBEAT_AGE_SECONDS = 15


class LocalRuntimeSessionError(RuntimeError):
    pass


def _process_start_epoch(pid: int) -> float | None:
    """Return the OS creation time, including when a PID has been reused."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            if exited.dwHighDateTime or exited.dwLowDateTime:
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return (ticks - 116444736000000000) / 10000000
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        boot_line = next(line for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
                         if line.startswith("btime "))
        return int(boot_line.split()[1]) + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _lock_is_held(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        probe = _lock_file(path)
    except LocalRuntimeSessionError:
        return True
    probe.close()
    return False


def read_current_local_session(*, profile_path: Path, state_dir: Path,
                               clock: Callable[[], float] = time.time,
                               expected_session_ref: str | None = None) -> dict:
    """Fail closed unless the exact lease is fresh and its original process owns it."""
    host = LocalRuntimeSessionHost(profile_path=profile_path, state_dir=state_dir, clock=clock)
    host._profile_off()
    try:
        receipt = json.loads((Path(state_dir) / "session.json").read_text(encoding="utf-8"))
        now = clock()
        pid = receipt["pid"]
        started = receipt["process_start_epoch"]
        beat = receipt["heartbeat_at_epoch"]
        ref = receipt["session_ref"]
        valid = (receipt["schema"] == SCHEMA and receipt["state"] == "PREPARED_UNBOUND"
                 and isinstance(ref, str) and ref.startswith("local:")
                 and (expected_session_ref is None or ref == expected_session_ref)
                 and isinstance(started, (int, float)) and not isinstance(started, bool)
                 and isinstance(beat, (int, float)) and not isinstance(beat, bool)
                 and 0 <= now - beat <= MAX_HEARTBEAT_AGE_SECONDS
                 and _process_start_epoch(pid) == started
                 and _lock_is_held(Path(state_dir) / "session.lock"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LocalRuntimeSessionError("BK07_SESSION_NOT_CURRENT") from exc
    if not valid:
        raise LocalRuntimeSessionError("BK07_SESSION_NOT_CURRENT")
    return dict(receipt)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".session-", suffix=".tmp", delete=False) as stream:
            name = Path(stream.name)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def _lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        stream.seek(0)
        if stream.read(1) != b"1":
            stream.seek(0)
            stream.write(b"1")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except BaseException:
        stream.close()
        raise LocalRuntimeSessionError("BK07_SESSION_HOST_ALREADY_RUNNING") from None


class LocalRuntimeSessionHost:
    def __init__(self, *, profile_path: Path, state_dir: Path,
                 clock: Callable[[], float] = time.time):
        self.profile_path, self.state_dir, self.clock = Path(profile_path), Path(state_dir), clock
        self._lock = None
        self._receipt = None

    def _profile_off(self) -> None:
        try:
            profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LocalRuntimeSessionError("BK07_PROFILE_UNREADABLE") from exc
        if profile.get("schema") != PROFILE_SCHEMA or profile.get("enabled") is not False:
            raise LocalRuntimeSessionError("BK07_PROFILE_MUST_REMAIN_OFF")

    def start(self) -> dict:
        if self._lock is not None:
            raise LocalRuntimeSessionError("BK07_SESSION_HOST_ALREADY_STARTED")
        self._profile_off()
        self._lock = _lock_file(self.state_dir / "session.lock")
        now = self.clock()
        process_start = _process_start_epoch(os.getpid())
        if process_start is None:
            self._lock.close()
            self._lock = None
            raise LocalRuntimeSessionError("BK07_PROCESS_IDENTITY_UNAVAILABLE")
        self._receipt = {"schema": SCHEMA, "session_ref": "local:" + uuid.uuid4().hex,
                         "pid": os.getpid(), "started_at_epoch": now,
                         "process_start_epoch": process_start,
                         "heartbeat_at_epoch": now, "state": "PREPARED_UNBOUND"}
        _write_json(self.state_dir / "session.json", self._receipt)
        return dict(self._receipt)

    def heartbeat(self) -> dict:
        if self._lock is None or self._receipt is None:
            raise LocalRuntimeSessionError("BK07_SESSION_HOST_NOT_STARTED")
        self._profile_off()
        self._receipt["heartbeat_at_epoch"] = self.clock()
        _write_json(self.state_dir / "session.json", self._receipt)
        return dict(self._receipt)

    def stop(self) -> None:
        if self._lock is None:
            return
        if self._receipt is not None:
            self._receipt["state"] = "STOPPED"
            self._receipt["heartbeat_at_epoch"] = self.clock()
            _write_json(self.state_dir / "session.json", self._receipt)
        self._lock.close()
        self._lock = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Default-off Liv BK07 session host")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    with LocalRuntimeSessionHost(profile_path=args.profile, state_dir=args.state_dir) as host:
        while True:
            time.sleep(5)
            host.heartbeat()


if __name__ == "__main__":
    main()
