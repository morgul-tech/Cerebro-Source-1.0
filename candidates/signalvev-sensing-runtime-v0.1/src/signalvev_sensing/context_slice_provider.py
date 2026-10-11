"""CAP26 read-provider protocol + the one executable implementation: LocalPinnedSnapshotProvider.

Protocol (frozen in CONTEXT_SLICE.md):
  describe_authorized(subject_ref, content_ref, owner_ref, *, deadline) -> ReadDescriptor   (metadata only, fresh)
  open_verified_snapshot(descriptor, *, max_bytes, chunk_bytes, deadline) -> SnapshotHandle (context manager)
      handle.chunks()   -> bounded byte chunks of ONE stable open file handle
      handle.confirm()  -> True only if the handle/path identity still equals the descriptor's snapshot identity

The provider never receives a path or URL from a caller: a content_ref is looked up in an explicit registry that maps
known refs to canonical ROOT-RELATIVE files. Authorization (owner + subject allowlist) is decided BEFORE any
filesystem access; path confinement (no absolute/drive/UNC/traversal/encoded segments, no symlink/reparse component)
is decided before the content is opened. The registry is a SYNTHETIC_OWNER_ASSERTION: it is not authenticated real
source evidence and never establishes semantic currentness. Errors carry reason CODES only (no paths, no content).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol

from .model import HEX64, is_id

PROVIDER_LOCAL = "LOCAL_PINNED_SNAPSHOT"
SYNTHETIC_OWNER_ASSERTION = "SYNTHETIC_OWNER_ASSERTION"
SAME, SUPERSEDED, UNKNOWN = "SAME", "SUPERSEDED", "UNKNOWN"
HEAD_RELATIONS = frozenset({SAME, SUPERSEDED, UNKNOWN})
MAX_REGISTRY_BYTES = 65536
MAX_REGISTRY_BINDINGS = 256
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ProviderError(Exception):
    """Typed provider failure. ``code`` is a machine reason; the message never contains paths or content."""
    kind = "UNREADABLE"

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProviderDenied(ProviderError):
    kind = "DENIED"


class ProviderUnavailable(ProviderError):
    kind = "UNREADABLE"


class ProviderDeadline(ProviderError):
    kind = "DEADLINE"


@dataclass(frozen=True)
class ReadDescriptor:
    canonical_ref: str
    owner_ref: str
    revision: str                       # opaque; the immutable revision this provider can serve
    size: int                           # total raw bytes (metadata; the reader re-checks while reading)
    sha256: str                         # full raw-content SHA256 as asserted by the owner/registry
    way_home: str
    head_relation: str                  # owner's relation of ``revision`` to its current head: SAME|SUPERSEDED|UNKNOWN
    superseded_revisions: tuple[str, ...]   # revisions the owner says are superseded (never ordered)
    owner_seq: int | None               # only when the provider attests it
    semantic_currentness: str           # UNKNOWN unless a real owner attests it
    authorization_basis: str
    assertion: str                      # SYNTHETIC_OWNER_ASSERTION for the local provider
    provider: str
    snapshot_identity: str              # opaque digest of the file identity (dev/ino/size/mtime_ns)

    def provenance(self) -> dict[str, Any]:
        return {"provider": self.provider, "assertion": self.assertion,
                "authorization_basis": self.authorization_basis, "snapshot_identity": self.snapshot_identity}


class SnapshotHandle(Protocol):
    def __enter__(self) -> "SnapshotHandle": ...
    def __exit__(self, *exc: Any) -> None: ...
    def chunks(self) -> Iterator[bytes]: ...
    def confirm(self) -> bool: ...


class ContentSliceProvider(Protocol):
    def describe_authorized(self, subject_ref: str, content_ref: str, owner_ref: str, *,
                            deadline: float) -> ReadDescriptor: ...

    def open_verified_snapshot(self, descriptor: ReadDescriptor, *, max_bytes: int, chunk_bytes: int,
                               deadline: float) -> SnapshotHandle: ...


@dataclass
class ProviderStats:
    """Actual provider-side operations (not the resolver's view)."""
    describes: int = 0
    registry_reads: int = 0
    content_opens: int = 0
    read_calls: int = 0
    bytes_read: int = 0
    stat_calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class _Binding:
    content_ref: str
    path: str
    owner_ref: str
    revision: str
    sha256: str
    authorized_subjects: tuple[str, ...]
    head_relation: str
    semantic_currentness: str
    owner_seq: int | None
    way_home: str
    superseded_revisions: tuple[str, ...] = ()
    size: int | None = None


_REQUIRED = {"content_ref", "path", "owner_ref", "revision", "sha256", "authorized_subjects", "head_relation",
             "semantic_currentness", "owner_seq", "way_home"}
_OPTIONAL = {"superseded_revisions", "size"}


def _strict_json(data: bytes) -> Any:
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError("DUPLICATE_KEY")
            out[k] = v
        return out

    def no_const(_):
        raise ValueError("NON_FINITE")
    return json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=no_const)


