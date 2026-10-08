"""Read-only Windows handle custody for one protected local ROM-A episode.

The file is opened without write/delete sharing.  Final path, reparse state,
owner and every allow ACE are checked on handles, then bytes are read from the
same handle.  No caller path is accepted by the public normal-host operation.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import msvcrt
import ntpath
import os
from pathlib import Path
from typing import Iterator


SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
_TRUSTED_OWNERS = {SYSTEM, ADMINISTRATORS}
_WRITE_BITS = (0x10000000 | 0x40000000 | 0x00080000 | 0x00040000 |
               0x00010000 | 0x00000100 | 0x00000040 | 0x00000010 |
               0x00000004 | 0x00000002)
_REPARSE = 0x400
_INVALID = ctypes.c_void_p(-1).value


class _FileInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


class _AclHeader(ctypes.Structure):
    _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte),
                ("size", wintypes.WORD), ("ace_count", wintypes.WORD),
                ("reserved2", wintypes.WORD)]


def _fail(reason: str) -> None:
    raise ValueError(reason)


def _apis():
    if os.name != "nt":
        _fail("ROMA_WINDOWS_CUSTODY_REQUIRED")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,
                                                   ctypes.POINTER(_FileInfo)]
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                 wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(wintypes.LPWSTR)]
    return kernel, advapi


def _sid_text(advapi, kernel, pointer: ctypes.c_void_p) -> str:
    rendered = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(pointer, ctypes.byref(rendered)):
        _fail("ROMA_CUSTODY_SID_UNAVAILABLE")
    try:
        return rendered.value
    finally:
        kernel.LocalFree(rendered)


def _security(handle, *, expected_owner: str, kernel, advapi) -> None:
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = advapi.GetSecurityInfo(handle, 1, 1 | 4, ctypes.byref(owner), None,
                                  ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if code != 0 or not owner.value or not dacl.value:
        _fail("ROMA_PROTECTED_DACL_REQUIRED")
    try:
        if _sid_text(advapi, kernel, owner) != expected_owner:
            _fail("ROMA_CUSTODY_OWNER_MISMATCH")
        header = ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents
        for index in range(header.ace_count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                _fail("ROMA_CUSTODY_ACE_UNREADABLE")
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 1:  # ACCESS_DENIED_ACE: safe to leave in force.
                continue
            if kind != 0:  # Object/callback allow ACEs need a separate proof.
                _fail("ROMA_CUSTODY_ACE_UNSUPPORTED")
            mask = wintypes.DWORD.from_address(ace.value + 4).value
            sid = _sid_text(advapi, kernel, ctypes.c_void_p(ace.value + 8))
            if mask & _WRITE_BITS and sid not in _TRUSTED_OWNERS:
                _fail("ROMA_UNTRUSTED_WRITE_ACE")
    finally:
        kernel.LocalFree(descriptor)


def _normal(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        _fail("ROMA_NETWORK_PATH_FORBIDDEN")
    if path.startswith("\\\\?\\"):
        path = path[4:]
    if len(path) < 3 or path[1:3] != ":\\":
        _fail("ROMA_LOCAL_DRIVE_PATH_REQUIRED")
    return ntpath.normcase(ntpath.normpath(path))


def _inspect(handle, expected: Path, owner: str, kernel, advapi) -> str:
    info = _FileInfo()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        _fail("ROMA_HANDLE_INFO_UNAVAILABLE")
    if info.attributes & _REPARSE:
        _fail("ROMA_HANDLE_REPARSE_FORBIDDEN")
    buffer = ctypes.create_unicode_buffer(32768)
    used = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
    if not used or used >= len(buffer):
        _fail("ROMA_FINAL_PATH_UNAVAILABLE")
    final = _normal(buffer.value)
    if final != _normal(str(expected)):
        _fail("ROMA_FINAL_PATH_MISMATCH")
    _security(handle, expected_owner=owner, kernel=kernel, advapi=advapi)
    return final


@contextmanager
def open_protected(path: Path, *, root: Path, owner_sid: str) -> Iterator[tuple[object, str]]:
    """Yield a locked read handle and its verified final path.

    The protected root's immediate parent must itself be protected.  Every
    descendant component is opened with OPEN_REPARSE_POINT and checked before
    the target, whose handle stays open through the consumer's operation.
    """
    if owner_sid not in _TRUSTED_OWNERS or not path.is_absolute() or not root.is_absolute():
        _fail("ROMA_PROTECTED_ROOT_OR_OWNER_REQUIRED")
    try:
        relative = path.relative_to(root)
    except ValueError:
        _fail("ROMA_PATH_ESCAPE")
    if not relative.parts:
        _fail("ROMA_TARGET_FILE_REQUIRED")
    kernel, advapi = _apis()
    chain = [root.parent, root]
    chain.extend(root.joinpath(*relative.parts[:index])
                 for index in range(1, len(relative.parts)))
    for directory in chain:
        handle = kernel.CreateFileW(str(directory), 0x80000000, 1, None, 3,
                                    0x02000000 | 0x00200000, None)
        if handle == _INVALID:
            _fail("ROMA_CUSTODY_DIRECTORY_UNAVAILABLE")
        try:
            # This strict first adapter requires one admin/SYSTEM-owned chain.
            _inspect(handle, directory, owner_sid, kernel, advapi)
        finally:
            kernel.CloseHandle(handle)
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3,
                                0x00200000, None)
    if handle == _INVALID:
        _fail("ROMA_CUSTODY_FILE_UNAVAILABLE")
    transferred = False
    try:
        final = _inspect(handle, path, owner_sid, kernel, advapi)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        transferred = True
        with os.fdopen(fd, "rb") as stream:
            yield stream, final
    finally:
        if not transferred:
            kernel.CloseHandle(handle)


def sha256_stream(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1 << 20):
        digest.update(chunk)
    return digest.hexdigest()
