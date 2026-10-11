"""CAP26 focused acceptance tests A-F, H for the standalone slice resolver + LocalPinnedSnapshotProvider."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from context_slice_support import (EXPECTED_SLICE, FIX, FULL_SHA, ORIGINAL, SECRET, SLICE_SHA, FakeClock,
                                   ScriptedProvider, TmpRoot, binding, request, sha)
from signalvev_sensing.context_slice import (DENIED, FALLBACK_REQUIRED, FULL_ORIGINAL_READY, HASH_MISMATCH,
                                             IDENTITY_MISMATCH, LIMIT_EXCEEDED, SELECTOR_DRIFT, SLICE_READY, STALE,
                                             UNREADABLE, ContextSliceResolver, RequestInvalid, parse_request)
from signalvev_sensing.context_slice_provider import LocalPinnedSnapshotProvider

SRC = Path(__file__).resolve().parents[1] / "src"


def no_content(test, res):
    test.assertIsNone(res.slice_text)
    test.assertIsNone(res.slice_sha256)
    test.assertNotIn(b"row 00", res.to_json())          # no fixture body bytes leak into any failure envelope


class A_ExactSlice(TmpRoot):
    def test_a_supplied_fixture_exact_216_bytes_and_idempotent_without_writes(self):
        before = {p.name: (p.stat().st_mtime_ns, sha(p.read_bytes())) for p in FIX.iterdir()}
        provider = LocalPinnedSnapshotProvider(FIX, FIX / "registry.json")
        req = parse_request((FIX / "request.json").read_bytes())
        r1 = ContextSliceResolver(provider).resolve(req)
        r2 = ContextSliceResolver(provider).resolve(req)
        self.assertEqual(r1.outcome, SLICE_READY, r1.reason)
        self.assertEqual(r1.slice_text.encode("utf-8"), EXPECTED_SLICE)            # exact bytes, no normalization
        self.assertEqual((r1.slice_bytes, r1.slice_sha256), (216, SLICE_SHA))
        self.assertEqual(r1.verified_content_sha256, FULL_SHA)
        self.assertNotEqual(r1.slice_sha256, r1.verified_content_sha256)        # separate digest domains
        self.assertEqual(r1.selector, {"kind": "UTF8_BYTE_RANGE", "start": 774, "end": 990,
                                       "expected_slice_sha256": SLICE_SHA})
        self.assertEqual((r1.way_home, r1.observed_revision, r1.owner_seq), ("fixture:canonical-home", "r1", 1))
        self.assertEqual(r1.provenance["assertion"], "SYNTHETIC_OWNER_ASSERTION")
        self.assertEqual(r1.currentness["semantic_currentness"], "UNKNOWN")      # a hash match is never CURRENT
        self.assertEqual(r1.authority, "NONE")
        self.assertLessEqual(len(r1.to_json()), 4096)
        d1, d2 = r1.to_dict(), r2.to_dict()
        d1["counters"].pop("elapsed_ms"), d2["counters"].pop("elapsed_ms")
        self.assertEqual(d1, d2)                                                 # repeated call: same identity
        after = {p.name: (p.stat().st_mtime_ns, sha(p.read_bytes())) for p in FIX.iterdir()}
        self.assertEqual(before, after)                                          # no source writes

    def test_a_multibyte_boundaries_deterministic(self):
        i = ORIGINAL.index("â€”".encode())                                       # e2 80 94
        ok = self.resolver().resolve(
            request(selector={"start": i, "end": i + 3, "expected_slice_sha256": sha(ORIGINAL[i:i + 3])}))
        self.assertEqual((ok.outcome, ok.slice_text), (SLICE_READY, "â€”"))
        for lo, hi in ((i, i + 1), (i + 1, i + 3), (i - 2, i + 2)):              # split sequences, hash pinned
            res = self.resolver().resolve(
                request(selector={"start": lo, "end": hi, "expected_slice_sha256": sha(ORIGINAL[lo:hi])}))
            self.assertEqual((res.outcome, res.reason), (SELECTOR_DRIFT, "SELECTOR_SPLITS_UTF8_SEQUENCE"))
            no_content(self, res)

    def test_a_crlf_and_bytes_not_normalized(self):
        data = b"head\r\nline\rtail\n\xc3\xa6\r\n"
        self.add("crlf.txt", data, content_ref="fixture:crlf")
        res = self.resolver().resolve(request(
            content_ref="fixture:crlf", expected_content_sha256=sha(data),
            selector={"start": 4, "end": len(data), "expected_slice_sha256": sha(data[4:])}))
        self.assertEqual(res.outcome, SLICE_READY, res.reason)
        self.assertEqual(res.slice_text.encode("utf-8"), data[4:])


class B_CurrentnessAndIntegrity(TmpRoot):
    def resolve(self, **over):
        return self.resolver().resolve(request(**over))

    def test_b_stale_superseded_revision_no_slice_no_open(self):
        self.bindings[0].update(revision="r2", superseded_revisions=["r1"])
        self.write_registry()
        res = self.resolve()
        self.assertEqual((res.outcome, res.reason), (STALE, "REVISION_SUPERSEDED_BY_OWNER"))
        self.assertEqual((res.currentness["head_relation"], self.provider.stats.content_opens), ("SUPERSEDED", 0))
        no_content(self, res)

    def test_b_revision_not_served_is_stale_unknown_never_ordered(self):
        for rev in ("r0", "r10", "r1-old"):                      # neither lexical nor numeric ordering is used
            res = self.resolve(expected_revision=rev)
            self.assertEqual((res.outcome, res.currentness["head_relation"]), (STALE, "UNKNOWN"))
            no_content(self, res)
        self.assertEqual(self.provider.stats.content_opens, 0)

    def test_b_unknown_head_relation_fails_closed_unless_explicit_pinned_only(self):
        self.bindings[0].update(head_relation="UNKNOWN")
        self.write_registry()
        res = self.resolve()
        self.assertEqual((res.outcome, res.reason), (STALE, "HEAD_RELATION_UNKNOWN_FAIL_CLOSED"))
        no_content(self, res)
        pinned = self.resolve(allow_unknown_head_relation=True)
        self.assertEqual(pinned.outcome, SLICE_READY)
        self.assertEqual((pinned.currentness["head_relation"], pinned.currentness["receiver_acceptable"]),
                         ("UNKNOWN", False))

    def test_b_wrong_full_hash_and_wrong_slice_hash(self):
        res = self.resolve(expected_content_sha256="0" * 64)
        self.assertEqual((res.outcome, self.provider.stats.content_opens), (HASH_MISMATCH, 0))
        no_content(self, res)
        res = self.resolve(selector={"expected_slice_sha256": "1" * 64})
        self.assertEqual((res.outcome, res.reason), (SELECTOR_DRIFT, "SLICE_SHA256_DRIFT"))
        no_content(self, res)

    def test_b_source_bytes_altered_in_place_after_descriptor_metadata_kept(self):
        target = self.root / "original.txt"
        st = os.stat(target)

        def tamper(_path):                       # same size, same inode, mtime restored: metadata says "unchanged"
            with open(target, "r+b") as fh:
                fh.seek(800)
                fh.write(b"X")
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.provider.on("before_open", tamper)
        res = self.resolve()
        self.assertEqual((res.outcome, res.reason), (HASH_MISMATCH, "VERIFIED_FULL_SHA256_DIFFERS"))
        self.assertEqual(res.counters.bytes_read, len(ORIGINAL))
        no_content(self, res)

    def test_b_registry_changed_mid_read_no_slice(self):
        cases = [(dict(head_relation="UNKNOWN"), STALE), (dict(authorized_subjects=["actor:other"]), DENIED),
                 (dict(owner_seq=2), STALE)]
        for change, outcome in cases:
            self.bindings = [binding()]
            self.write_registry()
            fired = []

            def mutate(n, change=change):
                if not fired:
                    fired.append(n)
                    self.write_registry([binding(**change)])
            self.provider.on("after_read", mutate)
            res = self.resolve()
            self.assertEqual(res.outcome, outcome, (change, res.reason))
            no_content(self, res)

    def test_b_file_replaced_mid_read_is_unreadable(self):
        target = self.root / "original.txt"
        fired = []

        def replace(n):
            if not fired:
                fired.append(n)
                tmp = self.root / "new.tmp"
                tmp.write_bytes(ORIGINAL)                        # identical bytes, different file identity
                try:
                    os.replace(tmp, target)
                except PermissionError:
                    fired.append("REPLACEMENT_DENIED")
                    raise
        self.provider.on("after_read", replace)
        res = self.resolve()
        reason = "READ_INTERRUPTED" if "REPLACEMENT_DENIED" in fired else "SNAPSHOT_IDENTITY_CHANGED_DURING_READ"
        self.assertEqual((res.outcome, res.reason), (UNREADABLE, reason))
        no_content(self, res)


class C_AuthorizationAndConfinement(TmpRoot):
    def assert_denied_before_open(self, res, reason=None):
        self.assertEqual(res.outcome, DENIED, res.reason)
        if reason:
            self.assertEqual(res.reason, reason)
        self.assertEqual((self.provider.stats.content_opens, self.provider.stats.read_calls,
                          self.provider.stats.bytes_read, res.counters.content_opens, res.counters.bytes_read),
                         (0, 0, 0, 0, 0))
        self.assertNotIn(SECRET.encode(), res.to_json())
        no_content(self, res)

    def test_c_private_foreign_wrong_subject_unknown_ref_denied_before_any_fs_access(self):
        self.add("private.txt", f"{SECRET}\n".encode() * 40, content_ref="fixture:private",
                 authorized_subjects=["actor:owner-only"])
        priv = request(content_ref="fixture:private", expected_content_sha256=sha(f"{SECRET}\n".encode() * 40))
        cases = [(priv, "SUBJECT_NOT_AUTHORIZED"), (request(owner_ref="owner:foreign"), "FOREIGN_OWNER"),
                 (request(subject_ref="actor:stranger"), "SUBJECT_NOT_AUTHORIZED"),
                 (request(content_ref="fixture:nope"), "UNKNOWN_CONTENT_REF")]
        for req, reason in cases:
            res = self.resolver().resolve(req)
            self.assert_denied_before_open(res, reason)
        self.assertEqual(self.provider.stats.stat_calls, 0)                # authorization precedes the filesystem

    def test_c_path_confinement(self):
        outside = self.dir / "outside.txt"
        outside.write_bytes(ORIGINAL)
        bad = ["/etc/passwd", str(outside), "C:/Windows/win.ini", "C:original.txt", "\\\\server\\share\\x",
               "..\\original.txt", "../outside.txt", "a/../original.txt", "./original.txt", "%2e%2e/outside.txt",
               "original.txt%00", "a//original.txt", "original.txt/", "", ".hidden"]
        for p in bad:
            self.write_registry([binding(path=p)])
            res = self.resolver().resolve(request())
            self.assertEqual(res.outcome, DENIED, p)
            self.assertIn(res.reason, ("PATH_NOT_CONFINED", "PATH_INVALID"), p)
        self.assertEqual(self.provider.stats.content_opens, 0)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unsupported")
    def test_c_symlink_file_and_directory_escape(self):
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "original.txt").write_bytes(ORIGINAL)
        try:
            os.symlink(outside / "original.txt", self.root / "link.txt")
            os.symlink(outside, self.root / "linkdir")
        except OSError:
            self.skipTest("symlink creation not permitted")
        for p in ("link.txt", "linkdir/original.txt"):
            self.write_registry([binding(path=p)])
            res = self.resolver().resolve(request())
            self.assert_denied_before_open(res, "PATH_LINK_OR_REPARSE")

    def test_c_cli_denied_output_has_no_secret(self):
        self.add("private.txt", f"{SECRET}\n".encode(), content_ref="fixture:private",
                 authorized_subjects=["actor:owner-only"])
        req = json.loads((FIX / "request.json").read_bytes())
        req.update(content_ref="fixture:private", expected_content_sha256=sha(f"{SECRET}\n".encode()))
        rp = self.dir / "req.json"
        rp.write_text(json.dumps(req))
        env = dict(os.environ, PYTHONPATH=str(SRC))
        p = subprocess.run([sys.executable, "-B", "-m", "signalvev_sensing.context_slice_cli", "resolve",
                            "--registry", str(self.registry_path), "--root", str(self.root), "--request", str(rp)],
                           capture_output=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["outcome"], DENIED)
        self.assertNotIn(SECRET.encode(), p.stdout + p.stderr)
        self.assertNotIn(str(self.root).encode(), p.stdout + p.stderr)       # no filesystem paths in output


class D_BoundsAndStrictInput(TmpRoot):
    def test_d_oversized_source_rejected_before_open(self):
        res = self.resolver().resolve(request(limits={"max_source_bytes": 1000}))
        self.assertEqual((res.outcome, res.reason, res.counters.content_opens), (LIMIT_EXCEEDED,
                                                                                 "SOURCE_OVER_MAX_SOURCE_BYTES", 0))

    def test_d_lying_size_smaller_and_larger_and_unbounded_chunk(self):
        p = ScriptedProvider(size=1000, chunks=[ORIGINAL[:65536]])
        res = ContextSliceResolver(p).resolve(request())
        self.assertEqual((res.outcome, res.reason), (LIMIT_EXCEEDED, "SOURCE_LONGER_THAN_DECLARED_OR_CAP"))
        self.assertEqual(res.counters.bytes_read, 65536)                       # actual bytes recorded on rejection
        no_content(self, res)
        p = ScriptedProvider(size=len(ORIGINAL) + 10)
        res = ContextSliceResolver(p).resolve(request())
        self.assertEqual((res.outcome, res.reason), (HASH_MISMATCH, "SOURCE_SIZE_DIFFERS_FROM_DESCRIPTOR"))
        p = ScriptedProvider(chunks=[ORIGINAL])                                 # one 86454-byte chunk > 65536
        res = ContextSliceResolver(p).resolve(request())
        self.assertEqual((res.outcome, res.reason), (LIMIT_EXCEEDED, "PROVIDER_CHUNK_OVER_BOUND"))
        self.assertEqual(res.counters.bytes_read, len(ORIGINAL))
        no_content(self, res)

    def test_d_oversized_selector_and_result_metadata_fall_back_without_content(self):
        res = self.resolver().resolve(
            request(selector={"start": 0, "end": 2000, "expected_slice_sha256": sha(ORIGINAL[:2000])}))
        self.assertEqual((res.outcome, res.reason), (FALLBACK_REQUIRED, "SELECTOR_OVER_MAX_SLICE_BYTES"))
        self.assertEqual((res.fallback["canonical_ref"], res.fallback["content_sha256"], res.counters.content_opens),
                         ("fixture:public-report", FULL_SHA, 0))
        no_content(self, res)
        from signalvev_sensing.context_slice import ResultEnvelopeUnavailable
        before = self.provider.stats.content_opens
        with self.assertRaises(ResultEnvelopeUnavailable):
            self.resolver().resolve(request(limits={"max_result_bytes": 1500}))
        self.assertEqual(self.provider.stats.content_opens, before)

    def test_d_selector_end_beyond_source_is_drift(self):
        n = len(ORIGINAL)
        res = self.resolver().resolve(
            request(selector={"start": n - 10, "end": n + 1, "expected_slice_sha256": "2" * 64}))
        self.assertEqual((res.outcome, res.counters.content_opens), (SELECTOR_DRIFT, 0))

    def test_d_strict_request_parsing(self):
        good = json.loads((FIX / "request.json").read_bytes())

        def bad(mutate=None, raw=None):
            doc = json.loads(json.dumps(good))
            if mutate:
                mutate(doc)
            with self.assertRaises(RequestInvalid):
                parse_request(raw if raw is not None else json.dumps(doc).encode())
        bad(raw=b'{"content_ref":"a","content_ref":"b"}')
        bad(raw=b"{not json")
        bad(raw=json.dumps(good).replace("1048576", "NaN").encode())
        bad(lambda d: d.update(extra=1))
        bad(lambda d: d.pop("way_home"))
        bad(lambda d: d.pop("limits"))
        bad(lambda d: d.update(subject_ref=" "))
        bad(lambda d: d.update(expected_content_sha256=FULL_SHA.upper()))
        bad(lambda d: d["selector"].update(start=True))                       # bool-as-int
        bad(lambda d: d["selector"].update(start=1.0))
        bad(lambda d: d["selector"].update(start=10, end=10))
        bad(lambda d: d["selector"].update(start=-1))
        bad(lambda d: d["selector"].update(kind="HEADING"))
        bad(lambda d: d["selector"].update(ranges=[[0, 1]]))
        bad(lambda d: d["limits"].update(max_slice_bytes=2048))                # may only lower
        bad(lambda d: d["limits"].update(max_provider_calls=True))
        bad(lambda d: d["limits"].update(unbounded=1))
        bad(lambda d: d.update(allow_unknown_head_relation=1))
        self.assertEqual(parse_request(json.dumps(good).encode()).selector.end, 990)


class E_UnavailableAndDeadline(TmpRoot):
    def test_e_no_provider_offline_missing_file(self):
        res = ContextSliceResolver(None).resolve(request())
        self.assertEqual((res.outcome, res.reason), (UNREADABLE, "NO_PROVIDER"))
        p = ScriptedProvider(unavailable=True)
        res = ContextSliceResolver(p).resolve(request())
        self.assertEqual((res.outcome, p.describes, p.opens), (UNREADABLE, 1, 0))   # no automatic retry
        (self.root / "original.txt").unlink()
        res = self.resolver().resolve(request())
        self.assertEqual((res.outcome, res.reason, self.provider.stats.content_opens),
                         (UNREADABLE, "CONTENT_MISSING", 0))
        self.assertEqual(res.request["content_ref"], "fixture:public-report")  # obligation/ref stays inspectable

    def test_e_interrupted_read_no_retry(self):
        p = ScriptedProvider(fail_at=1)
        res = ContextSliceResolver(p).resolve(request())
        self.assertEqual((res.outcome, res.reason, p.opens, res.counters.retries), (UNREADABLE, "READ_INTERRUPTED",
                                                                                   1, 0))
        no_content(self, res)

    def test_e_deadline_before_open_and_after_last_read(self):
        clock = FakeClock()
        p = ScriptedProvider(clock=clock, advance_on_open=6.0)
        res = ContextSliceResolver(p, clock=clock).resolve(request())
        self.assertEqual(res.outcome, UNREADABLE)
        self.assertIn("DEADLINE", res.reason)
        no_content(self, res)
        fired = []

        def late(n):                                           # the final (empty) read returns after the deadline
            if n == 0 and not fired:
                fired.append(1)
                self.clock.advance(6.0)
        self.provider.on("after_read", late)
        res = self.resolver().resolve(request())
        self.assertEqual((res.outcome, res.reason), (UNREADABLE, "DEADLINE_EXPIRED_AFTER_LAST_READ"))
        self.assertEqual(res.counters.bytes_read, len(ORIGINAL))
        self.assertEqual((res.observed_revision, res.descriptor_content_sha256), ("r1", FULL_SHA))   # inspectable
        no_content(self, res)


class F_FullOriginal(TmpRoot):
    def full(self, req=None, **kw):
        return self.resolver().full_original(req or request(), **kw)

    def test_f_descriptor_only_and_task_needs_complete_original(self):
        res = self.full(max_full_output_bytes=None, out_path=None)
        self.assertEqual((res.outcome, res.missing_capability), (FALLBACK_REQUIRED,
                                                                 "EXPLICIT_MAX_FULL_OUTPUT_BYTES_AND_OUTPUT_FILE"))
        self.assertEqual((res.fallback["content_sha256"], res.fallback["body_inlined"]), (FULL_SHA, False))
        self.assertEqual(self.provider.stats.content_opens, 0)
        res = self.resolver().resolve(request(need="COMPLETE_ORIGINAL"))
        self.assertEqual((res.outcome, res.reason), (FALLBACK_REQUIRED, "TASK_NEEDS_COMPLETE_ORIGINAL"))
        self.assertEqual(self.provider.stats.content_opens, 0)
        no_content(self, res)

    def test_f_explicit_full_fetch_complete_bytes_no_overwrite(self):
        out = self.dir / "full.bin"
        res = self.full(max_full_output_bytes=131072, out_path=out)
        self.assertEqual(res.outcome, FULL_ORIGINAL_READY, res.reason)
        self.assertEqual(out.read_bytes(), ORIGINAL)
        self.assertEqual(res.full_output, {"file": "full.bin", "bytes": len(ORIGINAL), "sha256": FULL_SHA,
                                           "complete": True})
        again = self.full(max_full_output_bytes=131072, out_path=out)
        self.assertEqual((again.outcome, again.reason), ("INVALID_REQUEST", "OUTPUT_FILE_EXISTS_NO_OVERWRITE"))
        self.assertEqual(out.read_bytes(), ORIGINAL)

    def test_f_output_file_created_during_read_is_never_overwritten(self):
        out = self.dir / "race.bin"
        self.provider.on("after_read", lambda n: out.exists() or out.write_bytes(b"someone else's file"))
        res = self.full(max_full_output_bytes=131072, out_path=out)
        self.assertEqual((res.outcome, res.reason), ("INVALID_REQUEST", "OUTPUT_FILE_EXISTS_NO_OVERWRITE"))
        self.assertEqual(out.read_bytes(), b"someone else's file")

    def test_f_budget_too_small_is_bounded_refusal_never_truncation(self):
        out = self.dir / "small.bin"
        res = self.full(max_full_output_bytes=50_000, out_path=out)
        self.assertEqual((res.outcome, res.reason), (LIMIT_EXCEEDED, "SOURCE_EXCEEDS_FULL_OUTPUT_BUDGET"))
        self.assertEqual(res.fallback["canonical_ref"], "fixture:public-report")
        self.assertFalse(out.exists())
        self.assertEqual(self.full(max_full_output_bytes=2_000_000, out_path=out).outcome, "INVALID_REQUEST")

    def test_f_fallback_cannot_bypass_hash_stale_private(self):
        out = self.dir / "x.bin"
        self.assertEqual(self.full(request(expected_content_sha256="3" * 64), max_full_output_bytes=131072,
                                   out_path=out).outcome, HASH_MISMATCH)
        self.assertEqual(self.full(request(expected_revision="r0"), max_full_output_bytes=131072,
                                   out_path=out).outcome, STALE)
        self.assertEqual(self.full(request(subject_ref="actor:stranger"), max_full_output_bytes=131072,
                                   out_path=out).outcome, DENIED)
        self.assertEqual(self.full(request(way_home="fixture:elsewhere"), max_full_output_bytes=131072,
                                   out_path=out).outcome, IDENTITY_MISMATCH)
        self.assertFalse(out.exists())
        self.assertEqual(self.provider.stats.content_opens, 0)


class H_Measurement(TmpRoot):
    def test_h_compare_measures_fresh_scenarios(self):
        out = self.dir / "cmp"
        p = subprocess.run([sys.executable, "-B", "-m", "signalvev_sensing.context_slice_cli", "compare",
                            "--registry", str(FIX / "registry.json"), "--request", str(FIX / "request.json"),
                            "--out-dir", str(out)], capture_output=True, timeout=60,
                           env=dict(os.environ, PYTHONPATH=str(SRC)))
        self.assertEqual(p.returncode, 0, p.stderr)
        m = json.loads((out / "measurement.json").read_bytes())
        full, sl = m["scenarios"]
        self.assertEqual((full["outcome"], sl["outcome"]), (FULL_ORIGINAL_READY, SLICE_READY))
        self.assertEqual((out / "full_original.bin").read_bytes(), ORIGINAL)
        self.assertEqual(full["model_bound_output_bytes"]["body_file"], len(ORIGINAL))
        self.assertEqual(full["model_bound_output_bytes"]["total"],
                         len(ORIGINAL) + len((out / "full_original_envelope.json").read_bytes()) - 1)
        self.assertLessEqual(sl["model_bound_output_bytes"]["total"], 4096)
        self.assertEqual((full["local_bytes_read_for_hashing_and_checks"],
                          sl["local_bytes_read_for_hashing_and_checks"]), (len(ORIGINAL), len(ORIGINAL)))
        self.assertEqual((sl["resolver_counters"]["provider_calls"], sl["resolver_counters"]["content_opens"]), (2, 1))
        self.assertEqual((m["tokens"][:7], m["net_saving"], m["model_turns"][:7]), ("UNKNOWN", "UNKNOWN", "UNKNOWN"))
        self.assertTrue(m["cache"].startswith("NONE"))
        rc2 = subprocess.run([sys.executable, "-B", "-m", "signalvev_sensing.context_slice_cli", "compare",
                              "--registry", str(FIX / "registry.json"), "--request", str(FIX / "request.json"),
                              "--out-dir", str(out)], capture_output=True, timeout=60,
                             env=dict(os.environ, PYTHONPATH=str(SRC)))
        self.assertEqual(rc2.returncode, 64)                                       # no overwrite of evidence


if __name__ == "__main__":
    unittest.main()
