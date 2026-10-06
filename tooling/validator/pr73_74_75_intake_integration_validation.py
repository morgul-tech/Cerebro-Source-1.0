"""One focused boundary check for the PR73 capture + PR75 transport composition."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / "tooling/return-bridge/rom_a_dispatch_capture.py"
PUMP = ROOT / "tooling/return-bridge/Cerebro.ReturnBridgePump.ps1"
spec = importlib.util.spec_from_file_location("rom_a_dispatch_capture_integration", CAPTURE)
assert spec is not None and spec.loader is not None
capture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture_module)


def powershell() -> str:
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("POWERSHELL_NOT_FOUND")


def fields(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def run_pump(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PUMP), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"pump failed: {result.stdout[-2000:]} {result.stderr[-2000:]}")
    return result


class CaptureTransportComposition(unittest.TestCase):
    def test_captured_receipt_retrieves_by_hash_without_authority_promotion(self):
        with tempfile.TemporaryDirectory(prefix="pr73-pr75-integration-") as temp:
            root = Path(temp)
            request = {
                "schema": capture_module.SCHEMA,
                "task": {
                    "task_ref": "SYN-INTEGRATION-TASK",
                    "task_revision": "rev-1",
                    "actor_ref": "SYN-ACTOR",
                    "generation_ref": "SYN-GENERATION",
                    "carrier_ref": "SYN-CARRIER",
                    "arc_ref": "SYN-ARC",
                    "source_head": "a" * 40,
                    "effect_class": "NONE",
                    "privacy_class": "SYNTHETIC",
                    "live_scope": "LOCAL",
                    "authority_class": "NONE",
                    "return_target": "SYN-OWNER",
                    "way_home": "SYN-OWNER-RETURN",
                    "allowed_paths": ["example.py"],
                    "required_invariants": ["capture remains evidence only"],
                    "stop_edges": ["identity changed"],
                },
            }
            original = "dispatch\r\nexact bytes\n".encode("utf-8")
            captured = capture_module.capture(root / "capture", request, original)
            receipt_path = Path(captured["capture_path"])
            receipt_bytes = receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes)
            self.assertEqual(receipt["authority"], "NONE")
            self.assertFalse(receipt["recipient_read_or_use_proven"])
            self.assertEqual(
                receipt["publish_durability"],
                "LOCAL_READBACK_ONLY_CRASH_DURABILITY_UNPROVEN",
            )

            outbox, drive = root / "outbox", root / "drive"
            drive.mkdir()
            enqueued = run_pump(
                "-Mode", "Enqueue", "-OutboxRoot", str(outbox),
                "-AttemptId", "SYN-PR73-75", "-PatchId", "SYN-INTAKE",
                "-ClaimId", "SYN-NONE", "-Result", "PASS",
                "-SourceBefore", "0" * 40, "-SourceAfter", "1" * 40,
                "-ProductSha256", hashlib.sha256(receipt_bytes).hexdigest(),
                "-ReachedStage", "VALIDATION",
                "-SourceMutationAssessment", "NO_UNCOMMITTED_SOURCE_MUTATION_PRESENT",
                "-ArtifactPaths", str(receipt_path), "-CerebroSyncVerified",
            )
            enqueued_fields = fields(enqueued.stdout)
            package_ref = enqueued_fields["RETURN_BRIDGE_ENVELOPE"]
            run_pump("-Mode", "Drain", "-OutboxRoot", str(outbox), "-DriveReturnRoot", str(drive))

            current = json.loads(
                run_pump("-Mode", "VerifyCurrent", "-DriveReturnRoot", str(drive)).stdout
            )
            package = drive / package_ref
            artifact = next(path for path in package.iterdir() if path.name.startswith("01-"))
            self.assertEqual(current["Result"], "PASS")
            self.assertEqual(current["Authority"], "NONE")
            self.assertEqual(artifact.read_bytes(), receipt_bytes)
            self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(),
                             hashlib.sha256(receipt_bytes).hexdigest())
            self.assertEqual(receipt["authority"], "NONE")
            self.assertFalse(receipt["recipient_read_or_use_proven"])


if __name__ == "__main__":
    unittest.main()
