"""Programmatic Liv registration for the two separately authenticated routes.

The PM host injects only its current PM Context caller. The runtime host injects
only its current read-only Context caller. Neither callback can be loaded from a
profile, environment token, or the public CLI.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from signalvev_sensing.resolver import ResolverUnavailable

from . import pm_x9
from .config import load_config
from .context_notice import OBSERVE, PMNoticePublisher, RuntimeContextResolver
from .local_runtime_admission import read_admitted_local_session
from .local_runtime_session_host import LocalRuntimeSessionError
from .local_runtime_provider_port import (
    PROFILE_PATH, STATE_DIR, PortUnavailable, resolve_current_notice,
)
from .pm_x9_cli import _load_settings
from .pm_x9_normal_host import HostRefused, _profile, _same_path
from .session import ListenClient

REMOTE_FACTORY = "signalvev_client.context_notice_host:make_ports"


def make_ports(*_args: Any, **_kwargs: Any) -> None:
    """Prevent the old in-process PmX9HostPorts loader from binding this route."""
    raise HostRefused("REMOTE_CONTEXT_NOTICE_ROUTE_ONLY")


def _binding(profile_path: Path, binding_path: Path, *, require_enabled: bool = True
             ) -> tuple[dict, pm_x9.PmX9Settings]:
    profile = _profile(profile_path)
    if require_enabled and profile.get("enabled") is not True:
        raise HostRefused("PROFILE_DISABLED")
    settings = _load_settings(binding_path)
    if settings.mode != pm_x9.MODE_PRODUCTION or settings.ports_factory != REMOTE_FACTORY:
        raise HostRefused("REMOTE_CONTEXT_FACTORY_REQUIRED")
    return profile, settings


def compose_pm(profile_path: Path, binding_path: Path, sender_path: Path, *,
               call_pm: Callable[[str, dict[str, str]], Any]) -> PMNoticePublisher:
    """Construct an inert sender; no service or NATS call occurs here."""
    profile, settings = _binding(profile_path, binding_path)
    if not callable(call_pm):
        raise HostRefused("CURRENT_AUTHENTICATED_PM_CALL_REQUIRED")
    sender = load_config(sender_path)
    if (sender.server != profile["broker"] or sender.interests or
            not _same_path(sender.credentials_file, profile.get("publisher_credential_ref")) or
            not _same_path(sender.evidence_dir, profile.get("sender_state_dir"))):
        raise HostRefused("PUBLISHER_PROFILE_MISMATCH")
    return PMNoticePublisher(settings=settings, call_pm=call_pm, client_config=sender)


def compose_runtime(profile_path: Path, binding_path: Path, receiver_path: Path, *,
                    call_runtime: Callable[[str, dict[str, str]], Any]) -> ListenClient:
    """Construct an inert receiver; it starts only on explicit host call."""
    profile, settings = _binding(profile_path, binding_path)
    if not callable(call_runtime):
        raise HostRefused("CURRENT_AUTHENTICATED_RUNTIME_CALL_REQUIRED")
    receiver = load_config(receiver_path)
    if (receiver.server != profile["broker"] or receiver.resolver_kind != "factory" or
            not _same_path(receiver.credentials_file, profile.get("receiver_credential_ref")) or
            not _same_path(receiver.evidence_dir, profile.get("receiver_state_dir")) or
            len(receiver.interests) != 1 or
            receiver.interests[0].owner_ref != settings.owner_ref or
            receiver.interests[0].referent_type != pm_x9.PM_READY_HINT or
            receiver.interests[0].referent_id is not None):
        raise HostRefused("RECEIVER_PROFILE_MISMATCH")
    resolver = RuntimeContextResolver(owner_ref=settings.owner_ref, call_runtime=call_runtime)
    return ListenClient(receiver, resolver=resolver)


def compose_admitted_runtime(profile_path: Path, binding_path: Path,
                             receiver_path: Path) -> ListenClient:
    """Build an inert receiver from the current provider admission and OFF lease.

    The protected resolver port is fixed to this Liv host and rechecks admission
    for every Context resolution; callers cannot inject an authority callback.
    """
    if (not isinstance(profile_path, Path) or profile_path.is_symlink()
            or profile_path.resolve() != PROFILE_PATH):
        raise HostRefused("LOCAL_SESSION_PROFILE_PATH_MISMATCH")
    profile, settings = _binding(profile_path, binding_path, require_enabled=False)
    if profile.get("enabled") is not False:
        raise HostRefused("LOCAL_SESSION_PROFILE_MUST_REMAIN_OFF")
    receiver = load_config(receiver_path)
    if (receiver.server != profile["broker"] or receiver.resolver_kind != "factory" or
            not _same_path(receiver.credentials_file, profile.get("receiver_credential_ref")) or
            not _same_path(receiver.evidence_dir, profile.get("receiver_state_dir")) or
            len(receiver.interests) != 1 or
            receiver.interests[0].owner_ref != settings.owner_ref or
            receiver.interests[0].referent_type != pm_x9.PM_READY_HINT or
            receiver.interests[0].referent_id is not None):
        raise HostRefused("RECEIVER_PROFILE_MISMATCH")
    read_admitted_local_session(profile_path=PROFILE_PATH, state_dir=STATE_DIR)

    def current_call(name: str, arguments: dict[str, str]) -> Any:
        if name != OBSERVE or not isinstance(arguments, dict) or set(arguments) != {"event_id"}:
            raise ResolverUnavailable("BK07_RUNTIME_TOOL_NOT_ALLOWED")
        try:
            return resolve_current_notice(arguments["event_id"])
        except (LocalRuntimeSessionError, PortUnavailable) as exc:
            raise ResolverUnavailable("BK07_ADMISSION_NOT_CURRENT") from exc

    resolver = RuntimeContextResolver(owner_ref=settings.owner_ref, call_runtime=current_call)
    return ListenClient(receiver, resolver=resolver)
