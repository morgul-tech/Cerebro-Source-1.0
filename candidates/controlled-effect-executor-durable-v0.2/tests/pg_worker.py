"""Worker process for the multi-process PostgreSQL tests. Reads one JSON job from stdin, prints one JSON line.

Roles: admit | execute | recover | list. A job may install ONE deterministic hook at a named point (see the hook
points in store_pg / synthetic_pg) with an action:
  kill_self            SIGKILL this process exactly at that point (simulated crash; no cleanup, no result line)
  signal_then_block    write ``signal_file`` then sleep (the parent SIGKILLs it from outside)
  signal_then_release  write ``signal_file`` then wait for ``release_file`` (a barrier the parent controls)
A ``wait_file`` makes the worker spin until the parent creates it (a start barrier for real contention).
The generated test credentials arrive only through the CEE_V02_TEST_CONN environment variable; nothing is printed.
"""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
import time

import _pgworld as W  # noqa: E402  (sets sys.path for source/installed runs)
from controlled_effect_executor_durable import DurableControlledEffectExecutor  # noqa: E402


def _wait_for(path: str, what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise TimeoutError(what)
        time.sleep(0.005)


def _hook(job: dict):
    spec = job.get("hook")
    if not spec:
        return None, None

    def fn() -> None:
        action = spec["action"]
        if action == "kill_self":
            os.kill(os.getpid(), signal.SIGKILL)
        if spec.get("signal_file"):
            open(spec["signal_file"], "w").close()
        if action == "signal_then_block":
            time.sleep(600)
        elif action == "signal_then_release":
            _wait_for(spec["release_file"], "release barrier")
    return spec["point"], fn


def _jsonable(obj):
    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else obj


def main() -> int:
    job = json.loads(sys.stdin.read())
    schema, scope = job["schema"], job.get("scope", W.SCOPE)
    point, fn = _hook(job)
    store_hooks = {point: fn} if point and point.startswith(("admission.", "attempt.")) else {}
    provider_hooks = {point: fn} if point and point.startswith("provider.") else {}
    owner = W.PgSyntheticOwner(W.factory("cee_test_owner"), schema)
    store = W.make_store(schema, scope=scope, hooks=store_hooks, owner=owner, app=job.get("app", "cee_test_adapter"))
    if job.get("wait_file"):
        _wait_for(job["wait_file"], "start barrier")
    role = job["role"]
    if role == "admit":
        spec = W.spec_from_json(job["spec_json"])
        out = {"decision": _jsonable(store.admit(spec, spec.digest))}
    elif role == "list":
        page = store.list_unresolved(after_cursor=job.get("cursor"), limit=job.get("limit", 100))
        out = {"page": [{"admission_ref": i.admission_ref, "attempt_ref": i.attempt_ref, "state": i.state,
                         "batch_digest": i.batch_digest, "work_order_ref": i.work_order_ref,
                         "spec_json": i.spec.canonical_text(), "integrity_ok": i.integrity_ok} for i in page.items],
               "next_cursor": page.next_cursor}
    else:
        provider = W.PgSyntheticProvider(W.factory("cee_test_provider"), schema + "_prov",
                                         apply_mode=job.get("apply_mode", "commit_ok"),
                                         readback_mode=job.get("readback_mode", "normal"), hooks=provider_hooks)
        executor = DurableControlledEffectExecutor(store=store, provider=provider.adapter, readback=provider.readback,
                                                   clock=W.clock)
        if role == "execute":
            rec = store.get_admission(job["admission_ref"])
            out = {"result": _jsonable(executor.execute(rec.receipt, rec.spec))}
        elif role == "recover":
            out = {"result": _jsonable(executor.recover(job["admission_ref"]))}
        else:
            raise ValueError("unknown-role")
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
