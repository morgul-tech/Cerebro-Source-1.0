"""``signalvev-pm-x9`` -- binding entry point for the BK07 PM-to-X9 read flow.

  signalvev-pm-x9 diagnose --config pm_x9.toml   per-port binding diagnostics; never contacts a provider or network
  signalvev-pm-x9 selftest                        the whole flow with SYNTHETIC_TEST_ONLY ports, through the same factory

Exit codes: 0 ok | 2 config/usage error | 3 default-off or unbound (diagnostics on stdout) | 4 self-test failed.
Production mode is only diagnosed here; the host process composes ``build_binding`` with its real ports.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import pm_x9

MAX_CONFIG_BYTES = 16 * 1024


def _emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n")


def _load_settings(path: Path) -> pm_x9.PmX9Settings:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise pm_x9.PmX9ConfigError("CONFIG_TOO_LARGE")
        doc = tomllib.loads(path.read_bytes().decode("utf-8"))
    except OSError:
        raise pm_x9.PmX9ConfigError("CONFIG_UNREADABLE", path.name) from None
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise pm_x9.PmX9ConfigError("CONFIG_NOT_VALID_TOML", type(exc).__name__) from None
    if set(doc) - {"pm_x9"}:
        raise pm_x9.PmX9ConfigError("UNKNOWN_SECTION", ",".join(sorted(set(doc) - {"pm_x9"})))
    return pm_x9.parse_settings(doc.get("pm_x9", {}))


def diagnose(path: Path) -> int:
    settings = _load_settings(path)
    if settings.mode == pm_x9.MODE_OFF:
        _emit({"result": "PM_X9_DEFAULT_OFF", "diagnostics": pm_x9.diagnose(settings, None),
               "settings": settings.public_summary(), "claims": pm_x9.CLAIMS})
        return 3
    try:
        ports = pm_x9.load_ports(settings)
        binding = pm_x9.build_binding(settings, ports)
    except pm_x9.PmX9Unbound as exc:
        _emit({"result": exc.code, "diagnostics": exc.diagnostics, "settings": settings.public_summary(),
               "claims": pm_x9.CLAIMS})
        return 3
    _emit({"result": "BOUND_NOT_STARTED", "diagnostics": pm_x9.diagnose(settings, ports),
           "status": binding.status(), "note": "diagnose never sends, listens, reads PM or appends"})
    return 0


def selftest() -> int:
    """SYNTHETIC_TEST_ONLY end-to-end run: PM receipt -> D0 -> receiver -> pointer readback -> explicit consume."""
    from . import pm_x9_synthetic as syn

    world = syn.SyntheticWorld()
    binding = None
    report: dict[str, Any] = {"label": syn.LABEL, "adapters": None}
    try:
        receipt = "pm-receipt:synthetic-selftest"
        world.pm.seed_hint(receipt)
        binding = pm_x9.build_binding(world.settings, world.ports())
        report["adapters"] = pm_x9._adapters.origin()
        binding.start_listener()
        binding.open_sender()
        sent = binding.send_hint(receipt)
        arrived = binding.wait_for_ingress(1, timeout=5.0)
        deposits = [vars(r) for r in binding.sink.records]
        pulse = binding.pulse(human_conversation_active=False)
        consumed = pulse.processed[0] if pulse.processed else None
        report.update({
            "send": vars(sent), "ingress_handled": arrived, "deposits": deposits,
            "consume": None if consumed is None else {k: v for k, v in vars(consumed).items() if k != "record"},
            "counts": {"pm_reads": world.pm.read_calls, "pm_rereads": world.pm.reread_calls,
                       "publishes": len(world.broker.published), "pointer_appends": world.store.pointer_appends,
                       "disposition_appends": world.store.disposition_appends},
            "claims": pm_x9.CLAIMS})
        ok = consumed is not None and consumed.disposition == "MATERIAL_ROUTE" and consumed.state == "DISPOSITION_READBACK"
        report["result"] = "SELFTEST_PASS_SYNTHETIC_ONLY" if ok else "SELFTEST_FAIL"
        _emit(report)
        return 0 if ok else 4
    finally:
        if binding is not None:
            binding.close()
        world.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signalvev-pm-x9", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("diagnose", help="per-port binding diagnostics (no I/O against providers)")
    d.add_argument("--config", required=True, type=Path)
    sub.add_parser("selftest", help="SYNTHETIC_TEST_ONLY whole-flow run through the same factory")
    args = ap.parse_args(argv)
    try:
        return diagnose(args.config) if args.cmd == "diagnose" else selftest()
    except pm_x9.PmX9ConfigError as exc:
        _emit({"result": "CONFIG_ERROR", "code": exc.code, "detail": exc.detail})
        return 2


if __name__ == "__main__":
    sys.exit(main())
