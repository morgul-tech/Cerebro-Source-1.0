"""Shared fixtures for the real-PostgreSQL tests. All data is SYNTH-labelled; credentials are generated and disposable.

Tests that need PostgreSQL call ``require_pg()`` (skips with the reason 'UNRUN' if the runner did not provide a
disposable test endpoint). Source-tree runs put ../src on sys.path; an installed-wheel run sets CEE_V02_USE_INSTALLED=1
so the INSTALLED distribution is what gets imported.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
if os.environ.get("CEE_V02_USE_INSTALLED") != "1" and SRC not in sys.path:
    sys.path.insert(0, SRC)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from controlled_effect_executor import (ApprovalView, BatchSpec, DelegationView, Operation,  # noqa: E402
                                        TargetScopeEntry)
from controlled_effect_executor_durable import (DurableControlledEffectExecutor, PgAdmissionStore,  # noqa: E402
                                                create_schema, make_connection_factory)
from controlled_effect_executor_durable.synthetic_pg import (PgSyntheticOwner, PgSyntheticProvider,  # noqa: E402
                                                             owner_ddl, provider_ddl, run_ddl)

import pg_harness as H  # noqa: E402

T0 = 1_000_000
TARGET = "SYNTH-TARGET-A"
ART_OLD, ART_NEW = "SYNTH-ARTIFACT-V1", "SYNTH-ARTIFACT-V2"
OPS = (Operation.of("SYNTH-OP-SET", key="alpha", value=1), Operation.of("SYNTH-OP-TAG", tag="beta"))
SCOPE = "SYNTH-EXECUTOR-SCOPE-A"


def clock() -> int:
    return T0


def default_spec(**over) -> BatchSpec:
    base = dict(
        work_order_ref="SYNTH-WO-1", effect_ref="SYNTH-EFFECT-1", operations=OPS, target_identity=TARGET,
        target_precondition_version="3", artifact_version=ART_NEW, actor_ref="SYNTH-ACTOR-1", actor_generation=4,
        delegation_ref="SYNTH-DELEG-1", delegation_revision=1, delegation_expiry=T0 + 3600,
        human_approval_ref="SYNTH-APPROVAL-1", idempotency_key="SYNTH-IDEM-1", rollback_ref=None)
    base.update(over)
    return BatchSpec(**base)


def spec_to_json(spec: BatchSpec) -> str:
    return spec.canonical_text()


def spec_from_json(text: str) -> BatchSpec:
    return BatchSpec.from_mapping(json.loads(text))


def conn_params() -> "dict | None":
    return H.external_params_from_env()


def require_pg() -> dict:
    params = conn_params()
    if params is None:
        raise unittest.SkipTest("UNRUN: no disposable PostgreSQL endpoint (use run_pg_tests.py)")
    return params


def factory(application_name: str, **over):
    params = dict(require_pg())
    params["application_name"] = application_name
    params.update(over)
    return make_connection_factory(params)


def make_store(schema: str, *, scope: str = SCOPE, hooks=None, app: str = "cee_test_adapter", connect=None,
               owner=None, **kw) -> PgAdmissionStore:
    connect = connect or factory(app)
    owner = owner or PgSyntheticOwner(factory("cee_test_owner"), schema)
    return PgAdmissionStore(connect, schema=schema, scope_ref=scope, owner=owner, clock=clock, hooks=hooks, **kw)


def make_provider(schema: str, **kw) -> PgSyntheticProvider:
    return PgSyntheticProvider(factory("cee_test_provider"), schema + "_prov", **kw)


def seed_owner(owner: PgSyntheticOwner, spec: "BatchSpec | None" = None) -> None:
    spec = spec or default_spec()
    owner.put_delegation(DelegationView(
        delegation_ref="SYNTH-DELEG-1", delegation_revision=1, status="ACTIVE", actor_ref="SYNTH-ACTOR-1",
        actor_generation=4, allowed_operations=frozenset({"SYNTH-OP-SET", "SYNTH-OP-TAG"}),
        target_scope=(TargetScopeEntry(TARGET, frozenset({ART_NEW})),), expires_at=T0 + 3600, owner_currentness=0))
    approve(owner, spec)


def approve(owner: PgSyntheticOwner, spec: BatchSpec, **over) -> None:
    fields = dict(approval_ref=spec.human_approval_ref, status="APPROVED", target_identity=spec.target_identity,
                  artifact_version=spec.artifact_version, operations_digest=spec.operations_digest)
    fields.update(over)
    owner.put_approval(ApprovalView(**fields))


class PgWorld:
    """One fresh schema pair (adapter+owner / provider) per instance; dropped in ``close``."""

    def __init__(self, **provider_kw) -> None:
        self.params = require_pg()
        self.schema = H.new_schema_name()
        self.admin = factory("cee_test_admin")
        create_schema(self.admin, self.schema)
        run_ddl(self.admin, owner_ddl(self.schema))
        run_ddl(self.admin, provider_ddl(self.schema + "_prov"))
        self.owner = PgSyntheticOwner(factory("cee_test_owner"), self.schema)
        self.provider = make_provider(self.schema, **provider_kw)
        self.provider.seed_target(TARGET, "3", ART_OLD)
        seed_owner(self.owner)
        self.store = make_store(self.schema, owner=self.owner)
        self.executor = DurableControlledEffectExecutor(
            store=self.store, provider=self.provider.adapter, readback=self.provider.readback, clock=clock)

    def new_executor(self, store=None, *, readback=None, provider=None, scope: str = SCOPE):
        store = store or make_store(self.schema, scope=scope, owner=self.owner)
        return store, DurableControlledEffectExecutor(
            store=store, provider=(provider or self.provider).adapter,
            readback=readback or (provider or self.provider).readback, clock=clock)

    def admit(self, spec: "BatchSpec | None" = None, digest: "str | None" = None, store=None):
        spec = spec or default_spec()
        return (store or self.store).admit(spec, spec.digest if digest is None else digest)

    def fence(self, spec: "BatchSpec | None" = None):
        spec = spec or default_spec()
        decision = self.admit(spec)
        assert decision.state == "FENCED", decision
        return spec, decision.receipt

    # -- raw inspection through an independent admin connection ---------------------------------------------------------
    def q(self, sql: str, params=()) -> list:
        conn = self.admin()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            conn.close()

    def count(self, table: str, where: str = "", params=()) -> int:
        return int(self.q(f"SELECT count(*) AS n FROM {self.schema}.{table} {where}", params)[0]["n"])

    def sql(self, sql: str, params=()) -> None:
        conn = self.admin()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
        finally:
            conn.close()

    def close(self) -> None:
        H.drop_schemas(self.admin, [self.schema, self.schema + "_prov"])


class PgCase(unittest.TestCase):
    """Base: builds a fresh PgWorld per test and always drops its schemas."""

    provider_kw: dict = {}

    def setUp(self) -> None:
        self.w = PgWorld(**self.provider_kw)
        self.addCleanup(self.w.close)


def cluster():
    if H.ACTIVE_CLUSTER is None:
        raise unittest.SkipTest("UNRUN: the cluster is not owned by this run (restart tests need run_pg_tests.py)")
    return H.ACTIVE_CLUSTER


WORKER = os.path.join(HERE, "pg_worker.py")


def spawn(job: dict) -> subprocess.Popen:
    """Start a real OS process running one job. Credentials travel only through the child's environment."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           H.ENV_CONN: os.environ[H.ENV_CONN]}
    if os.environ.get("CEE_V02_USE_INSTALLED") == "1":
        env["CEE_V02_USE_INSTALLED"] = "1"
    proc = subprocess.Popen([sys.executable, WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(job))
    proc.stdin.close()
    return proc


def finish(proc: subprocess.Popen, timeout: float = 90.0) -> tuple:
    """(returncode, parsed-json-or-None, stderr-tail). A SIGKILLed worker has returncode -9 and no JSON."""
    out, err = proc.communicate(timeout=timeout)
    parsed = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    return proc.returncode, parsed, err[-400:]


def scratch(name: str) -> str:
    """A path inside a per-process temp directory (barrier files); cleaned by the OS temp policy / run end."""
    global _SCRATCH
    try:
        _SCRATCH
    except NameError:
        _SCRATCH = tempfile.mkdtemp(prefix="cee_v02_barrier_", dir=tempfile.gettempdir())
    return os.path.join(_SCRATCH, name)
