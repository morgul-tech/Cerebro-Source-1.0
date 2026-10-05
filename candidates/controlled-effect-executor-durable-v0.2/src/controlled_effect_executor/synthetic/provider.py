"""SYNTHETIC provider fixture (reference only): local dict state, no network, no credentials, no real provider.

Two facades over one state: `adapter` (the only mutation entry; refuses anything but an executor-minted grant) and
`readback` (read-only by construction). Behaviour is scripted per call so the ambiguity cases are deterministic.
"""
from __future__ import annotations

import threading

from ..errors import ExecutorError, ResponseLost
from ..grant import require_grant
from ..ports import ProviderAck, TargetObservation

LABEL = "SYNTHETIC_PROVIDER_FIXTURE_NOT_A_REAL_PROVIDER"
APPLY_MODES = ("commit_ok", "commit_lose_response", "fail_before_commit", "commit_bad_ack", "ack_without_commit")
READBACK_MODES = ("normal", "unavailable", "non_authoritative", "history_incomplete", "wrong_target")


class ProviderRejected(ExecutorError):
    """Synthetic definite rejection (precondition version mismatch). The executor still treats it as 'no response'."""


class _Adapter:
    def __init__(self, provider: "SyntheticProvider") -> None:
        self._p = provider

    def apply(self, grant: object) -> ProviderAck:
        return self._p._apply(require_grant(grant))


class _Readback:
    def __init__(self, provider: "SyntheticProvider") -> None:
        self._p = provider

    def read_target_state(self, target_identity: str) -> TargetObservation:
        return self._p._read(target_identity)


class SyntheticProvider:
    label = LABEL

    def __init__(self, targets: dict) -> None:
        """targets: {target_identity: (version_str, artifact_version_str)}"""
        self._lock = threading.RLock()
        self._state = {t: {"version": v, "artifact_version": a, "applied": [], "ops": []}
                       for t, (v, a) in targets.items()}
        self.apply_call_count = 0  # test oracle: calls that carried a valid grant
        self.mutation_count = 0  # test oracle: times the synthetic target state actually changed
        self.readback_call_count = 0
        self.refused_bypass_count = 0
        self.duplicate_correlation_refused = 0
        self.apply_script: list[str] = []
        self.readback_script: list[str] = []
        self.adapter = _Adapter(self)
        self.readback = _Readback(self)

    def _next(self, script: list, default: str, allowed: tuple) -> str:
        mode = script.pop(0) if script else default
        if mode not in allowed:
            raise ValueError(f"unknown synthetic mode {mode}")
        return mode

    def _commit(self, grant) -> None:
        st = self._state[grant.target_identity]
        if grant.target_precondition_version is not None and st["version"] != grant.target_precondition_version:
            raise ProviderRejected("precondition-version-mismatch")
        st["version"] = str(int(st["version"]) + 1)
        st["artifact_version"] = grant.artifact_version
        st["applied"].append(grant.correlation_ref)
        st["ops"].extend(op.name for op in grant.operations)
        self.mutation_count += 1

    def _apply(self, grant) -> ProviderAck:
        with self._lock:
            self.apply_call_count += 1
            if any(grant.correlation_ref in st["applied"] for st in self._state.values()):
                # provider-side idempotency on the correlation ref (what a production provider/fencing token must do)
                self.duplicate_correlation_refused += 1
                raise ProviderRejected("correlation-already-applied")
            mode = self._next(self.apply_script, "commit_ok", APPLY_MODES)
            if mode == "fail_before_commit":
                raise ResponseLost("synthetic: no response, nothing was committed")
            if mode == "ack_without_commit":
                return ProviderAck(grant.correlation_ref)
            self._commit(grant)
            if mode == "commit_lose_response":
                raise ResponseLost("synthetic: committed, response lost")
            if mode == "commit_bad_ack":
                return ProviderAck("SYNTH-WRONG-CORRELATION")
            return ProviderAck(grant.correlation_ref)

    def _read(self, target_identity: str) -> TargetObservation:
        with self._lock:
            self.readback_call_count += 1
            mode = self._next(self.readback_script, "normal", READBACK_MODES)
            if mode == "unavailable":
                raise ConnectionError("synthetic: readback unavailable")
            st = self._state[target_identity]
            return TargetObservation(
                target_identity="SYNTH-OTHER-TARGET" if mode == "wrong_target" else target_identity,
                observed_version=st["version"], artifact_version=st["artifact_version"],
                applied_correlation_refs=tuple(st["applied"]),
                history_complete=mode != "history_incomplete", authoritative=mode != "non_authoritative")

    # -- test inspection only (not part of the executor-facing ports) ---------------------------------------------------
    def snapshot(self, target_identity: str) -> dict:
        with self._lock:
            st = self._state[target_identity]
            return {"version": st["version"], "artifact_version": st["artifact_version"],
                    "applied": tuple(st["applied"]), "ops": tuple(st["ops"])}
