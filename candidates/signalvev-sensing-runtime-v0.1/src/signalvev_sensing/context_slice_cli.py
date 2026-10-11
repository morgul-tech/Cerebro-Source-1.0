"""CAP26 offline one-shot CLI (explicit commands only; no network, no daemon, no retry).

  python -m signalvev_sensing.context_slice_cli resolve       --registry REGISTRY --request REQUEST [--root DIR]
  python -m signalvev_sensing.context_slice_cli full-original --registry REGISTRY --request REQUEST [--root DIR]
                                                              [--max-full-output-bytes N --out NEW_FILE]
  python -m signalvev_sensing.context_slice_cli compare       --registry REGISTRY --request REQUEST [--root DIR]
                                                              --out-dir NEW_DIR [--max-full-output-bytes 131072]

--root defaults to the registry file's directory; registry paths are root-relative. Result envelopes (canonical JSON,
schema cap26.slice_result/v1) go to stdout. Exit codes: 0 SLICE_READY / FULL_ORIGINAL_READY / compare completed,
3 FALLBACK_REQUIRED, 2 any other typed outcome, 64 usage or unreadable request file.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

from .context_slice import (FALLBACK_REQUIRED, FULL_ORIGINAL_READY, MAX_REQUEST_BYTES, SLICE_READY,
                            ContextSliceResolver, RequestInvalid, ResultEnvelopeUnavailable, SliceResult, encode_envelope, parse_request)
from .context_slice_provider import LocalPinnedSnapshotProvider


def _load_request(path: Path):
    with open(path, "rb") as fh:
        data = fh.read(MAX_REQUEST_BYTES + 1)
    return parse_request(data)


def _resolver(args) -> tuple[ContextSliceResolver, LocalPinnedSnapshotProvider]:
    root = Path(args.root) if args.root else Path(args.registry).resolve().parent
    provider = LocalPinnedSnapshotProvider(root, args.registry)
    return ContextSliceResolver(provider), provider


def _exit(res: SliceResult) -> int:
    if res.outcome in (SLICE_READY, FULL_ORIGINAL_READY):
        return 0
    return 3 if res.outcome == FALLBACK_REQUIRED else 2


def _emit(obj) -> None:
    sys.stdout.buffer.write(obj.to_json() if isinstance(obj, SliceResult) else encode_envelope(obj) + b"\n")
    sys.stdout.flush()


def _scenario(name: str, res: SliceResult, provider: LocalPinnedSnapshotProvider, body_bytes: int) -> dict:
    envelope = len(res.to_json())
    return {"scenario": name, "outcome": res.outcome, "reason": res.reason,
            "resolver_counters": res.counters.as_dict(), "provider_actual_ops": provider.stats.as_dict(),
            "local_bytes_read_for_hashing_and_checks": res.counters.bytes_read,
            "model_bound_output_bytes": {"envelope": envelope, "body_file": body_bytes, "total": envelope + body_bytes},
            "failures": 0 if res.outcome in (SLICE_READY, FULL_ORIGINAL_READY) else 1, "retries": 0,
            "elapsed_ms": res.counters.elapsed_ms}


def cmd_compare(args) -> int:
    req = _load_request(Path(args.request))
    out_dir = Path(args.out_dir)
    if out_dir.exists():
        print("OUT_DIR_EXISTS_NO_OVERWRITE", file=sys.stderr)
        return 64
    out_dir.mkdir(parents=True)
    # Scenario 1: fresh provider + resolver, explicit authorized full original (baseline).
    r_full, p_full = _resolver(args)
    full = r_full.full_original(req, max_full_output_bytes=args.max_full_output_bytes,
                                out_path=out_dir / "full_original.bin")
    body = full.full_output["bytes"] if full.full_output else 0
    # Scenario 2: fresh provider + resolver (no shared object, no cache), the exact slice.
    r_sl, p_sl = _resolver(args)
    sl = r_sl.resolve(req)
    s_full = _scenario("FRESH_FULL_ORIGINAL", full, p_full, body)
    s_slice = _scenario("FRESH_SLICE", sl, p_sl, 0)
    a, b = s_full["model_bound_output_bytes"]["total"], s_slice["model_bound_output_bytes"]["total"]
    measurement = {
        "schema": "cap26.measurement/v1", "observed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                    "platform": platform.platform()},
        "content_ref": req.content_ref, "pinned_revision": req.expected_revision,
        "full_content_sha256": req.expected_content_sha256, "selector": req.selector.as_dict(),
        "max_full_output_bytes": args.max_full_output_bytes,
        "scenarios": [s_full, s_slice],
        "observed_comparison": {
            "model_bound_output_bytes_full": a, "model_bound_output_bytes_slice": b,
            "model_bound_output_bytes_reduction": a - b if full.outcome == FULL_ORIGINAL_READY and
            sl.outcome == SLICE_READY else None,
            "local_read_bytes_full": s_full["local_bytes_read_for_hashing_and_checks"],
            "local_read_bytes_slice": s_slice["local_bytes_read_for_hashing_and_checks"],
            "note": "The slice path still reads and hashes the WHOLE original locally (canonical full-SHA256 proof); "
                    "only the model-bound output is smaller. Byte reductions are not token savings."},
        "cache": "NONE: no cache exists; each scenario used a fresh provider and resolver; no assumed-cache scenario",
        "tokens": "UNKNOWN (no exact named/versioned tokenizer observed)",
        "model_turns": "UNKNOWN (no model use observed)", "spend": "UNKNOWN", "net_saving": "UNKNOWN",
        "authority": "NONE", "fixture": "SYNTHETIC_OWNER_ASSERTION registry; not live Drive/EDGE evidence"}
    (out_dir / "measurement.json").write_bytes(json.dumps(measurement, indent=1, sort_keys=True).encode() + b"\n")
    (out_dir / "full_original_envelope.json").write_bytes(full.to_json() + b"\n")
    (out_dir / "slice_envelope.json").write_bytes(sl.to_json() + b"\n")
    _emit(measurement)
    return 0 if full.outcome == FULL_ORIGINAL_READY and sl.outcome == SLICE_READY else 2


def main(argv=None) -> int:
    try:
        return _main(argv)
    except ResultEnvelopeUnavailable as exc:
        print(exc.code, file=sys.stderr)
        return 2  # ZERO serialized result bytes, no oversized error JSON


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="signalvev_sensing.context_slice_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("resolve", "full-original", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--registry", required=True)
        p.add_argument("--request", required=True)
        p.add_argument("--root")
        if name == "full-original":
            p.add_argument("--max-full-output-bytes", type=int)
            p.add_argument("--out")
        if name == "compare":
            p.add_argument("--out-dir", required=True)
            p.add_argument("--max-full-output-bytes", type=int, default=131072)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "compare":
            return cmd_compare(args)
        req = _load_request(Path(args.request))
    except RequestInvalid as exc:
        _emit({"schema": "cap26.slice_result/v1", "outcome": "INVALID_REQUEST", "reason": exc.code,
               "authority": "NONE"})
        return 2
    except OSError:
        print("REQUEST_FILE_UNREADABLE", file=sys.stderr)
        return 64
    resolver, _provider = _resolver(args)
    if args.cmd == "resolve":
        res = resolver.resolve(req)
    else:
        res = resolver.full_original(req, max_full_output_bytes=args.max_full_output_bytes, out_path=args.out)
    _emit(res)
    return _exit(res)


if __name__ == "__main__":
    sys.exit(main())
