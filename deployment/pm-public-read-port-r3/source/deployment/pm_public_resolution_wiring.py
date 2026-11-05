"""UNDEPLOYED read-only PM port composition, explicit future activation only."""
from __future__ import annotations

from pathlib import Path
import subprocess

from control_resolution_host import BoundRuntimeCapabilityResolver
import control_resolution

SOURCE_COMMIT = "b1aafd7cee9385227dd757a64884df2f816296d4"
SOURCE_ROOT = Path("/opt/cerebro-pm-source") / SOURCE_COMMIT


class NoOwnerEffectPersistence:
    """No owner effect is executable or accepted through this reading port."""
    def verify(self, *, receipt):
        raise RuntimeError("PM_PUBLIC_READ_PORT_HAS_NO_OWNER_EFFECT_PERSISTENCE")


def verify_material(root):
    root = Path(root)
    if root.resolve(strict=True) != SOURCE_ROOT or not (root / ".git").exists():
        raise RuntimeError("PM_PUBLIC_EXACT_SOURCE_MATERIAL_LOCATION_REQUIRED")
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    if git("rev-parse", "HEAD") != SOURCE_COMMIT:
        raise RuntimeError("PM_PUBLIC_SOURCE_COMMIT_MISMATCH")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("PM_PUBLIC_SOURCE_MATERIAL_NOT_CLEAN")
    basis = control_resolution.verify_promotion_basis(root, require_git_ancestry=True)
    if basis.get("promotion_basis_verified") is not True:
        raise RuntimeError("PM_PUBLIC_ACTUAL_PROMOTION_BASIS_REQUIRED")
    return {"source_commit": SOURCE_COMMIT, "promotion_basis": basis}


def bind_public_pm_resolution_if_enabled(runtime, attestor, *, enabled):
    if enabled == "":
        return {"state": "DISABLED", "reason": "EXPLICIT_INSTALLATION_SELECTION_REQUIRED"}
    if enabled != "1":
        raise RuntimeError("PM_PUBLIC_ENABLE_VALUE_INVALID")
    material = verify_material(SOURCE_ROOT)
    runtime.bind_public_pm_control_resolution(
        persistence_verifier=NoOwnerEffectPersistence(),
        capability_resolver=BoundRuntimeCapabilityResolver(executors={}, enabled_owners=set()),
        resolution_attestor=attestor, source_root=SOURCE_ROOT,
    )
    return {"state": "READ_ONLY_PM_PORT_BOUND", **material}
