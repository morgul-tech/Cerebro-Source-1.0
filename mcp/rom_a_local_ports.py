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
import importlib.util
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import uuid

_CUSTODY_SPEC = importlib.util.spec_from_file_location(
    "rom_a_windows_custody", Path(__file__).with_name("rom_a_windows_custody.py"))
if _CUSTODY_SPEC is None or _CUSTODY_SPEC.loader is None:
    raise RuntimeError("ROMA_WINDOWS_CUSTODY_MODULE_UNAVAILABLE")
_custody = importlib.util.module_from_spec(_CUSTODY_SPEC)
_CUSTODY_SPEC.loader.exec_module(_custody)
open_protected = _custody.open_protected
sha256_stream = _custody.sha256_stream


MAX_BYTES = 16_384
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")


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


@dataclass(frozen=True)
class LocalContentBinding:
    content_ref: str
    relative_path: str
    sha256: str
    selector_kind: str = "SECTION"
    selector: str = "selected"


class FixedLocalEpisodeReader:
    def __init__(self, *, root: Path, expected_process_sid: str,
                 expected_custody_owner_sid: str,
                 bindings: tuple[LocalContentBinding, ...], sid_reader=_token_sid):
        _require(root.is_absolute() and root.is_dir(), "ROMA_FIXED_ROOT_REQUIRED")
        _require(bool(re.fullmatch(r"S-1-[0-9-]+", expected_process_sid)), "ROMA_PRINCIPAL_SID_REQUIRED")
        _require(sid_reader() == expected_process_sid, "ROMA_WRONG_PRINCIPAL")
        _require(expected_custody_owner_sid in {"S-1-5-18", "S-1-5-32-544"},
                 "ROMA_PROTECTED_CUSTODY_OWNER_REQUIRED")
        _require(len(bindings) == 1, "ROMA_SINGLE_EPISODE_BINDING_REQUIRED")
        binding = bindings[0]
        _require(bool(_REF.fullmatch(binding.content_ref)), "ROMA_CONTENT_REF_INVALID")
        _require(binding.selector_kind == "SECTION" and binding.selector == "selected",
                 "ROMA_FIXED_SELECTOR_REQUIRED")
        _require(bool(_SHA.fullmatch(binding.sha256)), "ROMA_CONTENT_HASH_REQUIRED")
        relative = Path(binding.relative_path)
        _require(not relative.is_absolute() and relative.parts and
                 all(part not in (".", "..") for part in relative.parts) and
                 ":" not in binding.relative_path, "ROMA_RELATIVE_PATH_INVALID")
        self._root = root
        self._binding = binding
        self._sid = expected_process_sid
        self._owner_sid = expected_custody_owner_sid
        self._sid_reader = sid_reader
        # Construction itself must not advertise a bound port from stale or
        # unprotected bytes; every later read repeats the handle proof.
        self.read_current(binding.content_ref, binding.selector_kind, binding.selector)

    def read_current(self, content_ref: str, kind: str, selector: str) -> dict:
        binding = self._binding
        _require((content_ref, kind, selector) ==
                 (binding.content_ref, binding.selector_kind, binding.selector),
                 "ROMA_UNREGISTERED_CONTENT_OR_SELECTOR")
        _require(self._sid_reader() == self._sid, "ROMA_WRONG_PRINCIPAL")
        path = self._root / binding.relative_path
        with open_protected(path, root=self._root, owner_sid=self._owner_sid) as (stream, final):
            _require(final == ntpath.normcase(ntpath.normpath(str(path))),
                     "ROMA_FINAL_PATH_MISMATCH")
            payload = stream.read(MAX_BYTES + 1)
        _require(len(payload) <= MAX_BYTES, "ROMA_CONTENT_TOO_LARGE")
        digest = hashlib.sha256(payload).hexdigest()
        _require(digest == binding.sha256, "ROMA_STALE_REVISION")
        try:
            selected = payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValueError("ROMA_CONTENT_NOT_UTF8") from exc
        _require(bool(selected.strip()), "ROMA_EMPTY_CONTENT")
        return {"content_ref": content_ref, "selector_kind": kind, "selector": selector,
                "revision": "sha256:" + digest, "authority_ref": "local-custody:" + self._owner_sid,
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
    selection: str
    selected_sha256: str
    selected_bytes: bytes


class FixedCodexQueueSender:
    def __init__(self, *, codex_exe: Path, executable_root: Path,
                 expected_exe_sha256: str, registry_path: Path,
                 registry_root: Path, expected_custody_owner_sid: str,
                 expected_process_sid: str,
                 binding: QueueBinding, sid_reader=_token_sid,
                 runner=subprocess.run):
        _require(sid_reader() == expected_process_sid, "ROMA_WRONG_PRINCIPAL")
        _require(codex_exe.is_absolute() and codex_exe.is_file(), "ROMA_CODEX_EXE_REQUIRED")
        _require(bool(_SHA.fullmatch(expected_exe_sha256)), "ROMA_CODEX_HASH_REQUIRED")
        _require(expected_custody_owner_sid in {"S-1-5-18", "S-1-5-32-544"},
                 "ROMA_PROTECTED_CUSTODY_OWNER_REQUIRED")
        _require(bool(_REF.fullmatch(binding.recipient_ref)) and
                 bool(_REF.fullmatch(binding.task_ref)) and
                 bool(_REF.fullmatch(binding.task_revision)), "ROMA_QUEUE_BINDING_INVALID")
        uuid.UUID(binding.recipient_thread_id)
        _require(binding.selection in {"FULL_ORIGINAL_TASK", "BK05_STRUCTURAL_CAPSULE_CANDIDATE"},
                 "ROMA_SELECTION_NOT_ALLOWED")
        _require(bool(_SHA.fullmatch(binding.selected_sha256)) and
                 hashlib.sha256(binding.selected_bytes).hexdigest() == binding.selected_sha256 and
                 0 < len(binding.selected_bytes) <= MAX_BYTES, "ROMA_SELECTED_HASH_INVALID")
        binding.selected_bytes.decode("utf-8", errors="strict")
        self._exe = codex_exe
        self._exe_root = executable_root
        self._exe_sha256 = expected_exe_sha256
        self._registry = registry_path
        self._registry_root = registry_root
        self._owner_sid = expected_custody_owner_sid
        self._sid = expected_process_sid
        self._sid_reader = sid_reader
        self._binding = binding
        self._runner = runner
        self._attempted = False
        with open_protected(self._exe, root=self._exe_root,
                            owner_sid=self._owner_sid) as (stream, final):
            _require(final == ntpath.normcase(ntpath.normpath(str(self._exe))),
                     "ROMA_CODEX_FINAL_PATH_MISMATCH")
            _require(sha256_stream(stream) == self._exe_sha256,
                     "ROMA_CODEX_EXE_HASH_MISMATCH")
        self._registry_sha256 = self._check_registry()

    def _check_registry(self) -> str:
        with open_protected(self._registry, root=self._registry_root,
                            owner_sid=self._owner_sid) as (stream, final):
            _require(final == ntpath.normcase(ntpath.normpath(str(self._registry))),
                     "ROMA_REGISTRY_FINAL_PATH_MISMATCH")
            raw = stream.read(4097)
        _require(len(raw) <= 4096, "ROMA_REGISTRY_TOO_LARGE")
        try:
            def unique(items):
                keys = [key for key, _ in items]
                _require(len(keys) == len(set(keys)), "ROMA_REGISTRY_DUPLICATE_KEY")
                return dict(items)
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("ROMA_REGISTRY_INVALID") from exc
        bound = self._binding
        exact = {"schema": "cerebro-rom-a-selected-queue-registry/v1",
                 "recipient_ref": bound.recipient_ref,
                 "recipient_thread_id": bound.recipient_thread_id,
                 "task_ref": bound.task_ref, "task_revision": bound.task_revision,
                 "selection": bound.selection,
                 "selected_sha256": bound.selected_sha256,
                 "selected_bytes": len(bound.selected_bytes),
                 "codex_exe_sha256": self._exe_sha256}
        _require(record == exact, "ROMA_PROTECTED_REGISTRY_BINDING_MISMATCH")
        return hashlib.sha256(raw).hexdigest()

    def send_selected(self, *, recipient_ref: str, selected_bytes: bytes,
                      selected_sha256: str, selection: str, task_ref: str,
                      task_revision: str) -> dict:
        bound = self._binding
        _require(self._sid_reader() == self._sid, "ROMA_WRONG_PRINCIPAL")
        _require((recipient_ref, selected_bytes, selected_sha256, task_ref, task_revision) ==
                 (bound.recipient_ref, bound.selected_bytes, bound.selected_sha256,
                  bound.task_ref, bound.task_revision), "ROMA_QUEUE_BINDING_MISMATCH")
        _require(selection == bound.selection, "ROMA_SELECTION_BINDING_MISMATCH")
        _require(not self._attempted, "ROMA_QUEUE_OUTCOME_UNKNOWN_NO_RETRY")
        _require(self._check_registry() == self._registry_sha256,
                 "ROMA_REGISTRY_CHANGED_NO_SEND")
        message = ("ROM_A_NORMAL_RETURN\n"
                   f"TASK={task_ref}\nREVISION={task_revision}\n"
                   f"SELECTION={selection}\nSHA256={selected_sha256}\n"
                   "QUEUED_ONLY_NOT_START_OR_USE\n---SELECTED_BYTES_UTF8---\n"
                   + selected_bytes.decode("utf-8"))
        self._attempted = True
        # Hold a no-write/no-delete shared handle through CreateProcess and exit.
        with open_protected(self._exe, root=self._exe_root,
                            owner_sid=self._owner_sid) as (stream, final):
            _require(final == ntpath.normcase(ntpath.normpath(str(self._exe))),
                     "ROMA_CODEX_FINAL_PATH_MISMATCH")
            _require(sha256_stream(stream) == self._exe_sha256,
                     "ROMA_CODEX_EXE_HASH_MISMATCH")
            result = self._runner([str(self._exe), "queue", "--thread", bound.recipient_thread_id,
                                   "--message", message], shell=False, capture_output=True,
                                  text=True, timeout=30, check=False,
                                  cwd=str(self._exe_root))
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
                                       expected_custody_owner_sid: str,
                                       content: LocalContentBinding,
                                       delivery: QueueBinding,
                                       codex_exe: Path,
                                       executable_root: Path,
                                       expected_exe_sha256: str,
                                       registry_path: Path,
                                       registry_root: Path) -> tuple[FixedLocalEpisodeReader,
                                                                 FixedCodexQueueSender]:
    """Protected composition-root call; never expose these arguments as MCP input.

    One explicit local artifact and one exact recipient/task/payload bind.  No
    default root, identity, content reference, recipient or executable exists.
    The caller must provide verified operator custody and an authentic episode.
    """
    reader = FixedLocalEpisodeReader(root=operator_root,
                                     expected_process_sid=expected_process_sid,
                                     expected_custody_owner_sid=expected_custody_owner_sid,
                                     bindings=(content,))
    sender = FixedCodexQueueSender(codex_exe=codex_exe,
                                   executable_root=executable_root,
                                   expected_exe_sha256=expected_exe_sha256,
                                   registry_path=registry_path,
                                   registry_root=registry_root,
                                   expected_custody_owner_sid=expected_custody_owner_sid,
                                   expected_process_sid=expected_process_sid,
                                   binding=delivery)
    return reader, sender
