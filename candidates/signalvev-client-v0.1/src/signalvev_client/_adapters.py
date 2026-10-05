"""One canonical implementation of the two existing Signalvev adapters, reachable from both routes.

Installed distribution: the wheel bundles the byte-identical adapter files as package ``signalvev_adapters`` and
pins them in RESOURCE_MANIFEST.json; ``_bootstrap`` verifies those bytes before anything is imported.
Source checkout: the same module names are bound to candidates/signalvev-sensing-runtime-v0.1/adapters/*.py.
The installed route never falls back to a source path, and there is no edited copy anywhere.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from . import _bootstrap

PACKAGE = "signalvev_adapters"
SOURCE_DIR = Path(__file__).resolve().parents[3] / "signalvev-sensing-runtime-v0.1" / "adapters"


def _bind_source_package() -> None:
    if PACKAGE in sys.modules:
        return
    if not (SOURCE_DIR / "pm_owner_commit.py").is_file():
        raise ImportError("ADAPTERS_NOT_FOUND: neither the installed bundle nor the source checkout provides them")
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(SOURCE_DIR)]          # namespace-style package over the unchanged source directory
    pkg.__doc__ = "Signalvev adapters bound from the source checkout (development route)."
    sys.modules[PACKAGE] = pkg


def bind() -> None:
    """Make the ``signalvev_adapters.<name>`` modules importable: a no-op for the installed bundle (its package is real and
    pinned); in a source checkout the package name is bound to the unchanged source directory."""
    installed = _bootstrap.BOOTSTRAP_RESULT.get("mode") == "INSTALLED_BUNDLE"
    if not installed:
        _bind_source_package()
        # PR52 imports adapters.x9_channel_ingress. Bind both names to one
        # module so exact dataclass checks never see a second implementation.
        existing = sys.modules.get("adapters")
        if existing is None:
            pkg = types.ModuleType("adapters")
            pkg.__path__ = [str(SOURCE_DIR)]
            sys.modules["adapters"] = pkg
        elif not any(Path(p).resolve() == SOURCE_DIR.resolve()
                     for p in getattr(existing, "__path__", ())):
            raise ImportError("ADAPTER_NAMESPACE_CONFLICT")
    else:
        # Installed providers use the pinned bundle, never a source fallback.
        import signalvev_adapters as pkg
        existing = sys.modules.get("adapters")
        if existing is not None and existing is not pkg:
            raise ImportError("ADAPTER_NAMESPACE_CONFLICT")
        sys.modules["adapters"] = pkg
    if installed:
        from signalvev_adapters import pm_owner_commit, x9_channel_ingress
    else:
        from adapters import pm_owner_commit, x9_channel_ingress
    for name, module in (("pm_owner_commit", pm_owner_commit),
                         ("x9_channel_ingress", x9_channel_ingress)):
        for alias in (f"adapters.{name}", f"{PACKAGE}.{name}"):
            if alias in sys.modules and sys.modules[alias] is not module:
                raise ImportError("ADAPTER_MODULE_CONFLICT")
            sys.modules[alias] = module


bind()


def origin() -> str:
    return "INSTALLED_BUNDLE" if _bootstrap.BOOTSTRAP_RESULT.get("mode") == "INSTALLED_BUNDLE" else "SOURCE_TREE"
