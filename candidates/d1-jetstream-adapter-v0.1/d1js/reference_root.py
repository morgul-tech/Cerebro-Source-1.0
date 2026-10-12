"""Explicit reference-root import of the historical gateway (no copy, no reimplementation of its decision logic).

The caller passes the directory that holds the exact historical bytes of
candidates/d1-hybrid-gateway-v0.1 (commit cbbc315b476cdaa7965c46671c5133bb2f70cd18). Every file is checked against
its pinned SHA-256 before ``gateway.py`` is executed under a private module name. A mismatch fails closed.
The Project return/fence reference (commit a8f1780f62ee2cf91354af0156318610880a9476) is only hash-checked: its
PostgreSQL code is NOT imported here; ``stores.SyntheticReturnLedger`` maps its append/read_exact semantics.

Future internal action (not done here): Source integration chooses how the pinned gateway enters the tree (e.g. a
reviewed import of exactly these bytes at a chosen path); this adapter then binds that path instead of a reference root.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

GATEWAY_COMMIT = "cbbc315b476cdaa7965c46671c5133bb2f70cd18"
GATEWAY_PINS = {
    "gateway.py": "705de6f6509a298594b62ccee18f9161c91f121027ea85f09ba77a25b5635e51",
    "README.md": "bac54810b166ceb8950786362434b72f4fa6b9b2151ec5fe669ce1603512190a",
    "component.yaml": "70470849ed138684e62097938e79bd899fd3a2edda644f24f860965111c96a1a",
    "test_gateway.py": "2d54a1acc91f32f54c961d4e4b22288339e334a34297f8039598f1bebe1af3c3",
}
PROJECT_RETURN_COMMIT = "a8f1780f62ee2cf91354af0156318610880a9476"
PROJECT_RETURN_PINS = {
    "tooling/owner_state/project_d1_closure_return.py": "ec4d985732a639a33258ec4bc2c8211cd2c7e50f3af331c0ddd0ab4c55465a9d",
    "tooling/owner_state/project_d1_closure_ledger_candidate.sql": "611e2e4ecde8b256feb9f786995c7211ab7fefb2d90241fe6dae93e92acfe950",
}
MODULE_NAME = "d1js_reference_gateway_cbbc315b"


class ReferenceRootError(ImportError):
    """A reference root is missing, or its bytes are not the pinned historical bytes."""


def _check(root: Path, pins: dict[str, str]) -> dict[str, str]:
    root = Path(root)
    if not root.is_dir():
        raise ReferenceRootError(f"REFERENCE_ROOT_MISSING: {root}")
    seen = {}
    for rel, digest in pins.items():
        path = root / rel
        if not path.is_file():
            raise ReferenceRootError(f"REFERENCE_FILE_MISSING: {rel}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ReferenceRootError(f"REFERENCE_HASH_MISMATCH: {rel} {actual}")
        seen[rel] = actual
    return seen


def load_gateway(root: Path | str) -> ModuleType:
    """Import the pinned historical gateway module from ``root`` (idempotent, fail-closed)."""
    root = Path(root).resolve()
    _check(root, GATEWAY_PINS)
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        if Path(existing.__file__).resolve() != (root / "gateway.py"):
            raise ReferenceRootError("GATEWAY_ALREADY_BOUND_TO_ANOTHER_ROOT")
        return existing
    spec = importlib.util.spec_from_file_location(MODULE_NAME, root / "gateway.py")
    if spec is None or spec.loader is None:
        raise ReferenceRootError("GATEWAY_SPEC_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(MODULE_NAME, None)
        raise
    return module


def check_project_return_root(root: Path | str) -> dict[str, str]:
    """Hash-check the read-only Project return/fence reference that the synthetic ledger mirrors."""
    return _check(Path(root).resolve(), PROJECT_RETURN_PINS)
