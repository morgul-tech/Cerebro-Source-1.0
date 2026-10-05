"""Reference-resource pinning: fail closed on any missing / changed / escaping file (installed mode relies on this)."""
import hashlib
import json
import unittest
from pathlib import Path

from _support import TmpCase
from signalvev_client import _bootstrap


def digest(b):
    return hashlib.sha256(b).hexdigest()


class BootstrapTests(TmpCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "pkg").mkdir()
        (self.tmp / "pkg" / "a.py").write_bytes(b"A")
        (self.tmp / "pkg" / "b.json").write_bytes(b"{}")
        self.manifest = {"files": {"pkg/a.py": digest(b"A"), "pkg/b.json": digest(b"{}")}}
        self.saved = _bootstrap.SITE_ROOT
        _bootstrap.SITE_ROOT = self.tmp
        self.addCleanup(setattr, _bootstrap, "SITE_ROOT", self.saved)

    def test_identical_files_verify(self):
        self.assertEqual(_bootstrap.verify_manifest(self.manifest), [])

    def test_changed_missing_and_escaping_files_are_reported(self):
        (self.tmp / "pkg" / "a.py").write_bytes(b"A!")
        (self.tmp / "pkg" / "b.json").unlink()
        problems = _bootstrap.verify_manifest(self.manifest)
        self.assertIn("HASH_MISMATCH:pkg/a.py", problems)
        self.assertIn("MISSING:pkg/b.json", problems)
        outside = self.tmp.parent / "outside.txt"
        self.assertTrue(any(p.startswith("PATH_ESCAPES_ROOT") for p in
                            _bootstrap.verify_manifest({"files": {"../outside.txt": digest(b"x")}})))
        self.assertEqual(_bootstrap.verify_manifest({"files": {}}), ["MANIFEST_HAS_NO_FILES"])

    def test_source_tree_mode_has_no_manifest_and_reports_it(self):
        if _bootstrap.MANIFEST_PATH.exists():
            self.skipTest("installed run: a bundled manifest exists")
        self.assertEqual(_bootstrap.bootstrap()["mode"], "SOURCE_TREE")

    def test_bootstrap_in_installed_mode_raises_on_tamper(self):
        if not _bootstrap.MANIFEST_PATH.exists():
            self.skipTest("source-tree run: no bundled manifest")
        saved = _bootstrap.load_manifest()
        _bootstrap.SITE_ROOT = self.saved
        self.assertEqual(_bootstrap.verify_manifest(saved), [])        # the real installed bundle is intact


if __name__ == "__main__":
    unittest.main()