def parse_registry(data: bytes) -> dict[str, _Binding]:
    """Strict registry parse. Any defect makes the whole registry unusable (fail closed)."""
    if len(data) > MAX_REGISTRY_BYTES:
        raise ProviderUnavailable("REGISTRY_OVER_BOUND")
    try:
        doc = _strict_json(data)
    except (ValueError, UnicodeDecodeError):
        raise ProviderUnavailable("REGISTRY_MALFORMED") from None
    if type(doc) is not dict or set(doc) - {"note", "bindings"} or type(doc.get("bindings")) is not list:
        raise ProviderUnavailable("REGISTRY_SHAPE")
    if len(doc["bindings"]) > MAX_REGISTRY_BINDINGS:
        raise ProviderUnavailable("REGISTRY_OVER_BOUND")
    out: dict[str, _Binding] = {}
    for b in doc["bindings"]:
        if type(b) is not dict or not _REQUIRED <= set(b) or set(b) - _REQUIRED - _OPTIONAL:
            raise ProviderUnavailable("REGISTRY_BINDING_SHAPE")
        ok = (all(is_id(b[k]) for k in ("content_ref", "owner_ref", "revision", "way_home"))
              and type(b["path"]) is str and type(b["sha256"]) is str and HEX64.match(b["sha256"])
              and type(b["authorized_subjects"]) is list and all(is_id(s) for s in b["authorized_subjects"])
              and b["head_relation"] in HEAD_RELATIONS and b["semantic_currentness"] == UNKNOWN
              and (b["owner_seq"] is None or (type(b["owner_seq"]) is int and b["owner_seq"] >= 1))
              and (type(b.get("superseded_revisions", [])) is list
                   and all(is_id(r) for r in b.get("superseded_revisions", [])))
              and (b.get("size") is None or (type(b.get("size")) is int and b["size"] >= 0)))
        if not ok or b["revision"] in b.get("superseded_revisions", []):
            raise ProviderUnavailable("REGISTRY_BINDING_VALUE")
        if b["content_ref"] in out:
            raise ProviderUnavailable("REGISTRY_DUPLICATE_REF")
        out[b["content_ref"]] = _Binding(
            content_ref=b["content_ref"], path=b["path"], owner_ref=b["owner_ref"], revision=b["revision"],
            sha256=b["sha256"], authorized_subjects=tuple(b["authorized_subjects"]), head_relation=b["head_relation"],
            semantic_currentness=b["semantic_currentness"], owner_seq=b["owner_seq"], way_home=b["way_home"],
            superseded_revisions=tuple(b.get("superseded_revisions", [])), size=b.get("size"))
    return out


def validate_relative_path(path: str) -> tuple[str, ...]:
    """Root-relative POSIX path of plain segments only: no absolute, drive, UNC, backslash, traversal, '.',
    percent/encoded or control characters. Returns the segments."""
    if type(path) is not str or not path or len(path) > 512:
        raise ProviderDenied("PATH_INVALID")
    segments = tuple(path.split("/"))
    if any(not _SEGMENT.match(s) for s in segments):
        raise ProviderDenied("PATH_NOT_CONFINED")
    return segments


def _identity(st: os.stat_result) -> str:
    raw = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}".encode()
    return hashlib.sha256(raw).hexdigest()


def _is_link_or_reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


class _LocalHandle:
    def __init__(self, provider: "LocalPinnedSnapshotProvider", path: Path, descriptor: ReadDescriptor,
                 max_bytes: int, chunk_bytes: int, deadline: float) -> None:
        self._p, self._path, self._d = provider, path, descriptor
        self._max, self._chunk, self._deadline = max_bytes, chunk_bytes, deadline
        self._fd: int | None = None

    def __enter__(self) -> "_LocalHandle":
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if self._p.clock() >= self._deadline:
            raise ProviderDeadline("DEADLINE_BEFORE_OPEN")
        self._p.stats.content_opens += 1
        try:
            self._fd = os.open(self._path, flags)
        except OSError:
            raise ProviderUnavailable("CONTENT_OPEN_FAILED") from None
        self._p.stats.stat_calls += 1
        st = os.fstat(self._fd)
        if not stat.S_ISREG(st.st_mode) or _identity(st) != self._d.snapshot_identity:
            self.__exit__(None, None, None)
            raise ProviderUnavailable("SNAPSHOT_IDENTITY_CHANGED_BEFORE_READ")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def chunks(self) -> Iterator[bytes]:
        total = 0
        while True:
            if self._p.clock() >= self._deadline:
                raise ProviderDeadline("DEADLINE_DURING_READ")
            self._p.stats.read_calls += 1
            data = os.read(self._fd, self._chunk)
            self._p.stats.bytes_read += len(data)
            self._p.hook("after_read", len(data))
            if not data:
                return
            total += len(data)
            yield data
            if total > self._max:            # cap while reading even if metadata lies; the reader sees the excess
                return

    def confirm(self) -> bool:
        if self._fd is None:
            return False
        self._p.stats.stat_calls += 2
        try:
            now_fd, now_path = os.fstat(self._fd), os.lstat(self._path)
        except OSError:
            return False
        return (_identity(now_fd) == self._d.snapshot_identity == _identity(now_path)
                and not _is_link_or_reparse(now_path))


