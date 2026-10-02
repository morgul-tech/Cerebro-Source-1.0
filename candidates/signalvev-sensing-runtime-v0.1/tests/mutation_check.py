#!/usr/bin/env python3
"""Mutation check: every mutant below breaks one load-bearing line; the suite MUST fail for each (mutant killed).
Run: python3 tests/mutation_check.py   (copies the candidate to a temp dir; never edits the real source)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CAND = Path(__file__).resolve().parents[1]
VALIDATOR_DIR = CAND.parents[1] / "tooling" / "validator"

M = []  # (name, file, old, new)


def m(name, file, old, new):
    M.append((name, file, old, new))


m("irrelevant-receiver-still-processed", "receiver.py", "if applicability == APPLICABILITY_NOT_APPLICABLE:", "if False:")
m("applicability-hold-treated-as-applies", "receiver.py", "if applicability != APPLIES:", "if False:")
m("dedupe-by-event-id-disabled", "receiver.py", "if seen is not None:", "if False:")
m("changed-fingerprint-not-conflict", "receiver.py", 'if seen["fingerprint"] != fp or seen["d0_hash"] != d0_hash:', "if False:")
m("idempotency-scope-dedupe-disabled", "receiver.py", "if same_scope is not None:", "if False:")
m("owner-seq-collision-ignored", "receiver.py", 'if hw is not None and seq == hw["owner_seq"] and fp != hw["fingerprint"]:', "if False:")
m("local-stale-check-disabled", "receiver.py", 'stale_locally = hw is not None and seq < hw["owner_seq"]', "stale_locally = False")
m("ttl-not-enforced", "receiver.py", 'if now > issued + env["ttl_seconds"]:', "if False:")
m("resolver-unavailable-acks", "receiver.py", 'self._terminal(d0, HOLD_UNREADABLE, "RESOLVER_UNAVAILABLE_OR_FAILED"', 'self._terminal(d0, ACK_READ, "RESOLVER_UNAVAILABLE_OR_FAILED"')
m("resolver-called-twice", "receiver.py", "res = self._resolver.resolve(request)            # EXACTLY ONE call; no retry on any outcome; lock NOT held", "self._resolver.resolve(request); res = self._resolver.resolve(request)")
m("depth-always-revision-check", "receiver.py", 'depth = DEPTH_REVISION_CHECK if delta["kind"] == "INLINE" else DEPTH_POINTER_GROUND', "depth = DEPTH_REVISION_CHECK")
m("truth-guard-skipped", "receiver.py", 'if truth["status"] != "TRUTH_ESTABLISHED":', "if False:")
m("pointer-source-not-checked", "receiver.py", "elif res.source_ref != d0[\"owner_ref\"]:", "elif False:")
m("content-mismatch-not-conflict", "receiver.py", 'if res.observed_sha256 != d0["delta"]["expected_sha256"]:', "if False:")
m("owner-superseded-ignored", "receiver.py", "if res.revision_relation == SUPERSEDED:", "if False:")
m("unknown-relation-acked", "receiver.py", "if res.revision_relation != SAME:", "if False:")
m("conflict-spam-not-suppressed", "cursor.py", "if (event_id, seen_d0_hash) in self._conflicts:", "if False:")
m("highwater-can-decrease", "cursor.py", '(hw is None or ev["owner_seq"] > hw["owner_seq"])', "True")
m("restart-loses-dedupe", "cursor.py", "        for rec in records:\n            try:\n                self._apply(rec)", "        for rec in []:\n            try:\n                self._apply(rec)")
m("pending-claim-not-recovered", "cursor.py", 'if ev["disposition"] == PENDING:', "if False:")
m("semantic-change-never-judgment", "activation.py", "if change_class == SEMANTIC:", "if False:")
m("candidates-not-sorted", "activation.py", "uniq = tuple(sorted(set(candidates)))", "uniq = tuple(candidates)")
m("mechanical-wakes-judgment", "activation.py", 'return ActivationDecision(DETERMINISTIC, ("MECHANICAL_CHANGE_CONFIRMED_AT_OWNER",))', 'return ActivationDecision(JUDGMENT_REQUIRED, ("MECHANICAL_CHANGE_CONFIRMED_AT_OWNER",))')
m("holds-can-wake-judgment", "activation.py", "if disposition != ACK_READ:", "if False:")
m("uncommitted-event-accepted", "owner_event.py", 'commit.get("state") == COMMITTED', "True")
m("unknown-send-replayed", "sender.py", "if prior in (ACCEPTED, UNKNOWN_SEND, INTENDED):", "if prior in (ACCEPTED, INTENDED):")
m("write-ahead-intent-removed", "sender.py", "self._ledger.intend(ev.event_id, built.fingerprint)   # write-ahead: crash after this => UNKNOWN_SEND", "pass")
m("crash-intent-not-unknown", "sender.py", "if st[\"state\"] == INTENDED:", "if False:")
m("flush-failure-reported-accepted", "transport.py", 'return TransportResult(UNKNOWN_SEND, "FLUSH_FAILED_SERVER_RECEIPT_UNPROVEN")', 'return TransportResult(ACCEPTED, "x")')
m("publish-failure-reported-not-sent", "transport.py", 'return TransportResult(UNKNOWN_SEND, "PUBLISH_FAILED_WRITE_STATE_UNPROVEN")', 'return TransportResult(NOT_SENT, "x")')
m("d0-size-unbounded", "d0.py", "if len(d0_bytes) > MAX_D0_BYTES:", "if False:")
m("frame-size-unbounded", "d0.py", "len(data) > MAX_FRAME_BYTES:\n        raise Hold", "False:\n        raise Hold")
m("d0-extra-fields-allowed", "d0.py", "if set(d0) != expected:", "if False:")
m("d0-not-bound-to-envelope", "d0.py", 'if env["payload_hash"] != sha256_hex(canonical(dict(d0))) or env["payload_ref"] != f"d0:{d0.get(\'event_id\')}":', "if False:")
m("effect-authority-claim-allowed", "d0.py", 'if env["effect_class"] != "NONE" or env["authority_class"] != "NONE":', "if False:")
m("version-gate-skipped", "d0.py", 'if gate["status"] != "ACCEPTED":', "if False:")
m("envelope-d0-identity-unchecked", "d0.py", 'd0["referent"]["id"], d0["owner_ref"]) or env["message_id"] != d0["event_id"]:', 'd0["referent"]["id"], d0["owner_ref"]) and False:')
m("registry-idempotency-scope-unchecked", "d0.py", 'if env["idempotency_key"] != idempotency_key(d0):', "if False:")
m("way-home-optional", "d0.py", 'if not (isinstance(wh, list) and wh and all(is_id(w) for w in wh)):', "if False:")
m("evidence-accepts-free-text", "store.py", '    raise SensingError("EVIDENCE_NOT_ID_ONLY", f"{key}: evidence stores ids/hashes/codes only, never free text or state")', "    return")
m("torn-tail-not-repaired", "store.py", "os.truncate(self.path, good_len)", "pass")
m("complete-corruption-tolerated", "store.py", 'raise StoreCorrupt(f"{self.path}: corrupt complete record at line {i + 1}; refusing to guess")', "continue")
m("closure-claims-work-consumed", "return_sink.py", "work_consumed: bool = False", "work_consumed: bool = True")
m("noise-is-returned", "return_sink.py", "return disposition in RETURN_SELECTED", "return True")
m("ack-stage-always-read", "return_sink.py", 'receipt_stage="READ" if disposition == ACK_READ else "DELIVERED"', 'receipt_stage="READ"')
m("edge-half-view-claims-break", "evidence.py", 'if send_ledger is None or cursor is None:', "if False:")

m("admit-not-atomic-lock-removed", "receiver.py", '        with self._lock:\n            early = self._admit(d0, env["payload_hash"])', '        early = self._admit(d0, env["payload_hash"])')
m("lock-held-across-owner-read", "receiver.py", "res = self._resolver.resolve(request)            # EXACTLY ONE call; no retry on any outcome; lock NOT held", "with self._lock:\n                res = self._resolver.resolve(request)")
m("hostile-frames-uncaught-both-layers", "receiver.py",
  ["except Exception:  # noqa: BLE001 - last line of defence: a hostile frame must never raise out of ingress",
   "except Exception:  # noqa: BLE001 - wrong types/unicode/depth inside a frame => typed hold, never a crash"],
  ["except ZeroDivisionError:", "except ZeroDivisionError:"])
# (removing only ONE of the two layers is an intentionally equivalent mutant: defence in depth keeps frames typed)
m("malformed-result-leaves-stuck-claim", "receiver.py", "except Exception:  # noqa: BLE001 - a malformed result is a typed hold, never a stuck PENDING claim", "except ZeroDivisionError:")
m("resolver-result-text-passes", "receiver.py", "# Only ids/hashes may travel on into the typed closure (STATE_STAYS_HOME): anything else is a bad result.\n        if not (", "# x\n        if False and (")
m("conflict-ignores-provenance-hash", "receiver.py", 'if seen["fingerprint"] != fp or seen["d0_hash"] != d0_hash:', 'if seen["fingerprint"] != fp:')
m("recovered-hold-never-surfaced", "receiver.py", 'if seen["reason"] == RECOVERED and not seen["surfaced"]:', "if False:")
m("duplicates-write-evidence", "receiver.py", "if disposition == DUPLICATE:\n            self._recorder.count(DUPLICATE)", "if False:\n            self._recorder.count(DUPLICATE)")
m("ttl-receiver-unbounded", "d0.py", 'if env["ttl_seconds"] > MAX_TTL_SECONDS:', "if False:")
m("principal-not-owner-accepted", "d0.py", 'if env["source"]["principal_ref"] != d0["owner_ref"]:', "if False:")
m("surfaced-marker-ignored", "cursor.py", 'elif kind == "SURFACED" and rec["event_id"] in self._events:', "elif False:")
m("publish-exception-uncaught", "sender.py", "except Exception:  # noqa: BLE001 - a raising transport may have written: unknown, never retried", "except ZeroDivisionError:")
m("intended-state-not-unknown", "sender.py", "if prior in (ACCEPTED, UNKNOWN_SEND, INTENDED):", "if prior in (ACCEPTED, UNKNOWN_SEND):")
m("sender-lock-removed", "sender.py", "self._lock = threading.RLock()", "self._lock = type('N', (), {'__enter__': lambda s: None, '__exit__': lambda s, *a: False})()")
m("recorder-unbounded", "evidence.py", "if self._written[kind] >= self._max_for(kind):", "if False:")
m("hold-schema-is-returned", "return_sink.py", "                             HOLD_APPLICABILITY})", "                             HOLD_APPLICABILITY, 'HOLD_SCHEMA'})")

m("highwater-without-owner-seq-confirmation", "cursor.py", 'rec["disposition"] == "ACK_READ" and rec["seq_confirmed"] and', 'rec["disposition"] == "ACK_READ" and')
m("owner-seq-mismatch-acked", "receiver.py", 'if res.owner_seq is not None and res.owner_seq != d0["owner_seq"]:', "if False:")
m("seq-confirmed-always-true", "receiver.py", 'res.candidates, res.owner_seq == d0["owner_seq"]', "res.candidates, True")
m("refuted-claim-holds-idem-scope", "cursor.py", 'if rec["disposition"] in ("CONFLICT_HOLD", "HOLD_IDENTITY") and', "if False and")
m("claim-failure-does-not-stop-work", "receiver.py", "except Exception:  # noqa: BLE001 - dedupe state that cannot be made durable => refuse the work (fail closed)", "except ZeroDivisionError:")
m("claim-write-failure-swallowed", "cursor.py", "            if strict:\n                raise", "            pass")
m("recorder-failure-not-contained", "receiver.py", "        except Exception:  # noqa: BLE001\n            self._recorder.count(\"RECORDER_WRITE_FAILED\")\n            return False", "        except ZeroDivisionError:\n            return False")
m("sender-lock-held-across-publish", "sender.py", "result = self._transport.publish(built.subject, built.data)   # lock NOT held: handlers may re-enter send()", "with self._lock:\n                    result = self._transport.publish(built.subject, built.data)")
m("inflight-send-not-refused", "sender.py", "if ev.event_id in self._inflight:", "if False:")
m("old-format-cursor-accepted", "cursor.py", "if records and records[0].get(\"schema\") != STORE_SCHEMA:", "if False:")
m("conflict-flood-unbounded", "cursor.py", "if (self._conflicts_per_event.get(event_id, 0) >= MAX_CONFLICTS_PER_EVENT", "if (False")
m("recorder-budget-not-rebuilt-from-file", "evidence.py", 'Counter(r["kind"] for r in self._store.records() if r.get("kind") != "HEADER")', "Counter()")


def run(tree: Path) -> int:
    env = dict(os.environ, SIGNALVEV_VALIDATOR_DIR=str(VALIDATOR_DIR), PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(tree / "tests"), "-p", "test_*.py"],
                       capture_output=True, text=True, env=env, cwd=tree)
    return p.returncode


def main() -> int:
    survivors, missing = [], []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(CAND, base, ignore=shutil.ignore_patterns("__pycache__"))
        assert run(base) == 0, "unmutated suite must pass first"
        for name, file, old, new in M:
            work = Path(tmp) / "mut"
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(base, work)
            target = work / "src" / "signalvev_sensing" / file
            text = target.read_text()
            olds, news = (old, new) if isinstance(old, list) else ([old], [new])
            if any(o not in text for o in olds):
                missing.append(name)
                continue
            for o, n in zip(olds, news):
                text = text.replace(o, n, 1)
            target.write_text(text)
            if run(work) == 0:
                survivors.append(name)
    killed = len(M) - len(survivors) - len(missing)
    print(f"mutation_check: {killed}/{len(M)} killed; survivors={survivors}; unapplied={missing}")
    return 1 if survivors or missing else 0


if __name__ == "__main__":
    sys.exit(main())
