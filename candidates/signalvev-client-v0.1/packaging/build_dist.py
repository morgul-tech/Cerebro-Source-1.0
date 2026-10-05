#!/usr/bin/env python3
"""Build the installable wheel from a staged tree (self-contained writer, no setuptools needed). Dev/build tool: NOT part of the runtime.

The wheel carries three things, all taken from this repository at build time and pinned by RESOURCE_MANIFEST.json:
  * signalvev_client            (new, this candidate)
  * signalvev_sensing           (the existing core, byte-identical to candidates/signalvev-sensing-runtime-v0.1/src)
  * _reference_resources/       (the v0.1/v0.9/v0.15/v0.16 reference modules + subject registry/schema, byte-identical,
                                 in the repo layout so the unchanged core bridge and v0.1 still find them)
No code is copied-and-edited: every bundled file's SHA-256 equals its repository source and is verified here AND at
import time. Reproducible: SOURCE_DATE_EPOCH is the source commit time (or 0 without git).

usage: python3 candidates/signalvev-client-v0.1/packaging/build_dist.py --out DIR
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANDIDATE = HERE.parent
REPO = CANDIDATE.parents[1]
CORE_SRC = REPO / "candidates" / "signalvev-sensing-runtime-v0.1" / "src" / "signalvev_sensing"
CLIENT_SRC = CANDIDATE / "src" / "signalvev_client"
REFERENCE_PY = ["signalvev_reference_v01_validation.py", "signalvev_reference_v09_validation.py",
                "signalvev_reference_v15_validation.py", "signalvev_reference_v16_validation.py"]
REFERENCE_DATA = ["cerebro-message-v1-candidate.schema.json", "subject-registry-v0.1-candidate.json"]
RES = "signalvev_client/_reference_resources"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def committed_bytes(src: Path) -> bytes:
    """Read a tracked input from HEAD, rejecting any staged or working-tree edit.

    Git's blob is the canonical byte source. This avoids checkout EOL conversion
    changing wheel contents while keeping real source drift fail-closed.
    """
    rel = src.relative_to(REPO).as_posix()
    check = subprocess.run(["git", "-C", str(REPO), "diff", "--quiet", "HEAD", "--", rel],
                           capture_output=True)
    if check.returncode != 0:
        raise RuntimeError(f"SOURCE_HEAD_MISMATCH: {rel}")
    try:
        return subprocess.run(["git", "-C", str(REPO), "show", f"HEAD:{rel}"],
                              check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"SOURCE_NOT_IN_HEAD: {rel}") from exc


def committed_python_sources(root: Path) -> list[Path]:
    """Enumerate the top-level packaged Python files at HEAD and reject tree drift."""
    rel_root = root.relative_to(REPO).as_posix()
    try:
        listed = subprocess.run(["git", "-C", str(REPO), "ls-tree", "-r", "--name-only",
                                 "HEAD", "--", rel_root], check=True, capture_output=True,
                                text=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"SOURCE_HEAD_MISMATCH: cannot enumerate {rel_root}") from exc
    tracked = {Path(name).name for name in listed
               if Path(name).parent.as_posix() == rel_root and name.endswith(".py")}
    present = {path.name for path in root.glob("*.py") if path.is_file()}
    if tracked != present:
        missing, extra = sorted(tracked - present), sorted(present - tracked)
        detail = f"missing={missing}; untracked={extra}"
        raise RuntimeError(f"SOURCE_HEAD_MISMATCH: {rel_root}: {detail}")
    return [root / name for name in sorted(tracked)]


def stage(dest: Path) -> dict:
    files: dict[str, str] = {}
    sources: dict[str, str] = {}

    def put(src: Path, rel: str, *, pin: bool = True) -> None:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        content = committed_bytes(src)
        target.write_bytes(content)
        assert target.read_bytes() == content, rel
        if pin:
            files[rel] = hashlib.sha256(content).hexdigest()
            sources[rel] = src.relative_to(REPO).as_posix()

    for f in committed_python_sources(CORE_SRC):
        put(f, f"signalvev_sensing/{f.name}")
    for f in sorted(REFERENCE_PY):
        put(REPO / "tooling" / "validator" / f, f"{RES}/tooling/validator/{f}")
    for f in sorted(REFERENCE_DATA):
        put(REPO / "candidates" / "signalvev-reference-v0.1" / f, f"{RES}/candidates/signalvev-reference-v0.1/{f}")
    for f in committed_python_sources(CLIENT_SRC):                # the client package itself is also pinned (tamper evidence)
        put(f, f"signalvev_client/{f.name}")
    manifest = {"schema": "signalvev-client-resource-manifest/v0.1", "authority": "NONE",
                "source_repository": "morgul-tech/Cerebro-Source-1.0", "source_commit": git("rev-parse", "HEAD") or "NOT_AVAILABLE",
                "files": dict(sorted(files.items())), "sources": dict(sorted(sources.items()))}
    (dest / RES).mkdir(parents=True, exist_ok=True)
    (dest / RES / "RESOURCE_MANIFEST.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    put(CANDIDATE / "pyproject.toml", "pyproject.toml", pin=False)
    put(CANDIDATE / "README.md", "README.md", pin=False)
    return manifest


def _b64(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def write_wheel(files: dict[str, bytes], *, name: str, version: str, metadata: list[str], entry_points: str | None,
                out: Path, epoch: int, requires_python: str) -> Path:
    """Minimal deterministic PEP 427 pure-Python wheel writer (no setuptools: the build host's setuptools is not
    needed, and the output is byte-reproducible for a given source tree + SOURCE_DATE_EPOCH)."""
    dist = f"{name.replace('-', '_')}-{version}"
    info = f"{dist}.dist-info"
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}", *metadata,
            f"Requires-Python: {requires_python}"]
    body = dict(files)
    body[f"{info}/METADATA"] = ("\n".join(meta) + "\n").encode("utf-8")
    body[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: signalvev-client-build_dist 0.1\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    if entry_points:
        body[f"{info}/entry_points.txt"] = entry_points.encode("utf-8")
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


def build(stage_dir: Path, out: Path, epoch: str) -> list[Path]:
    project = tomllib.loads((stage_dir / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    metadata = [f"Summary: {project['description']}"]
    metadata += [f"Classifier: {c}" for c in project.get("classifiers", [])]
    metadata += [f"Requires-Dist: {d}" for d in project.get("dependencies", [])]
    for extra, reqs in sorted(project.get("optional-dependencies", {}).items()):
        metadata.append(f"Provides-Extra: {extra}")
        metadata += [f'Requires-Dist: {r}; extra == "{extra}"' for r in reqs]
    readme = (stage_dir / project["readme"]).read_text(encoding="utf-8")
    metadata += ["Description-Content-Type: text/markdown", "", readme]
    files = {p.relative_to(stage_dir).as_posix(): p.read_bytes()
             for p in sorted(stage_dir.rglob("*")) if p.is_file() and p.name not in ("pyproject.toml", "README.md")
             and "__pycache__" not in p.parts}
    scripts = "".join(f"{k} = {v}\n" for k, v in sorted(project.get("scripts", {}).items()))
    wheel = write_wheel(files, name=project["name"], version=project["version"], metadata=metadata,
                        entry_points=f"[console_scripts]\n{scripts}" if scripts else None, out=out, epoch=int(epoch),
                        requires_python=project["requires-python"])
    return [wheel]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--keep-stage", type=Path, help="also leave the staged tree here (inspection)")
    args = ap.parse_args()
    commit_time = git("log", "-1", "--format=%ct")
    epoch = commit_time if commit_time and commit_time.isdigit() else "0"
    with tempfile.TemporaryDirectory(prefix="signalvev-client-stage-") as tmp:
        stage_dir = Path(tmp) / "stage"
        manifest = stage(stage_dir)
        if args.keep_stage:
            shutil.copytree(stage_dir, args.keep_stage, dirs_exist_ok=True)
        built = build(stage_dir, args.out.resolve(), epoch)
    print(json.dumps({"built": [{"file": p.name, "sha256": sha256(p), "bytes": p.stat().st_size} for p in built],
                      "pinned_files": len(manifest["files"]), "source_commit": manifest["source_commit"],
                      "source_date_epoch": epoch}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
