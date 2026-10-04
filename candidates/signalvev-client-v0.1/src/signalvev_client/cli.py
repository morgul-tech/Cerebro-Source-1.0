"""Minimal FOREGROUND CLI: check-config | health | send | listen. JSON lines on stdout, diagnostics on stderr.

No daemon, no service manager, no scheduler. `listen` runs until Ctrl+C / SIGTERM / --duration / --max-frames.
Exit codes: 0 ok | 2 usage/config/event error | 3 NOT_SENT | 4 UNKNOWN_SEND (or send in flight) | 5 connect/health
failure | 6 listen ended because the connection was lost.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from . import __version__
from .config import load_config
from .errors import ClientError, ConfigError, ConnectError
from .session import CLAIMS, ListenClient, SendClient, health, load_resolver, precheck_event
from signalvev_sensing.model import SensingError

EXIT_OK, EXIT_USAGE, EXIT_NOT_SENT, EXIT_UNKNOWN, EXIT_CONNECT, EXIT_CONN_LOST = 0, 2, 3, 4, 5, 6
_SEND_EXIT = {"ACCEPTED": EXIT_OK, "NOT_SENT": EXIT_NOT_SENT, "UNKNOWN_SEND": EXIT_UNKNOWN, "IN_FLIGHT": EXIT_UNKNOWN}


def _emit(out: TextIO, obj: dict[str, Any]) -> None:
    out.write(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
    out.flush()


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="signalvev-client", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=f"signalvev-client {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    for name, helptext in (("check-config", "validate the configuration offline (no network)"),
                           ("health", "config + environment report; --connect adds one bounded server round-trip"),
                           ("send", "publish ONE committed owner event (JSON file) through the existing sender"),
                           ("listen", "subscribe and route frames into the existing receiver (foreground)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--config", required=True, help="path to the TOML configuration")
        if name == "health":
            sp.add_argument("--connect", action="store_true", help="also connect and PING the server once")
        if name == "send":
            sp.add_argument("--event", required=True, help="owner event JSON (commit+readback required)")
        if name == "listen":
            sp.add_argument("--duration", type=float, help="stop after this many seconds")
            sp.add_argument("--max-frames", type=int, help="stop after this many frames were handled")
    return p


def _install_signals(stop: Callable[[], None]) -> None:
    def handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass                                    # not the main thread / unsupported on this platform


def main(argv: Sequence[str] | None = None, *, connect_fn: Callable[..., Any] | None = None,
         stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    out, err = stdout or sys.stdout, stderr or sys.stderr
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        _emit(out, {"command": args.command, "ok": False, "error": exc.code, "detail": exc.detail})
        return EXIT_USAGE

    if args.command == "check-config":
        _emit(out, {"command": "check-config", "ok": True, "config": cfg.public_summary(),
                    "network": "NOT_USED"})
        return EXIT_OK

    if args.command == "health":
        report = health(cfg, connect=args.connect, connect_fn=connect_fn)
        _emit(out, {"command": "health", **report})
        return EXIT_OK if report["ok"] else EXIT_CONNECT

    if args.command == "send":
        try:
            raw = json.loads(Path(args.event).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _emit(out, {"command": "send", "ok": False, "error": "EVENT_UNREADABLE", "detail": type(exc).__name__,
                        "network": "NOT_USED"})
            return EXIT_USAGE
        try:
            precheck_event(raw, ttl_seconds=cfg.ttl_seconds)
        except SensingError as exc:                      # SensingError is a ValueError: it must be caught on its own
            _emit(out, {"command": "send", "ok": False, "error": exc.code, "detail": str(exc), "network": "NOT_USED"})
            return EXIT_USAGE
        try:
            with SendClient(cfg, connect_fn=connect_fn) as client:
                outcome = client.send(raw)
                status = client.status()
        except ConnectError as exc:
            _emit(out, {"command": "send", "ok": False, "error": exc.code, "detail": exc.detail,
                        "published": False})
            return EXIT_CONNECT
        except ClientError as exc:
            _emit(out, {"command": "send", "ok": False, "error": exc.code, "detail": exc.detail})
            return EXIT_USAGE
        _emit(out, {"command": "send", "ok": outcome.state == "ACCEPTED", "event_id": outcome.event_id,
                    "state": outcome.state, "reason": outcome.reason, "replay_refused": outcome.replay_refused,
                    "sender_stages": list(outcome.stages), "claims": dict(CLAIMS),
                    "note": "ACCEPTED is transport evidence only; UNKNOWN_SEND is never blindly replayed",
                    "connection": {"state": status["state"], "server": status.get("server")}})
        return _SEND_EXIT.get(outcome.state, EXIT_UNKNOWN)

    # listen
    try:
        resolver = load_resolver(cfg)
    except ClientError as exc:
        _emit(out, {"command": "listen", "ok": False, "error": exc.code, "detail": exc.detail})
        return EXIT_USAGE

    def on_result(res: Any) -> None:
        _emit(out, {"event": "INGRESS", "event_id": res.event_id, "disposition": res.disposition, "reason": res.reason,
                    "applicability": res.applicability, "resolver_calls": res.resolver_calls,
                    "activation": res.activation.decision, "returned": res.returned,
                    "claim": "ACK_READ != WORK_CONSUMED != EFFECT"})

    client = ListenClient(cfg, resolver=resolver, connect_fn=connect_fn, on_result=on_result)
    _install_signals(client.stop_event.set)
    try:
        client.start()
    except ConnectError as exc:
        _emit(out, {"command": "listen", "ok": False, "error": exc.code, "detail": exc.detail})
        return EXIT_CONNECT
    except ClientError as exc:
        _emit(out, {"command": "listen", "ok": False, "error": exc.code, "detail": exc.detail})
        return EXIT_USAGE
    _emit(out, {"event": "LISTEN_START", "node_id": cfg.node_id, "subjects": client.status()["subjects"],
                "resolver": client.status()["resolver"], "claims": dict(CLAIMS)})
    try:
        reason = client.wait(duration=args.duration, max_frames=args.max_frames)
    except KeyboardInterrupt:
        reason = "STOP_REQUESTED"
    finally:
        client.stop()
    _emit(out, {"event": "LISTEN_END", "reason": reason, **client.summary()})
    return EXIT_CONN_LOST if reason == "CONNECTION_LOST" else EXIT_OK
