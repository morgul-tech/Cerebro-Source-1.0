"""BK07 read-front local DERIVED state: navigation bookmarks + small verified slice cache. Not an owner source.

One JSON file, schema ``bk07.read_front_state/v1``:
  {"schema": ..., "body": {...}, "body_sha256": sha256(canonical(body))}
* corruption detection: wrong schema / malformed JSON / digest mismatch / oversize => state treated as EMPTY
  (safe cold read) and reported CORRUPT_IGNORED; nothing from it is reused.
* exactly one explicit writer: an exclusive lock file (O_CREAT|O_EXCL) next to the state; a busy lock means this call
  does not persist (STATE_BUSY_NOT_PERSISTED) and never breaks someone else's lock.
* atomic commit: write a new temp file in the same directory, flush+fsync, os.replace.
* bounds: <= 64 entries, cached slice <= 1024 bytes each, <= 1 MiB cached slice bytes in total, <= 16 row scopes of
  <= 128 rows. No eval/pickle, no secrets (principal/scope are stored only as attested ids/fingerprints).
The state is disposable: deleting it only costs one cold bounded read.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

SCHEMA = "bk07.read_front_state/v1"
MAX_STATE_FILE_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 64
MAX_SLICE_BYTES = 1024
MAX_CACHE_BYTES = 1024 * 1024
MAX_SCOPES = 16
MAX_ROWS = 128

MISSING, LOADED, CORRUPT = "MISSING_COLD", "LOADED", "CORRUPT_IGNORED"


class StateBoundExceeded(ValueError):
    pass


class StateBusy(RuntimeError):
    pass


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _empty() -> dict[str, Any]:
    return {"entries": {}, "rows": {}, "seq": 0}


class ReadFrontState:
    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path) if path is not None else None
        self.body: dict[str, Any] = _empty()
        self.status = MISSING
        self.read_bytes = 0
        self.write_bytes = 0
        self.ms = 0.0

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | os.PathLike[str] | None) -> "ReadFrontState":
        st = cls(path)
        t0 = time.perf_counter()
        try:
            st._load()
        finally:
            st.ms += (time.perf_counter() - t0) * 1000.0
        return st

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            self.status = MISSING
            return
        try:
            with open(self.path, "rb") as fh:
                data = fh.read(MAX_STATE_FILE_BYTES + 1)
        except OSError:
            self.status = CORRUPT
            return
        self.read_bytes = len(data)
        try:
            if len(data) > MAX_STATE_FILE_BYTES:
                raise ValueError("oversize")
            doc = json.loads(data.decode("utf-8"), parse_constant=_reject)
            if type(doc) is not dict or set(doc) != {"schema", "body", "body_sha256"} or doc["schema"] != SCHEMA:
                raise ValueError("schema")
            body = doc["body"]
            if hashlib.sha256(canonical(body)).hexdigest() != doc["body_sha256"]:
                raise ValueError("digest")
            _validate_body(body)
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            self.body, self.status = _empty(), CORRUPT
            return
        self.body, self.status = body, LOADED

    # ------------------------------------------------------------------ access
    def entry(self, knagg: str) -> dict[str, Any] | None:
        e = self.body["entries"].get(knagg)
        return copy.deepcopy(e) if e is not None else None

    def put_entry(self, knagg: str, entry: dict[str, Any]) -> None:
        self.body["entries"][knagg] = copy.deepcopy(entry)

    def invalidate_entry(self, knagg: str, reason: str) -> bool:
        e = self.body["entries"].get(knagg)
        if e is None:
            return False
        for k in ("slice_text", "slice_sha256", "descriptor"):
            e.pop(k, None)                          # never keep a payload of an invalidated binding
        e["valid"] = False
        e["invalidated_reason"] = reason
        return True

    def rows(self, scope: str) -> dict[str, Any] | None:
        r = self.body["rows"].get(scope)
        return copy.deepcopy(r) if r is not None else None

    def put_rows(self, scope: str, rows: dict[str, Any]) -> None:
        self.body["rows"][scope] = copy.deepcopy(rows)

    # ------------------------------------------------------------------ commit (one explicit writer, atomic)
    def commit(self, *, check=None) -> str:
        if self.path is None:
            return "NO_STATE_PATH_NOT_PERSISTED"
        if check is not None:check()
        t0 = time.perf_counter()
        self.body["seq"] = int(self.body.get("seq", 0)) + 1
        _validate_body(self.body)
        data = canonical({"schema": SCHEMA, "body": self.body,
                          "body_sha256": hashlib.sha256(canonical(self.body)).hexdigest()})
        if len(data) > MAX_STATE_FILE_BYTES:
            raise StateBoundExceeded("STATE_FILE_OVER_BOUND")
        lock = self.path.with_name(self.path.name + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise StateBusy("STATE_BUSY_NOT_PERSISTED") from None
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            os.close(fd)
            previous = None
            if check is not None and self.path.exists():
                with open(self.path,'rb') as old:previous=old.read(MAX_STATE_FILE_BYTES+1)
                if len(previous)>MAX_STATE_FILE_BYTES:raise StateBoundExceeded("ROLLBACK_SNAPSHOT_OVER_BOUND")
            if check is not None:check()
            with open(tmp, "xb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            self.write_bytes = len(data)
            if check is not None:check()
            os.replace(tmp, self.path)
            try:
                if check is not None:check()
            except Exception:
                # Cleanup rollback of this guarded commit, while OUR exclusive lock
                # remains held. No deadline-expired delivery marker is retained.
                if previous is None:self.path.unlink()
                else:
                    with open(tmp,"xb") as fh:fh.write(previous);fh.flush();os.fsync(fh.fileno())
                    os.replace(tmp,self.path)
                raise
        finally:
            if tmp.exists():
                tmp.unlink()
            os.unlink(lock)
            self.ms += (time.perf_counter() - t0) * 1000.0
        return "COMMITTED"


def _reject(_):
    raise ValueError("non-finite")


def _validate_body(body: Any) -> None:
    if type(body) is not dict or set(body) != {"entries", "rows", "seq"}:
        raise ValueError("body shape")
    if type(body["entries"]) is not dict or type(body["rows"]) is not dict or type(body["seq"]) is not int:
        raise ValueError("body types")
    if len(body["entries"]) > MAX_ENTRIES:
        raise StateBoundExceeded("STATE_ENTRIES_OVER_BOUND")
    total = 0
    for e in body["entries"].values():
        if type(e) is not dict:
            raise ValueError("entry")
        text = e.get("slice_text")
        if text is not None:
            n = len(text.encode("utf-8"))
            if n > MAX_SLICE_BYTES:
                raise StateBoundExceeded("STATE_SLICE_OVER_BOUND")
            total += n
    if total > MAX_CACHE_BYTES:
        raise StateBoundExceeded("STATE_CACHE_OVER_BOUND")
    if len(body["rows"]) > MAX_SCOPES:
        raise StateBoundExceeded("STATE_SCOPES_OVER_BOUND")
    for r in body["rows"].values():
        if type(r) is not dict or type(r.get("messages")) is not dict or len(r["messages"]) > MAX_ROWS:
            raise StateBoundExceeded("STATE_ROWS_OVER_BOUND")
