"""G. private-staging install/upgrade/rollback rehearsal on synthetic isolated local roots (deploy/rehearse_staging.py).
Actual EDGE install is UNRUN and never inferred from this."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import ROOT


class G_Rehearsal(unittest.TestCase):
    def test_rehearsal_install_upgrade_rollback_restore(self):
        d = Path(tempfile.mkdtemp(prefix="cb-reh-"))
        self.addCleanup(shutil.rmtree, d, True)
        p = subprocess.run([sys.executable, "-B", str(ROOT / "deploy" / "rehearse_staging.py"), "--source", str(ROOT),
                            "--work", str(d / "w")], capture_output=True, text=True, timeout=600)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        rec = json.loads((d / "w" / "rehearsal.json").read_text())
        self.assertEqual((rec["result"], rec["public_main_unchanged"], rec["edge_install"]),
                         ("PASS", True, "UNRUN_NOT_INFERRED"))
        steps = {s["step"]: s for s in rec["steps"]}
        r1, r2 = steps["build_install"]["r1"], steps["build_install"]["r2"]
        self.assertNotEqual(r1, r2)
        self.assertEqual(steps["r1_start_readback_stop"]["build_id"], r1)
        self.assertEqual(steps["r2_upgrade_readback_stop"]["build_id"], r2)
        self.assertEqual(steps["rollback_switch_back_compatible_schema"]["build_id"], r1)
        self.assertEqual(steps["rollback_restore_isolated_root"]["rooms"],
                         ["room-fixture-andreas-admin", "room-fixture-marianne-pilot"])
        self.assertEqual(steps["staging_into_prod_refused"]["codes"], ["STAGING_POINTS_INTO_PRODUCTION"])


if __name__ == "__main__":
    unittest.main()
