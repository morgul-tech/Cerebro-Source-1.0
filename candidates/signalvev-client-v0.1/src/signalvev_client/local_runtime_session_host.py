"""Liv-owned BK07 session lifetime; no OAuth, PM/X9, NATS or profile activation.

The lease is a local host fact, not an authenticated Context binding. A new
process always receives a new reference and must be bound again by Context's
provider-owned operator before the read-only resolver can be enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

SCHEMA = "cerebro-bk07-local-runtime-session/v1"
PROFILE_SCHEMA = "cerebro-bk07-normal-use-profile/v1"


class LocalRuntimeSessionError(RuntimeError):
    pass


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
        self._receipt = {"schema": SCHEMA, "session_ref": "local:" + uuid.uuid4().hex,
                         "pid": os.getpid(), "started_at_epoch": now,
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
