"""Advisory single-owner lock for an evidence directory + role (sender | receiver).

Two processes appending to the same JSONL evidence files could corrupt them or reopen a send; the OS releases this
lock when the process exits, so a crash leaves no stale lock. Not a coordination service.
"""
from __future__ import annotations

import os
from pathlib import Path

from .errors import EvidenceBusyError

try:  # POSIX
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


class EvidenceLock:
    def __init__(self, directory: Path, role: str) -> None:
        self._path = Path(directory) / f"{role}.lock"
        self._fd: int | None = None
        self.role = role

    def acquire(self) -> "EvidenceLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            raise EvidenceBusyError("EVIDENCE_DIR_BUSY", f"another process holds the {self.role} role here") from None
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                _unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None
