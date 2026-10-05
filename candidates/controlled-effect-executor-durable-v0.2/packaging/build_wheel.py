#!/usr/bin/env python3
"""Build the pure-Python wheel with a small self-contained, deterministic PEP 427 writer (dev/build tool, NOT runtime).

Why not `pip wheel`: on the build host, Debian's patched setuptools 68.1.2 fails on Python 3.13
(``AttributeError: install_layout``) and the network policy blocks installing another one. `pyproject.toml` still
declares the normal setuptools backend, so a standard build host can use `pip wheel .`; this script produces the wheel
actually tested here. It packs exactly the two packages under ``src/`` (``controlled_effect_executor`` = V0.1 unchanged,
``controlled_effect_executor_durable``) plus dist-info, reads metadata from ``pyproject.toml``, and is reproducible for
a given tree and SOURCE_DATE_EPOCH.

usage: python3 packaging/build_wheel.py --out DIR [--epoch N]     (default epoch: $SOURCE_DATE_EPOCH, else the git commit time, else 0)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("controlled_effect_executor", "controlled_effect_executor_durable")


def _b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _epoch(arg: "str | None") -> int:
    if arg:
        return int(arg)
    if os.environ.get("SOURCE_DATE_EPOCH", "").isdigit():
        return int(os.environ["SOURCE_DATE_EPOCH"])
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct"], capture_output=True, text=True,
                             check=True).stdout.strip()
        return int(out) if out.isdigit() else 0
    except (OSError, subprocess.CalledProcessError):
        return 0


def build(out: Path, epoch: int) -> Path:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = project["name"], project["version"]
    files: dict = {}
    for pkg in PACKAGES:
        for path in sorted((ROOT / "src" / pkg).rglob("*.py")):
            if "__pycache__" not in path.parts:
                files[path.relative_to(ROOT / "src").as_posix()] = path.read_bytes()
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}", f"Summary: {project['description']}",
            f"License: {project['license']['text']}", f"Requires-Python: {project['requires-python']}"]
    meta += [f"Requires-Dist: {d}" for d in project.get("dependencies", [])]
    for extra, reqs in sorted(project.get("optional-dependencies", {}).items()):
        meta.append(f"Provides-Extra: {extra}")
        meta += [f'Requires-Dist: {r}; extra == "{extra}"' for r in reqs]
    readme = (ROOT / project["readme"]).read_text(encoding="utf-8")
    dist = f"{name.replace('-', '_')}-{version}"
    info = f"{dist}.dist-info"
    body = dict(files)
    body[f"{info}/METADATA"] = ("\n".join(meta + ["Description-Content-Type: text/markdown", "", readme])).encode("utf-8")
    body[f"{info}/WHEEL"] = (b"Wheel-Version: 1.0\nGenerator: cee-v02-build_wheel 0.2\n"
                             b"Root-Is-Purelib: true\nTag: py3-none-any\n")
    record = [f"{n},sha256={_b64(hashlib.sha256(b).digest())},{len(b)}" for n, b in sorted(body.items())]
    record.append(f"{info}/RECORD,,")
    body[f"{info}/RECORD"] = ("\n".join(record) + "\n").encode("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{dist}-py3-none-any.whl"
    stamp = time.gmtime(max(epoch, 315532800))[:6]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arc in sorted(body, key=lambda n: (n.endswith("/RECORD"), n)):
            zi = zipfile.ZipInfo(arc, date_time=stamp)
            zi.compress_type, zi.create_system, zi.external_attr = zipfile.ZIP_DEFLATED, 3, 0o100644 << 16
            z.writestr(zi, body[arc])
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--epoch")
    args = ap.parse_args()
    epoch = _epoch(args.epoch)
    wheel = build(args.out.resolve(), epoch)
    print(json.dumps({"file": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                      "bytes": wheel.stat().st_size, "source_date_epoch": epoch}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
