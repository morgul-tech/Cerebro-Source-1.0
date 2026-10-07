"""Normal BK07 host entry: one receipt send or a retained receiver, never X9 consume.

The owner supplies current PM and channel ports through the existing ports_factory.
This entry binds the already scoped NATS credentials and durable state directories
from the host profile; it neither creates owner receipts nor grants new rights.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

from . import pm_x9
from .config import load_config
from .pm_x9_cli import _load_settings

PROFILE_SCHEMA = "cerebro-bk07-normal-use-profile/v1"
SUBJECT = "cerebro.v1.artifact.pointer"
MAX_PROFILE_BYTES = 16 * 1024


class HostRefused(ValueError):
    pass


def _profile(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size > MAX_PROFILE_BYTES:
        raise HostRefused("PROFILE_UNAVAILABLE")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HostRefused("PROFILE_INVALID") from exc
    if not isinstance(doc, dict) or doc.get("schema") != PROFILE_SCHEMA or doc.get("mode") != "PRODUCTION":
        raise HostRefused("PROFILE_INVALID")
    if doc.get("subject") != SUBJECT or doc.get("broker") != "nats://10.77.0.1:4222":
        raise HostRefused("TRANSPORT_PROFILE_MISMATCH")
    rights = doc.get("rights", {})
    if (rights.get("no_new_acl") is not True or rights.get("wildcards_forbidden") is not True or
            rights.get("publisher_pub_only") != [SUBJECT] or rights.get("receiver_sub_only") != [SUBJECT]):
        raise HostRefused("RIGHTS_PROFILE_MISMATCH")
    operation = doc.get("x9_operation", {})
    if operation.get("host_auto_consume") is not False or operation.get("context_append_from_host") is not False:
        raise HostRefused("X9_OWNERSHIP_MISMATCH")
    return doc


def _same_path(actual: Path | None, expected: str | None) -> bool:
    return actual is not None and isinstance(expected, str) and actual.resolve() == Path(expected).resolve()


def compose(profile_path: Path, binding_path: Path, sender_path: Path, receiver_path: Path,
            *, require_enabled: bool = True) -> pm_x9.PmX9Binding:
    profile = _profile(profile_path)
    if require_enabled and profile.get("enabled") is not True:
        raise HostRefused("PROFILE_DISABLED")
    settings = _load_settings(binding_path)
    if settings.mode != pm_x9.MODE_PRODUCTION:
        raise HostRefused("BINDING_NOT_PRODUCTION")
    sender = load_config(sender_path)
    receiver = load_config(receiver_path)
    if (sender.server != profile["broker"] or receiver.server != profile["broker"] or
            not _same_path(sender.credentials_file, profile.get("publisher_credential_ref")) or
            not _same_path(receiver.credentials_file, profile.get("receiver_credential_ref")) or
            not _same_path(sender.evidence_dir, profile.get("sender_state_dir")) or
            not _same_path(receiver.evidence_dir, profile.get("receiver_state_dir")) or
            sender.interests or receiver.resolver_kind != "factory" or
            len(receiver.interests) != 1 or
            receiver.interests[0].owner_ref != settings.owner_ref or
            receiver.interests[0].referent_type != pm_x9.PM_READY_HINT or
            receiver.interests[0].referent_id is not None):
        raise HostRefused("CLIENT_PROFILE_MISMATCH")
    ports = pm_x9.load_ports(settings)
    # The factory owns authenticated PM and separately scoped channel ports;
    # transport paths are always taken from the reviewed host configuration.
    ports = replace(ports, client_config=sender, receiver_client_config=receiver)
    return pm_x9.build_binding(settings, ports)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--binding-config", required=True, type=Path)
    parser.add_argument("--sender-config", required=True, type=Path)
    parser.add_argument("--receiver-config", required=True, type=Path)
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("diagnose")
    send = sub.add_parser("send")
    send.add_argument("receipt_ref")
    sub.add_parser("listen")
    args = parser.parse_args(argv)
    binding = None
    try:
        binding = compose(args.profile, args.binding_config, args.sender_config, args.receiver_config,
                          require_enabled=args.operation != "diagnose")
        if args.operation == "diagnose":
            result = {"state": "BOUND_NOT_STARTED", "diagnostics": pm_x9.diagnose(binding.settings, binding.ports),
                      "profile_enabled": _profile(args.profile).get("enabled") is True,
                      "authority": "NONE", "recipient_use": "NOT_OBSERVED"}
        elif args.operation == "send":
            if not args.receipt_ref.startswith("pm-receipt:"):
                raise HostRefused("RECEIPT_REF_REQUIRED")
            binding.open_sender()
            result = {"operation": "send", **vars(binding.send_hint(args.receipt_ref)),
                      "authority": "NONE", "x9_consumed": False}
        else:
            binding.start_listener()
            result = {"operation": "listen", "state": "LISTENING", "authority": "NONE",
                      "x9_consumed": False}
        print(json.dumps(result, sort_keys=True, default=str), flush=True)
        if args.operation == "listen":
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        return 0
    except (HostRefused, pm_x9.PmX9Unbound, pm_x9.PmX9ConfigError, OSError, ValueError) as exc:
        print(json.dumps({"state": "REFUSED", "reason": getattr(exc, "code", str(exc).split(":", 1)[0]),
                          "authority": "NONE"}, sort_keys=True))
        return 2
    finally:
        if binding is not None:
            binding.close()


if __name__ == "__main__":
    sys.exit(main())
