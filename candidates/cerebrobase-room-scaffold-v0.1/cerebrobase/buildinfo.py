"""Content-addressed build identity + deterministic artifact.

build_id = sha256 over every packaged file (sorted POSIX path, NUL, bytes) EXCEPT BUILD_INFO.json, plus the release
label, truncated to 16 hex. The artifact carries BUILD_INFO.json {build_id, label, versions}; at runtime the same
function recomputes the id over the installed files, so /health shows the exact build and a tampered install is
BUILD_INTEGRITY_MISMATCH (not ready). Running from a source checkout without BUILD_INFO.json gives label "source".
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
import zlib
from pathlib import Path

from . import CONFIG_SCHEMA, PACKAGE, SCHEMA_VERSION, VERSION

ROOT = Path(__file__).resolve().parents[1]           # candidate root (source tree or extracted release)
INCLUDE_DIRS = ("cerebrobase", "deploy")
INCLUDE_FILES = ("pyproject.toml", "README.md", "CONTRACTS.md", "DEPLOY.md", "DEPENDENCIES.json", "LIMITATIONS.md")
EXCLUDE_PARTS = {"__pycache__", ".git", ".pytest_cache"}
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
BUILD_INFO = "BUILD_INFO.json"


def package_files(root: Path = ROOT) -> list[str]:
    out = []
    for name in INCLUDE_FILES:
        if (root / name).is_file():
            out.append(name)
    for d in INCLUDE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(root)
            if p.is_file() and not (EXCLUDE_PARTS & set(rel.parts)) and not p.name.endswith((".pyc", ".pyo")):
                out.append(rel.as_posix())
    return sorted(out)


def compute_build_id(root: Path = ROOT, label: str = "source") -> str:
    h = hashlib.sha256()
    for rel in package_files(root):
        h.update(rel.encode("utf-8") + b"\0")
        h.update((root / rel).read_bytes())
        h.update(b"\0")
    h.update(b"label:" + label.encode("utf-8"))
    return h.hexdigest()[:16]


def runtime_build(root: Path = ROOT) -> dict:
    info_path = root / BUILD_INFO
    if info_path.is_file():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        actual = compute_build_id(root, info.get("label", ""))
        return {"build_id": actual, "label": info.get("label"), "declared_build_id": info.get("build_id"),
                "integrity": "MATCH" if actual == info.get("build_id") else "BUILD_INTEGRITY_MISMATCH",
                "origin": "ARTIFACT"}
    return {"build_id": compute_build_id(root, "source"), "label": "source", "declared_build_id": None,
            "integrity": "SOURCE_TREE", "origin": "SOURCE_TREE"}


def build_artifact(out_dir: Path, label: str = "release", root: Path = ROOT) -> dict:
    """Deterministic: fixed order, timestamps, permissions, compression; no host paths, time or secrets."""
    if not label or len(label) > 40 or not all(c.isalnum() or c in "._-" for c in label):
        raise ValueError("RELEASE_LABEL_INVALID")
    files = package_files(root)
    build_id = compute_build_id(root, label)
    info = {"build_id": build_id, "label": label, "package": PACKAGE, "version": VERSION,
            "schema_version": SCHEMA_VERSION, "config_schema": CONFIG_SCHEMA}
    info_bytes = (json.dumps(info, indent=1, sort_keys=True) + "\n").encode("utf-8")
    buf = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        entries = [(rel, (root / rel).read_bytes()) for rel in files] + [(BUILD_INFO, info_bytes)]
        for rel, data in sorted(entries):
            zi = zipfile.ZipInfo(f"{PACKAGE}/{rel}", date_time=FIXED_TIME)
            zi.external_attr = (0o100644 & 0xFFFF) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            z.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
            manifest.append({"path": f"{PACKAGE}/{rel}", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    data = buf.getvalue()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PACKAGE}-{VERSION}+{build_id}.zip"
    art = out_dir / name
    if art.exists() and art.read_bytes() != data:
        raise FileExistsError("ARTIFACT_EXISTS_WITH_OTHER_BYTES")
    art.write_bytes(data)
    man = {"schema": "cerebrobase.artifact_manifest/v1", "artifact": name, "artifact_bytes": len(data),
           "artifact_sha256": hashlib.sha256(data).hexdigest(), "build_id": build_id, "label": label,
           "version": VERSION, "schema_version": SCHEMA_VERSION, "config_schema": CONFIG_SCHEMA, "files": manifest}
    (out_dir / f"{name}.manifest.json").write_text(json.dumps(man, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return man


def check_manifest(artifact: Path, manifest_path: Path) -> list[str]:
    """Returns a list of problems (empty == PASS)."""
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = artifact.read_bytes()
    problems = []
    if hashlib.sha256(data).hexdigest() != man["artifact_sha256"] or len(data) != man["artifact_bytes"]:
        problems.append("ARTIFACT_HASH_MISMATCH")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return problems + ["ZIP_UNREADABLE"]
    with z:
        names = z.namelist()
        if len(names) != len(set(names)):
            problems.append("DUPLICATE_ENTRY")
        listed = {f["path"]: f for f in man["files"]}
        if set(names) != set(listed):
            problems.append("ENTRY_SET_MISMATCH")
        for n in names:
            if n.startswith("/") or ".." in n.split("/") or "\\" in n:
                problems.append("UNSAFE_ENTRY:" + n)
            f = listed.get(n)
            if f is not None:
                try:
                    b = z.read(n)
                except (zipfile.BadZipFile, zlib.error, OSError, ValueError, EOFError):
                    problems.append("ENTRY_UNREADABLE:" + n)
                    continue
                if len(b) != f["bytes"] or hashlib.sha256(b).hexdigest() != f["sha256"]:
                    problems.append("FILE_HASH_MISMATCH:" + n)
    return problems


def install_artifact(artifact: Path, manifest_path: Path, releases_root: Path) -> Path:
    """Extract into releases_root/<build_id>/ (must not exist) after manifest verification."""
    problems = check_manifest(artifact, manifest_path)
    if problems:
        raise ValueError("MANIFEST_CHECK_FAILED:" + ",".join(problems))
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    dest = releases_root / man["build_id"]
    if dest.exists():
        raise FileExistsError("RELEASE_DIR_EXISTS")
    tmp = releases_root / f".{man['build_id']}.partial"
    if tmp.exists():
        raise FileExistsError("PARTIAL_RELEASE_DIR_EXISTS")
    with zipfile.ZipFile(artifact) as z:
        for n in z.namelist():
            rel = n.split("/", 1)[1]
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(n))
    os.replace(tmp, dest)
    return dest
