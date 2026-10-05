"""Focused offline checks for the owner-visible V0.2 repair; no DB or provider calls."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, os.path.join(HERE, "v01_regression"))

from controlled_effect_executor_durable.executor_pg import BoundedRecoverySweep  # noqa: E402
from controlled_effect_executor_durable.settlement import (  # noqa: E402
    PENDING, SETTLED_ABSENT, SettlementEvidence, classify_durable,
)
from controlled_effect_executor_durable.store_pg import PgAdmissionStore, RecoveryPage  # noqa: E402
import run_v01_regression as regression  # noqa: E402


class FakeUnresolvedStore:
    def __init__(self):
        self.visible = {2: "ADMISSION-2", 3: "ADMISSION-3"}
        self.calls = []

    def unresolved_high_water(self, *, work_order_ref=None):
        return max(self.visible, default=0)

    def list_unresolved(self, *, after_cursor=None, limit=100, work_order_ref=None, through_seq=None):
        self.calls.append((after_cursor, limit, work_order_ref, through_seq))
        after = int(after_cursor) if after_cursor is not None else 0
        rows = [SimpleNamespace(admit_seq=seq, admission_ref=ref)
                for seq, ref in sorted(self.visible.items())
                if seq > after and (through_seq is None or seq <= through_seq)][:limit]
        return RecoveryPage(tuple(rows), str(rows[-1].admit_seq) if len(rows) == limit else None)


class OfflineCorrections(unittest.TestCase):
    def test_high_water_and_bounded_sql_are_read_only_and_parameterized(self):
        class Cursor:
            def __init__(self):
                self.sql = ""
                self.params = []

            def execute(self, sql, params):
                self.sql, self.params = sql, params

            def fetchone(self):
                return {"high_water": 3}

            def fetchall(self):
                return []

        class Store:
            _s = "synthetic_schema"
            _scope = "SCOPE-1"

            def __init__(self):
                self.cursor = Cursor()
                self.read_only = None

            def _tx(self, body, *, read_only=False):
                self.read_only = read_only
                return body(self.cursor)

        store = Store()
        self.assertEqual(PgAdmissionStore.unresolved_high_water(store, work_order_ref="WO-1"), 3)
        self.assertTrue(store.read_only)
        self.assertIn("MAX(p.admit_seq)", store.cursor.sql)
        self.assertEqual(store.cursor.params, ["SCOPE-1", "WO-1"])
        page = PgAdmissionStore.list_unresolved(
            store, after_cursor="2", limit=1, work_order_ref="WO-1", through_seq=3)
        self.assertEqual(page.items, ())
        self.assertTrue(store.read_only)
        self.assertIn("p.admit_seq <= %s", store.cursor.sql)
        self.assertEqual(store.cursor.params, ["SCOPE-1", 2, 3, "WO-1", 1])

    def test_late_low_sequence_appears_on_the_next_fresh_sweep(self):
        store = FakeUnresolvedStore()
        sweep = BoundedRecoverySweep(store, limit=1, work_order_ref="TASK-1")
        self.assertEqual([x.admission_ref for x in sweep.next_page().items], ["ADMISSION-2"])
        store.visible[1] = "LATE-COMMIT-1"  # admitted earlier, committed after cursor 2 was read
        store.visible[4] = "NEW-HIGH-SEQUENCE-4"  # cannot keep this sweep open forever
        self.assertEqual([x.admission_ref for x in sweep.next_page().items], ["ADMISSION-3"])
        self.assertEqual(sweep.next_page().items, ())  # tail resets the cursor
        self.assertEqual(sweep.completed_sweeps, 1)
        self.assertEqual([x.admission_ref for x in sweep.next_page().items], ["LATE-COMMIT-1"])
        self.assertEqual(store.calls[-1], (None, 1, "TASK-1", 4))

    def test_new_scanner_after_process_restart_starts_at_head_and_bounds_pages(self):
        store = FakeUnresolvedStore()
        self.assertEqual(BoundedRecoverySweep(store, limit=2).next_page().items[0].admit_seq, 2)
        store.visible[1] = "LATE-COMMIT-1"
        self.assertEqual(BoundedRecoverySweep(store, limit=2).next_page().items[0].admit_seq, 1)
        for bad in (0, 501, True, "2"):
            with self.assertRaises(ValueError):
                BoundedRecoverySweep(store, limit=bad)

    def test_empty_history_needs_authenticated_settled_absent(self):
        with patch("controlled_effect_executor_durable.settlement.classify_observation",
                   return_value=("NO_COMMIT", "READBACK_PROVES_NO_COMMIT")):
            args = ("CORR-1", object(), object())
            self.assertEqual(classify_durable(*args, None, ack_seen=False)[0], "INDETERMINATE")
            unsigned = SettlementEvidence("CORR-1", SETTLED_ABSENT, True, "SETTLEMENT-1")
            self.assertEqual(classify_durable(*args, unsigned, ack_seen=False),
                             ("INDETERMINATE", "SETTLEMENT_NOT_AUTHENTICATED"))
            no_ref = SettlementEvidence("CORR-1", SETTLED_ABSENT, True, None, authenticated=True)
            self.assertEqual(classify_durable(*args, no_ref, ack_seen=False)[0], "INDETERMINATE")
            pending = SettlementEvidence("CORR-1", PENDING, True, "PENDING-1", authenticated=True)
            self.assertEqual(classify_durable(*args, pending, ack_seen=False)[0], "INDETERMINATE")
            settled = SettlementEvidence("CORR-1", SETTLED_ABSENT, True, "SETTLEMENT-1", authenticated=True)
            self.assertEqual(classify_durable(*args, settled, ack_seen=False),
                             ("NO_COMMIT", "READBACK_PROVES_NO_COMMIT"))

    def test_installed_regression_wrapper_propagates_inner_failure(self):
        probe = subprocess.CompletedProcess([], 0, stdout="synthetic-installed-package\n", stderr="")
        failed = subprocess.CompletedProcess([], 1, stdout="FAIL source-only\n", stderr="")
        with patch.object(sys, "argv", ["run_v01_regression.py", "--mode", "installed"]), \
             patch.object(regression.subprocess, "run", side_effect=[probe, failed]):
            self.assertEqual(regression.main(), 1)


if __name__ == "__main__":
    unittest.main()
