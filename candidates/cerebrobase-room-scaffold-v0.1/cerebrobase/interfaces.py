"""Frozen CB-P01 interfaces that CB-P02 (private files/quota/recovery) and CB-P03 (Stambok/Dagbok mirrors) consume.

Contracts only. Nothing here stores, uploads, restores or mirrors anything; the HTTP layer answers these routes with
503 CAPABILITY_UNAVAILABLE *after* the membership check, so the shell never suggests a working upload or recovery and
an admin cannot use them to enumerate a pilot's metadata.

Every call takes the server-resolved Principal and a room_id that is only a referent: implementations MUST call
auth.room_for(con, principal, room_id) first and treat None as NOT_FOUND (same answer for foreign and missing rooms).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .auth import Principal

UNAVAILABLE = "CAPABILITY_UNAVAILABLE"


@dataclass(frozen=True)
class FileRef:                      # mirrors table p02_file_metadata_reserved
    room_id: str
    file_id: str
    revision: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class QuotaView:                    # mirrors table p02_quota_reserved
    room_id: str
    limit_bytes: int
    used_bytes: int


@dataclass(frozen=True)
class MirrorRef:                    # mirrors table p03_mirror_metadata_reserved (canonical -> room only)
    room_id: str
    mirror_kind: str                # STAMBOK | DAGBOK
    source_ref: str
    source_revision: str
    source_sha256: str


class RoomFileStore(Protocol):      # CB-P02
    def list(self, principal: Principal, room_id: str) -> list[FileRef]: ...
    def put(self, principal: Principal, room_id: str, name: str, data: bytes) -> FileRef: ...
    def get(self, principal: Principal, room_id: str, file_id: str) -> bytes: ...
    def tombstone(self, principal: Principal, room_id: str, file_id: str) -> FileRef: ...
    def quota(self, principal: Principal, room_id: str) -> QuotaView: ...


class RoomMirror(Protocol):         # CB-P03
    def list(self, principal: Principal, room_id: str) -> list[MirrorRef]: ...
    def read(self, principal: Principal, room_id: str, mirror: MirrorRef) -> bytes: ...


P02_FILES = "P02_PRIVATE_FILES"
P03_MIRRORS = "P03_STAMBOK_DAGBOK_MIRROR"
