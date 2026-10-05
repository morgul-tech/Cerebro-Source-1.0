#!/usr/bin/env python3
"""Prove one committed head builds identical wheels from clean LF and CRLF checkouts."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUILD = Path("candidates/signalvev-client-v0.1/packaging/build_dist.py")


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(args, cwd=cwd or REPO, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def main() -> int:
    head = run("git", "rev-parse", "HEAD")
    results: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(prefix="signalvev-eol-regression-") as tmp:
        root = Path(tmp)
        for mode in ("false", "true"):
            checkout = root / f"checkout-{mode}"
            run("git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(REPO), str(checkout))
            run("git", "config", "core.autocrlf", mode, cwd=checkout)
            run("git", "checkout", "--quiet", "--detach", head, cwd=checkout)
            output = checkout / "wheelhouse"
            run(sys.executable, str(BUILD), "--out", str(output), cwd=checkout)
            wheel = next(output.glob("*.whl"))
            data = wheel.read_bytes()
            results[mode] = {"path": wheel.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if results["false"] != results["true"]:
        raise SystemExit("LF/CRLF wheel mismatch: " + json.dumps(results, sort_keys=True))
    print(json.dumps({"result": "PASS", "head": head, "checkouts": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
