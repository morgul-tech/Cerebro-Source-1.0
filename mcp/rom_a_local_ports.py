"""Operator-bound, single-episode local ROM-A read and Codex queue ports.

These ports are inert at construction.  A trusted host must pin every path,
digest, principal and recipient before exposing the normal host operation.
They neither grant Context authority nor prove recipient use.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import uuid


MAX_BYTES = 16_384
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")
_REPARSE = 0x400


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def _token_sid() -> str:
    """Read the process token, never an environment/user-name assertion."""
    _require(os.name == "nt", "ROMA_WINDOWS_TOKEN_REQUIRED")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                           ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    _require(bool(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))),
             "ROMA_TOKEN_UNAVAILABLE")
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        _require(needed.value > 0, "ROMA_TOKEN_USER_UNAVAILABLE")
        data = ctypes.create_string_buffer(needed.value)
        _require(bool(advapi.GetTokenInformation(token, 1, data, needed, ctypes.byref(needed))),
                 "ROMA_TOKEN_USER_UNAVAILABLE")
        sid_ptr = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        rendered = wintypes.LPWSTR()
        _require(bool(advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(rendered))),
                 "ROMA_TOKEN_SID_UNAVAILABLE")
        try:
            return rendered.value
        finally:
            kernel.LocalFree(rendered)
    finally:
        kernel.CloseHandle(token)


def _owner_sid(path: Path) -> str:
    """Read Windows file owner from the security descriptor, not file metadata text."""
    _require(os.name == "nt", "ROMA_WINDOWS_CUSTODY_REQUIRED")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int,
                                             wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = advapi.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(owner),
                                         None, None, None, ctypes.byref(descriptor))
    _require(code == 0 and bool(owner.value), "ROMA_CUSTODY_OWNER_UNAVAILABLE")
    try:
        rendered = wintypes.LPWSTR()
        _require(bool(advapi.ConvertSidToStringSidW(owner, ctypes.byref(rendered))),
                 "ROMA_CUSTODY_SID_UNAVAILABLE")
        try:
            return rendered.value
        finally:
            kernel.LocalFree(rendered)
    finally:
        kernel.LocalFree(descriptor)


def _no_reparse(path: Path) -> None:
    stat = path.lstat()
    _require(not path.is_symlink() and not (getattr(stat, "st_file_attributes", 0) & _REPARSE),
             "ROMA_PATH_REPARSE_FORBIDDEN")


@dataclass(frozen=True)
class LocalContentBinding:
    content_ref: str
    relative_path: str
    sha256: str
    selector_kind: str = "SECTION"
    selector: str = "selected"


class FixedLocalEpisodeReader:
    def __init__(self, *, root: Path, expected_process_sid: str,
                 bindings: tuple[LocalContentBinding, ...], sid_reader=_token_sid,
                 owner_reader=_owner_sid):
        _require(root.is_absolute() and root.is_dir(), "ROMA_FIXED_ROOT_REQUIRED")
        _require(bool(re.fullmatch(r"S-1-[0-9-]+", expected_process_sid)), "ROMA_PRINCIPAL_SID_REQUIRED")
        _require(sid_reader() == expected_process_sid, "ROMA_WRONG_PRINCIPAL")
        _require(owner_reader(root) == expected_process_sid, "ROMA_ROOT_CUSTODY_MISMATCH")
        _require(len(bindings) == 1, "ROMA_SINGLE_EPISODE_BINDING_REQUIRED")
        for parent in (root, *root.parents):
            if parent.exists():
                _no_reparse(parent)
        binding = bindings[0]
        _require(bool(_REF.fullmatch(binding.content_ref)), "ROMA_CONTENT_REF_INVALID")
        _require(binding.selector_kind == "SECTION" and binding.selector == "selected",
                 "ROMA_FIXED_SELECTOR_REQUIRED")
        _require(bool(_SHA.fullmatch(binding.sha256)), "ROMA_CONTENT_HASH_REQUIRED")
        relative = Path(binding.relative_path)
        _require(not relative.is_absolute() and relative.parts and
                 all(part not in (".", "..") for part in relative.parts) and
                 ":" not in binding.relative_path, "ROMA_RELATIVE_PATH_INVALID")
        self._root = root.resolve(strict=True)
        self._binding = binding
        self._sid = expected_process_sid
        self._sid_reader = sid_reader
        self._owner_reader = owner_reader

    def read_current(self, content_ref: str, kind: str, selector: str) -> dict:
        binding = self._binding
        _require((content_ref, kind, selector) ==
                 (binding.content_ref, binding.selector_kind, binding.selector),
                 "ROMA_UNREGISTERED_CONTENT_OR_SELECTOR")
        _require(self._sid_reader() == self._sid, "ROMA_WRONG_PRINCIPAL")
        path = self._root / binding.relative_path
        parent = path.parent
        while parent != self._root:
            _require(parent.is_relative_to(self._root), "ROMA_PATH_ESCAPE")
            _no_reparse(parent)
            parent = parent.parent
        _no_reparse(self._root)
        _no_reparse(path)
        _require(self._owner_reader(self._root) == self._sid and
                 self._owner_reader(path) == self._sid,
                 "ROMA_CONTENT_CUSTODY_MISMATCH")
        _require(path.resolve(strict=True).is_relative_to(self._root), "ROMA_PATH_ESCAPE")
        before = path.stat()
        _require(before.st_size <= MAX_BYTES and path.is_file(), "ROMA_CONTENT_TOO_LARGE_OR_NOT_FILE")
        with path.open("rb") as stream:
            payload = stream.read(MAX_BYTES + 1)
            opened = os.fstat(stream.fileno())
        after = path.stat()
        _require(len(payload) <= MAX_BYTES and (before.st_dev, before.st_ino, before.st_size,
                 before.st_mtime_ns) == (opened.st_dev, opened.st_ino, opened.st_size,
                 opened.st_mtime_ns) == (after.st_dev, after.st_ino, after.st_size,
                 after.st_mtime_ns), "ROMA_CONCURRENT_CONTENT_CHANGE")
        digest = hashlib.sha256(payload).hexdigest()
        _require(digest == binding.sha256, "ROMA_STALE_REVISION")
        try:
            selected = payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("ROMA_CONTENT_NOT_UTF8") from exc
        _require(bool(selected.strip()), "ROMA_EMPTY_CONTENT")
        return {"content_ref": content_ref, "selector_kind": kind, "selector": selector,
                "revision": "sha256:" + digest, "authority_ref": "local-custody:" + self._sid,
                "currentness": "CURRENT", "provider_readback_verified": True,
                "access_verified": True, "access_scope_ref": "os-token:" + self._sid,
                "provenance_refs": ["local-episode:" + binding.content_ref,
                                    "sha256:" + digest],
                "parts": {selector: selected}}


@dataclass(frozen=True)
class QueueBinding:
    recipient_ref: str
    recipient_thread_id: str
    task_ref: str
    task_revision: str
    selected_sha256: str
    selected_bytes: bytes


class FixedCodexQueueSender:
    def __init__(self, *, codex_exe: Path, expected_process_sid: str,
                 binding: QueueBinding, sid_reader=_token_sid,
                 runner=subprocess.run):
        _require(sid_reader() == expected_process_sid, "ROMA_WRONG_PRINCIPAL")
        _require(codex_exe.is_absolute() and codex_exe.is_file(), "ROMA_CODEX_EXE_REQUIRED")
        _no_reparse(codex_exe)
        _require(bool(_REF.fullmatch(binding.recipient_ref)) and
                 bool(_REF.fullmatch(binding.task_ref)) and
                 bool(_REF.fullmatch(binding.task_revision)), "ROMA_QUEUE_BINDING_INVALID")
        uuid.UUID(binding.recipient_thread_id)
        _require(bool(_SHA.fullmatch(binding.selected_sha256)) and
                 hashlib.sha256(binding.selected_bytes).hexdigest() == binding.selected_sha256 and
                 0 < len(binding.selected_bytes) <= MAX_BYTES, "ROMA_SELECTED_HASH_INVALID")
        binding.selected_bytes.decode("utf-8", errors="strict")
        self._exe = codex_exe.resolve(strict=True)
        self._exe_stat = self._exe.stat()
        self._sid = expected_process_sid
        self._sid_reader = sid_reader
        self._binding = binding
        self._runner = runner
        self._attempted = False

    def send_selected(self, *, recipient_ref: str, selected_bytes: bytes,
                      selected_sha256: str, selection: str, task_ref: str,
                      task_revision: str) -> dict:
        bound = self._binding
        _require(self._sid_reader() == self._sid, "ROMA_WRONG_PRINCIPAL")
        _require((recipient_ref, selected_bytes, selected_sha256, task_ref, task_revision) ==
                 (bound.recipient_ref, bound.selected_bytes, bound.selected_sha256,
                  bound.task_ref, bound.task_revision), "ROMA_QUEUE_BINDING_MISMATCH")
        _require(selection in {"FULL_ORIGINAL_TASK", "BK05_STRUCTURAL_CAPSULE_CANDIDATE"},
                 "ROMA_SELECTION_NOT_ALLOWED")
        _require(not self._attempted, "ROMA_QUEUE_OUTCOME_UNKNOWN_NO_RETRY")
        _no_reparse(self._exe)
        now = self._exe.stat()
        _require((now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) ==
                 (self._exe_stat.st_dev, self._exe_stat.st_ino, self._exe_stat.st_size,
                  self._exe_stat.st_mtime_ns), "ROMA_CODEX_EXE_CHANGED")
        message = ("ROM_A_NORMAL_RETURN\n"
                   f"TASK={task_ref}\nREVISION={task_revision}\n"
                   f"SELECTION={selection}\nSHA256={selected_sha256}\n"
                   "QUEUED_ONLY_NOT_START_OR_USE\n---SELECTED_BYTES_UTF8---\n"
                   + selected_bytes.decode("utf-8"))
        self._attempted = True
        result = self._runner([str(self._exe), "queue", "--thread", bound.recipient_thread_id,
                               "--message", message], shell=False, capture_output=True,
                              text=True, timeout=30, check=False)
        # A successful exit alone cannot establish an identifiable accepted send.
        if result.returncode != 0:
            raise ValueError("ROMA_QUEUE_OUTCOME_UNKNOWN_NO_RETRY")
        match = re.fullmatch(
            r"Queued message ([0-9a-fA-F-]{36}) for thread ([0-9a-fA-F-]{36})\s*",
            result.stdout)
        if match is None:
            raise ValueError("ROMA_QUEUE_RECEIPT_UNRECOGNIZED_NO_RETRY")
        if str(uuid.UUID(match.group(2))) != bound.recipient_thread_id:
            raise ValueError("ROMA_QUEUE_RECIPIENT_UNRECOGNIZED_NO_RETRY")
        delivery_ref = str(uuid.UUID(match.group(1)))
        return {"state": "ACCEPTED", "recipient_ref": recipient_ref,
                "selected_sha256": selected_sha256, "delivery_ref": delivery_ref,
                "recipient_use": "NOT_OBSERVED"}


def assemble_fixed_local_episode_ports(*, operator_root: Path,
                                       expected_process_sid: str,
                                       content: LocalContentBinding,
                                       delivery: QueueBinding,
                                       codex_exe: Path) -> tuple[FixedLocalEpisodeReader,
                                                                 FixedCodexQueueSender]:
    """Protected composition-root call; never expose these arguments as MCP input.

    One explicit local artifact and one exact recipient/task/payload bind.  No
    default root, identity, content reference, recipient or executable exists.
    The caller must provide verified operator custody and an authentic episode.
    """
    reader = FixedLocalEpisodeReader(root=operator_root,
                                     expected_process_sid=expected_process_sid,
                                     bindings=(content,))
    sender = FixedCodexQueueSender(codex_exe=codex_exe,
                                   expected_process_sid=expected_process_sid,
                                   binding=delivery)
    return reader, sender
