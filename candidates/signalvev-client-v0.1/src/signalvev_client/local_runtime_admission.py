"""Explicit, read-only admission of an existing Liv session to a Context binding.

This module never starts a host, enables a profile, emits an event, or sends to
NATS. The provider reader must be bound by a reviewed host implementation;
caller-supplied evidence and the local admission file are never authority.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Mapping

from .local_runtime_session_host import (
    LocalRuntimeSessionError, _write_json, read_current_local_session,
)

ADMISSION_SCHEMA = "cerebro-bk07-local-runtime-admission/v1"
_BINDING_FIELDS = (
    "binding_id", "binding_fingerprint", "project_revision", "session_revision",
    "session_ref", "issuer", "client_id", "audience", "tenant", "workspace",
    "project", "principal", "enabled",
)
_EXACT_IDENTITY = {
    "binding_id": "CSB-1DE1D89140DF68AC917715EC",
    "session_ref": "local:c549c9ba53c741d39558150beab3d2b6",
    "issuer": "https://dev-lmzknalapbdhfdua.us.auth0.com",
    "client_id": "s6Lu5katfEwbnFEoRyxq7RFvx1fG5i1v",
    "audience": "https://cerebro-context-deploy-production.up.railway.app/bk07-runtime",
    "tenant": "CEREBRO-TENANT-1",
    "workspace": "BOOT-FULL-IMPLEMENTER-20260902T033946Z-GENERIC-7BAC373D",
    "project": "TOTAL_MCP_REVISION",
    "principal": "s6Lu5katfEwbnFEoRyxq7RFvx1fG5i1v@clients",
}


def _binding(value: Mapping) -> dict:
    try:
        binding = {key: value[key] for key in _BINDING_FIELDS}
    except (KeyError, TypeError) as exc:
        raise LocalRuntimeSessionError("BK07_BINDING_INCOMPLETE") from exc
    strings = set(_BINDING_FIELDS) - {"enabled", "project_revision", "session_revision"}
    if (binding["enabled"] is not True
            or any(not isinstance(binding[key], str) or not binding[key] for key in strings)
            or not binding["session_ref"].startswith("local:")
            or re.fullmatch(r"[0-9a-f]{64}", binding["binding_fingerprint"]) is None
            or any(type(binding[key]) is not int or binding[key] < 1
                   for key in ("project_revision", "session_revision"))):
        raise LocalRuntimeSessionError("BK07_BINDING_INVALID")
    return binding


def _read_provider_binding() -> Mapping:
    """Fixed protected transport; no caller callback or authority argument."""
    from .local_runtime_provider_port import read_current_binding
    return read_current_binding()


def _provider_binding() -> dict:
    try:
        binding = _binding(_read_provider_binding())
        if any(binding[key] != expected for key, expected in _EXACT_IDENTITY.items()):
            raise LocalRuntimeSessionError("BK07_BINDING_IDENTITY_MISMATCH")
        return binding
    except LocalRuntimeSessionError:
        raise
    except Exception as exc:
        raise LocalRuntimeSessionError("BK07_PROVIDER_READ_FAILED") from exc


def _same_lease(first: Mapping, second: Mapping) -> bool:
    return all(first[key] == second[key] for key in
               ("session_ref", "pid", "process_start_epoch", "started_at_epoch"))


def admit_local_runtime_session(*, profile_path: Path, state_dir: Path,
                                clock: Callable[[], float] = time.time) -> dict:
    """Pin a host-read enabled provider binding to the already running lease."""
    expected = _provider_binding()
    first = read_current_local_session(profile_path=profile_path, state_dir=state_dir,
                                       expected_session_ref=expected["session_ref"], clock=clock)
    if _provider_binding() != expected:
        raise LocalRuntimeSessionError("BK07_BINDING_MISMATCH")
    second = read_current_local_session(profile_path=profile_path, state_dir=state_dir,
                                        expected_session_ref=expected["session_ref"], clock=clock)
    if not _same_lease(first, second):
        raise LocalRuntimeSessionError("BK07_SESSION_CHANGED")
    receipt = {"schema": ADMISSION_SCHEMA, "binding": expected,
               "session_ref": second["session_ref"], "pid": second["pid"],
               "process_start_epoch": second["process_start_epoch"],
               "started_at_epoch": second["started_at_epoch"],
               "admitted_at_epoch": clock()}
    _write_json(Path(state_dir) / "admission.json", receipt)
    return dict(receipt)


def read_admitted_local_session(*, profile_path: Path, state_dir: Path,
                                clock: Callable[[], float] = time.time) -> dict:
    """Recheck the lock, heartbeat, process identity, and provider on every read."""
    try:
        receipt = json.loads((Path(state_dir) / "admission.json").read_text(encoding="utf-8"))
        if receipt["schema"] != ADMISSION_SCHEMA:
            raise ValueError("schema")
        binding = _binding(receipt["binding"])
        session = read_current_local_session(profile_path=profile_path, state_dir=state_dir,
                                             expected_session_ref=receipt["session_ref"], clock=clock)
        if (binding["session_ref"] != receipt["session_ref"]
                or not _same_lease(receipt, session)):
            raise ValueError("lease")
        if _provider_binding() != binding:
            raise LocalRuntimeSessionError("BK07_BINDING_MISMATCH")
        current = read_current_local_session(profile_path=profile_path, state_dir=state_dir,
                                             expected_session_ref=receipt["session_ref"], clock=clock)
        if not _same_lease(session, current):
            raise ValueError("lease changed")
        return {"admission": receipt, "session": current}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LocalRuntimeSessionError("BK07_ADMISSION_NOT_CURRENT") from exc