class LocalPinnedSnapshotProvider:
    """Executable provider for this candidate: configured root + explicit content_ref registry file.

    The registry file is re-read on EVERY describe (fresh: a registry change between the two describes is seen)."""

    def __init__(self, root: str | os.PathLike[str], registry_path: str | os.PathLike[str], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.root = Path(root)
        self.registry_path = Path(registry_path)
        self.clock = clock
        self.stats = ProviderStats()
        self._hooks: dict[str, Callable[..., None]] = {}

    def on(self, name: str, fn: Callable[..., None]) -> None:
        """Test seam (synthetic): called at named points; never used by production code paths."""
        self._hooks[name] = fn

    def hook(self, name: str, *args: Any) -> None:
        fn = self._hooks.get(name)
        if fn is not None:
            fn(*args)

    def _registry(self) -> dict[str, _Binding]:
        self.stats.registry_reads += 1
        try:
            with open(self.registry_path, "rb") as fh:
                data = fh.read(MAX_REGISTRY_BYTES + 1)
        except OSError:
            raise ProviderUnavailable("REGISTRY_UNREADABLE") from None
        return parse_registry(data)

    def _resolve_confined(self, segments: tuple[str, ...]) -> tuple[Path, os.stat_result]:
        try:
            root = self.root.resolve(strict=True)
        except OSError:
            raise ProviderUnavailable("ROOT_UNAVAILABLE") from None
        cur = root
        st = None
        for seg in segments:
            cur = cur / seg
            self.stats.stat_calls += 1
            try:
                st = os.lstat(cur)
            except FileNotFoundError:
                raise ProviderUnavailable("CONTENT_MISSING") from None
            except OSError:
                raise ProviderUnavailable("CONTENT_UNREADABLE") from None
            if _is_link_or_reparse(st):
                raise ProviderDenied("PATH_LINK_OR_REPARSE")
        if st is None or not stat.S_ISREG(st.st_mode):
            raise ProviderDenied("PATH_NOT_REGULAR_FILE")
        if not cur.resolve(strict=True).is_relative_to(root):
            raise ProviderDenied("PATH_ESCAPES_ROOT")
        return cur, st

    def describe_authorized(self, subject_ref: str, content_ref: str, owner_ref: str, *,
                            deadline: float) -> ReadDescriptor:
        self.stats.describes += 1
        if self.clock() >= deadline:
            raise ProviderDeadline("DEADLINE_BEFORE_DESCRIBE")
        binding = self._registry().get(content_ref)
        if binding is None:
            raise ProviderDenied("UNKNOWN_CONTENT_REF")            # unknown == denied: no existence oracle
        if binding.owner_ref != owner_ref:
            raise ProviderDenied("FOREIGN_OWNER")
        if subject_ref not in binding.authorized_subjects:
            raise ProviderDenied("SUBJECT_NOT_AUTHORIZED")
        _path, st = self._resolve_confined(validate_relative_path(binding.path))   # metadata only, no content open
        if binding.size is not None and binding.size != st.st_size:
            raise ProviderUnavailable("REGISTRY_SIZE_DISAGREES_WITH_FILE")
        return ReadDescriptor(
            canonical_ref=binding.content_ref, owner_ref=binding.owner_ref, revision=binding.revision,
            size=st.st_size, sha256=binding.sha256, way_home=binding.way_home, head_relation=binding.head_relation,
            superseded_revisions=binding.superseded_revisions, owner_seq=binding.owner_seq,
            semantic_currentness=binding.semantic_currentness,
            authorization_basis="REGISTRY_OWNER_AND_SUBJECT_ALLOWLIST", assertion=SYNTHETIC_OWNER_ASSERTION,
            provider=PROVIDER_LOCAL, snapshot_identity=_identity(st))

    def open_verified_snapshot(self, descriptor: ReadDescriptor, *, max_bytes: int, chunk_bytes: int,
                               deadline: float) -> _LocalHandle:
        if descriptor.provider != PROVIDER_LOCAL:
            raise ProviderUnavailable("DESCRIPTOR_FOREIGN_PROVIDER")
        binding = self._registry_binding_for(descriptor)
        path, st = self._resolve_confined(validate_relative_path(binding.path))
        if _identity(st) != descriptor.snapshot_identity:
            raise ProviderUnavailable("SNAPSHOT_IDENTITY_CHANGED_BEFORE_OPEN")
        self.hook("before_open", path)
        return _LocalHandle(self, path, descriptor, max_bytes, chunk_bytes, deadline)

    def _registry_binding_for(self, descriptor: ReadDescriptor) -> _Binding:
        binding = self._registry().get(descriptor.canonical_ref)
        if binding is None or (binding.owner_ref, binding.revision, binding.sha256) != (
                descriptor.owner_ref, descriptor.revision, descriptor.sha256):
            raise ProviderUnavailable("REGISTRY_CHANGED_BEFORE_OPEN")
        return binding
