"""Reference-resource bootstrap. The ONLY module of this package that reads or writes os.environ.

The Signalvev core (`signalvev_sensing`, unchanged) imports the v0.1/v0.9/v0.15/v0.16 reference modules through
its own bridge, which honours SIGNALVEV_VALIDATOR_DIR. In a source checkout that bridge finds
`tooling/validator` by itself. In an installed distribution the byte-identical reference files are bundled under
`_reference_resources/` (mirroring the repo layout, so v0.1 still finds its registry relative to itself) and are
pinned by RESOURCE_MANIFEST.json. This module points the bridge there and verifies every pinned file BEFORE
the core is imported. A mismatch fails closed: nothing is imported from bytes that are not the pinned ones.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = PKG_DIR / "_reference_resources"
MANIFEST_PATH = RESOURCES_DIR / "RESOURCE_MANIFEST.json"
SITE_ROOT = PKG_DIR.parent          # manifest paths are relative to the directory that holds both packages
_ENV = "SIGNALVEV_VALIDATOR_DIR"


class ReferenceIntegrityError(ImportError):
    """A bundled reference/core file is missing or is not the pinned byte sequence."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest() -> dict | None:
    if not MANIFEST_PATH.is_file():
        return None
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def verify_manifest(manifest: dict) -> list[str]:
    """Return a list of problems (empty = every pinned file present and byte-identical)."""
    problems: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return ["MANIFEST_HAS_NO_FILES"]
    for rel, expected in sorted(files.items()):
        target = (SITE_ROOT / rel).resolve()
        if SITE_ROOT.resolve() not in target.parents:
            problems.append(f"PATH_ESCAPES_ROOT:{rel}")
        elif not target.is_file():
            problems.append(f"MISSING:{rel}")
        elif _sha256(target) != expected:
            problems.append(f"HASH_MISMATCH:{rel}")
    return problems


def bootstrap() -> dict:
    """Idempotent. Returns {'mode': 'INSTALLED_BUNDLE'|'SOURCE_TREE', ...}. Raises ReferenceIntegrityError."""
    manifest = load_manifest()
    if manifest is None:
        return {"mode": "SOURCE_TREE", "validator_dir": os.environ.get(_ENV), "pinned_files": 0}
    problems = verify_manifest(manifest)
    if problems:
        raise ReferenceIntegrityError("bundled reference/core files are not the pinned bytes: " + ", ".join(problems[:5]))
    validator_dir = RESOURCES_DIR / "tooling" / "validator"
    already = sys.modules.get("signalvev_sensing._reference")
    if already is not None and Path(already.VALIDATOR_DIR).resolve() != validator_dir.resolve():
        raise ReferenceIntegrityError("signalvev_sensing was imported before the bundled references were bound")
    os.environ[_ENV] = str(validator_dir)
    return {"mode": "INSTALLED_BUNDLE", "validator_dir": str(validator_dir), "pinned_files": len(manifest["files"]),
            "source_commit": manifest.get("source_commit")}


BOOTSTRAP_RESULT = bootstrap()
