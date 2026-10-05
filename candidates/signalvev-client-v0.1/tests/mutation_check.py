#!/usr/bin/env python3
"""Mutation sanity check for the CLIENT package: each mutant breaks one safety property; the suite must kill it.

Copies the client + core + reference files to a temp tree, applies one textual mutation to the copy, runs the client
unit suite, and requires a failure. The real tree is never modified. Needs no network. If nats-py is importable
(via PYTHONPATH) the real-client tests take part, otherwise they are skipped and the mutants are judged without them.
usage: python3 candidates/signalvev-client-v0.1/tests/mutation_check.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAND = HERE.parent
REPO = CAND.parents[1]

B, S, C, CF, SO = ("nats_binding.py", "session.py", "cli.py", "config.py", "synthetic_owner.py")
MUTANTS = [
    (B, '"allow_reconnect": role == LISTEN,', '"allow_reconnect": True,', "send connection may reconnect (silent replay path)"),
    (B, '"pending_size": 0,', '"pending_size": 2 * 1024 * 1024,', "library buffers publishes across reconnects"),
    (B, "if _is_pre_send_error(exc):", "if True:", "every publish error claims 'proven not written'"),
    (B, "or nc.is_closed or not nc.is_connected:", "or nc.is_closed:", "publish attempted while disconnected"),
    (B, 'if self._role != SEND:\n            raise NotSentError("ROLE_NOT_SEND")', "if False:\n            raise NotSentError(\"ROLE_NOT_SEND\")", "listen connection may publish"),
    (B, 'if subject not in ALLOWED_SUBJECTS:\n            raise BindingError("SUBJECT_NOT_ALLOWED"', 'if False:\n            raise BindingError("SUBJECT_NOT_ALLOWED"', "wildcard/other subjects may be subscribed"),
    (B, "dispatcher.submit(handler, bytes(msg.data))", "handler(bytes(msg.data))", "receiver runs on the event loop"),
    (B, "            self._bump(\"flush_errors\")\n            self._note_error(exc)\n            raise", "            self._bump(\"flush_errors\")\n            self._note_error(exc)\n            return", "flush failure swallowed => false ACCEPTED"),
    ("dispatch.py", "                self.counters[\"dropped_overflow\"] += 1\n                return False\n        return True", "                return False\n        return True", "overflow not counted"),
    (S, 'self._lock = EvidenceLock(self._cfg.evidence_dir, "sender").acquire()', "self._lock = None", "two senders share one ledger"),
    (CF, "if not tls_requested and not _is_loopback(parts.hostname) and not allow_plaintext:", "if False:", "plaintext to non-loopback allowed"),
    (CF, "if any(f in lowered for f in _SECRET_KEY_FRAGMENTS) and key not in allowed:", "if False:", "inline secrets accepted"),
    (C, '"UNKNOWN_SEND": EXIT_UNKNOWN', '"UNKNOWN_SEND": EXIT_OK', "UNKNOWN_SEND reported as success"),
    (SO, 'elif entry["owner_seq"] > request.owner_seq:', "elif False:", "newer owner truth not recognised (C1)"),
    ("_bootstrap.py", "elif _sha256(target) != expected:", "elif False:", "tampered bundled file accepted"),
]


def main() -> int:
    survivors, killed, hangs = [], 0, []
    for fname, old, new, label in MUTANTS:
        with tempfile.TemporaryDirectory(prefix="client-mutant-") as tmp:
            root = Path(tmp)
            shutil.copytree(CAND, root / "candidates" / "signalvev-client-v0.1", ignore=shutil.ignore_patterns("__pycache__", "packaging"))
            shutil.copytree(REPO / "candidates" / "signalvev-sensing-runtime-v0.1" / "src", root / "candidates" / "signalvev-sensing-runtime-v0.1" / "src",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (root / "tooling" / "validator").mkdir(parents=True)
            for f in ("v01", "v09", "v15", "v16"):
                shutil.copyfile(REPO / "tooling" / "validator" / f"signalvev_reference_{f}_validation.py", root / "tooling" / "validator" / f"signalvev_reference_{f}_validation.py")
            ref = root / "candidates" / "signalvev-reference-v0.1"
            shutil.copytree(REPO / "candidates" / "signalvev-reference-v0.1", ref)
            target = root / "candidates" / "signalvev-client-v0.1" / "src" / "signalvev_client" / fname
            text = target.read_text(encoding="utf-8")
            if old not in text:
                survivors.append(f"UNAPPLIED:{label}")
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            try:
                res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(root / "candidates" / "signalvev-client-v0.1" / "tests")],
                                     capture_output=True, text=True, timeout=90,
                                     env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "PYTHONPATH": os.environ.get("PYTHONPATH", "")})
                died = res.returncode != 0
            except subprocess.TimeoutExpired:      # a hang is a detected break too (the suite no longer completes)
                died = True
                hangs.append(label)
            if died:
                killed += 1
            else:
                survivors.append(label)
    print(f"client mutation_check: {killed}/{len(MUTANTS)} killed (of which by hang: {hangs}); survivors={survivors}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
