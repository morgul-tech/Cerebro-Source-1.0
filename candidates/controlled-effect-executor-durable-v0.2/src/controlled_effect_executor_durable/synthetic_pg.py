"""SYNTHETIC fixtures that live in a disposable PostgreSQL test schema. Reference only; nothing here is production.

* ``PgSyntheticOwner``: the owner side of the shared admission transaction (delegation + approval rows in the SAME
  database as the adapter). Its mutations (revoke, amend, expire, approval changes) are ordinary UPDATEs, so they
  conflict with the adapter's ``FOR SHARE`` locks. It mints no Human or PM authority; every ref is SYNTH-labelled.
* ``PgSyntheticProvider``: a provider whose EFFECT transaction is a separate connection/transaction from any
  admission or ledger transaction, with provider-side idempotency per correlation, scripted failure modes, a delayed
  ("late") commit, and an explicit settlement record (the evidence behind a final NO_COMMIT). It is a model of a
  provider for tests, not a provider.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from controlled_effect_executor.errors import ExecutorError, ResponseLost
from controlled_effect_executor.grant import require_grant
from controlled_effect_executor.ports import ProviderAck, TargetObservation
from controlled_effect_executor.views import ApprovalView, DelegationView, TargetScopeEntry

from .schema import validate_schema
from .settlement import APPLIED, PENDING, SETTLED_ABSENT, UNKNOWN, SettlementEvidence

OWNER_LABEL = "SYNTHETIC_PG_OWNER_FIXTURE_NOT_PRODUCTION"
PROVIDER_LABEL = "SYNTHETIC_PG_PROVIDER_FIXTURE_NOT_A_REAL_PROVIDER"
APPLY_MODES = ("commit_ok", "commit_lose_response", "fail_before_commit", "reject_definite", "delay_commit",
               "ack_without_commit")
READBACK_MODES = ("normal", "unavailable", "non_authoritative", "history_incomplete")


class ProviderRejected(ExecutorError):
    """Synthetic definite refusal (precondition mismatch, duplicate correlation, fenced correlation)."""


def owner_ddl(schema: str) -> list:
    s = validate_schema(schema)
    return [
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"CREATE SEQUENCE {s}.cee_synth_owner_seq",
        f"""CREATE TABLE {s}.cee_synth_owner_delegation (
  delegation_ref text PRIMARY KEY, delegation_revision integer NOT NULL, status text NOT NULL,
  actor_ref text NOT NULL, actor_generation integer NOT NULL, allowed_operations text NOT NULL,
  target_scope text NOT NULL, expires_at bigint, currentness bigint NOT NULL)""",
        f"""CREATE TABLE {s}.cee_synth_owner_approval (
  approval_ref text PRIMARY KEY, status text NOT NULL, target_identity text NOT NULL,
  artifact_version text NOT NULL, operations_digest text NOT NULL, currentness bigint NOT NULL)""",
        f"CREATE TABLE {s}.cee_synth_owner_log (log_seq bigserial PRIMARY KEY, currentness bigint NOT NULL,"
        f" event text NOT NULL, ref text NOT NULL)",
    ]


def provider_ddl(schema: str) -> list:
    s = validate_schema(schema)
    return [
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"CREATE TABLE {s}.cee_synth_provider_target (target_identity text PRIMARY KEY, version bigint NOT NULL,"
        f" artifact_version text NOT NULL)",
        f"CREATE TABLE {s}.cee_synth_provider_applied (applied_seq bigserial PRIMARY KEY,"
        f" correlation_ref text NOT NULL UNIQUE, target_identity text NOT NULL, ops text NOT NULL)",
        f"CREATE TABLE {s}.cee_synth_provider_settlement (correlation_ref text PRIMARY KEY,"
        f" status text NOT NULL CHECK (status IN ('PENDING','SETTLED_ABSENT')), evidence_ref text NOT NULL,"
        f" queued_grant text)",
        f"CREATE TABLE {s}.cee_synth_provider_call (call_seq bigserial PRIMARY KEY, correlation_ref text NOT NULL,"
        f" mode text NOT NULL)",
    ]


def run_ddl(connect: Callable[[], Any], statements: list) -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        for st in statements:
            cur.execute(st)
        cur.execute("COMMIT")
    except Exception:
        try:
            conn.cursor().execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()


class _Tx:
    """One short transaction on its own connection (the synthetic fixtures' own boundary, distinct from the adapter's)."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def run(self, body: Callable[[Any], Any]) -> Any:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            try:
                result = body(cur)
            except BaseException:
                try:
                    cur.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise
            cur.execute("COMMIT")
            return result
        finally:
            conn.close()


def _scope_json(scope: tuple) -> str:
    return json.dumps([{"t": e.target_identity,
                        "v": None if e.artifact_versions is None else sorted(e.artifact_versions)} for e in scope],
                      sort_keys=True)


def _scope_from(text: str) -> tuple:
    return tuple(TargetScopeEntry(e["t"], None if e["v"] is None else frozenset(e["v"])) for e in json.loads(text))


class PgSyntheticOwner:
    label = OWNER_LABEL

    def __init__(self, connect: Callable[[], Any], schema: str) -> None:
        self._tx = _Tx(connect)
        self._s = validate_schema(schema)

    # -- the owner side of the SHARED admission transaction (runs on the adapter's cursor) ---------------------------
    def lock_and_read_delegation(self, cur: Any, delegation_ref: str) -> "DelegationView | None":
        cur.execute(
            f"SELECT delegation_ref, delegation_revision, status, actor_ref, actor_generation, allowed_operations,"
            f" target_scope, expires_at, currentness FROM {self._s}.cee_synth_owner_delegation"
            f" WHERE delegation_ref = %s FOR SHARE", (delegation_ref,))
        r = cur.fetchone()
        if r is None:
            return None
        return DelegationView(
            delegation_ref=r["delegation_ref"], delegation_revision=int(r["delegation_revision"]), status=r["status"],
            actor_ref=r["actor_ref"], actor_generation=int(r["actor_generation"]),
            allowed_operations=frozenset(json.loads(r["allowed_operations"])), target_scope=_scope_from(r["target_scope"]),
            expires_at=None if r["expires_at"] is None else int(r["expires_at"]), owner_currentness=int(r["currentness"]))

    def lock_and_read_approval(self, cur: Any, approval_ref: str) -> "ApprovalView | None":
        cur.execute(
            f"SELECT approval_ref, status, target_identity, artifact_version, operations_digest"
            f" FROM {self._s}.cee_synth_owner_approval WHERE approval_ref = %s FOR SHARE", (approval_ref,))
        r = cur.fetchone()
        return None if r is None else ApprovalView(
            approval_ref=r["approval_ref"], status=r["status"], target_identity=r["target_identity"],
            artifact_version=r["artifact_version"], operations_digest=r["operations_digest"])

    # -- owner-side mutations (each its own transaction; they take the row locks that conflict with FOR SHARE) -------
    def _tick(self, cur: Any, event: str, ref: str) -> int:
        cur.execute(f"SELECT nextval('{self._s}.cee_synth_owner_seq') AS n")
        n = int(cur.fetchone()["n"])
        cur.execute(f"INSERT INTO {self._s}.cee_synth_owner_log (currentness, event, ref) VALUES (%s, %s, %s)",
                    (n, event, ref))
        return n

    def put_delegation(self, view: DelegationView) -> None:
        def body(cur: Any) -> None:
            n = self._tick(cur, "PUT_DELEGATION", view.delegation_ref)
            cur.execute(
                f"INSERT INTO {self._s}.cee_synth_owner_delegation (delegation_ref, delegation_revision, status,"
                f" actor_ref, actor_generation, allowed_operations, target_scope, expires_at, currentness)"
                f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (view.delegation_ref, view.delegation_revision, view.status, view.actor_ref, view.actor_generation,
                 json.dumps(sorted(view.allowed_operations)), _scope_json(view.target_scope), view.expires_at, n))
        self._tx.run(body)

    def _update(self, ref: str, event: str, assignments: str, params: tuple) -> int:
        def body(cur: Any) -> int:
            n = self._tick(cur, event, ref)
            cur.execute(
                f"UPDATE {self._s}.cee_synth_owner_delegation SET {assignments},"
                f" delegation_revision = delegation_revision + 1, currentness = %s WHERE delegation_ref = %s",
                params + (n, ref))
            if cur.rowcount != 1:
                raise ExecutorError("owner-delegation-not-found")
            return n
        return self._tx.run(body)

    def revoke(self, delegation_ref: str) -> int:
        """UPDATE of the delegation row: waits for any admission transaction holding FOR SHARE on it."""
        return self._update(delegation_ref, "REVOKE", "status = 'REVOKED'", ())

    def expire(self, delegation_ref: str) -> int:
        return self._update(delegation_ref, "EXPIRE", "status = 'EXPIRED'", ())

    def amend(self, delegation_ref: str, *, actor_generation: "int | None" = None) -> int:
        if actor_generation is None:
            raise ExecutorError("owner-amend-nothing-to-change")
        return self._update(delegation_ref, "AMEND", "actor_generation = %s", (actor_generation,))

    def put_approval(self, view: ApprovalView) -> None:
        def body(cur: Any) -> None:
            n = self._tick(cur, "PUT_APPROVAL", view.approval_ref)
            cur.execute(
                f"INSERT INTO {self._s}.cee_synth_owner_approval (approval_ref, status, target_identity,"
                f" artifact_version, operations_digest, currentness) VALUES (%s, %s, %s, %s, %s, %s)",
                (view.approval_ref, view.status, view.target_identity, view.artifact_version, view.operations_digest, n))
        self._tx.run(body)

    def withdraw_approval(self, approval_ref: str) -> int:
        def body(cur: Any) -> int:
            n = self._tick(cur, "WITHDRAW_APPROVAL", approval_ref)
            cur.execute(f"UPDATE {self._s}.cee_synth_owner_approval SET status = 'WITHDRAWN', currentness = %s"
                        f" WHERE approval_ref = %s", (n, approval_ref))
            return n
        return self._tx.run(body)

    def log(self) -> list:
        def body(cur: Any) -> list:
            cur.execute(f"SELECT currentness, event, ref FROM {self._s}.cee_synth_owner_log ORDER BY log_seq")
            return [(int(r["currentness"]), r["event"], r["ref"]) for r in cur.fetchall()]
        return self._tx.run(body)

    def status_of(self, delegation_ref: str) -> "str | None":
        def body(cur: Any) -> "str | None":
            cur.execute(f"SELECT status FROM {self._s}.cee_synth_owner_delegation WHERE delegation_ref = %s",
                        (delegation_ref,))
            r = cur.fetchone()
            return None if r is None else r["status"]
        return self._tx.run(body)


class _Adapter:
    def __init__(self, provider: "PgSyntheticProvider") -> None:
        self._p = provider

    def apply(self, grant: object) -> ProviderAck:
        return self._p._apply(require_grant(grant))


class _Readback:
    def __init__(self, provider: "PgSyntheticProvider") -> None:
        self._p = provider

    def read_target_state(self, target_identity: str) -> TargetObservation:
        return self._p._read(target_identity)

    def read_settlement(self, correlation_ref: str) -> SettlementEvidence:
        return self._p._settlement(correlation_ref)


class _ReadbackV01Only:
    """A readback WITHOUT read_settlement (V0.1-shaped): proves NO_COMMIT can never be final through it."""

    def __init__(self, provider: "PgSyntheticProvider") -> None:
        self._p = provider

    def read_target_state(self, target_identity: str) -> TargetObservation:
        return self._p._read(target_identity)


class PgSyntheticProvider:
    label = PROVIDER_LABEL

    def __init__(self, connect: Callable[[], Any], schema: str, *, apply_mode: str = "commit_ok",
                 readback_mode: str = "normal", hooks: "Mapping[str, Callable[[], None]] | None" = None) -> None:
        if apply_mode not in APPLY_MODES or readback_mode not in READBACK_MODES:
            raise ValueError("unknown-synthetic-mode")
        self._tx = _Tx(connect)
        self._s = validate_schema(schema)
        self.apply_mode = apply_mode
        self.readback_mode = readback_mode
        self.apply_script: list = []  # per-call override, consumed first (in-process tests)
        self._hooks = dict(hooks or {})
        self.adapter = _Adapter(self)
        self.readback = _Readback(self)
        self.readback_v01_only = _ReadbackV01Only(self)

    def _hook(self, name: str) -> None:
        fn = self._hooks.get(name)
        if fn is not None:
            fn()

    # -- provider behaviour -------------------------------------------------------------------------------------------
    def seed_target(self, target_identity: str, version: str, artifact_version: str) -> None:
        def body(cur: Any) -> None:
            cur.execute(f"INSERT INTO {self._s}.cee_synth_provider_target VALUES (%s, %s, %s)",
                        (target_identity, int(version), artifact_version))
        self._tx.run(body)

    def _effect(self, cur: Any, correlation_ref: str, target: str, precondition: "str | None", artifact: str,
                op_names: list) -> None:
        # one provider-side critical section per correlation: an effect and a settlement can never interleave
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (correlation_ref,))
        cur.execute(f"SELECT status FROM {self._s}.cee_synth_provider_settlement WHERE correlation_ref = %s FOR UPDATE",
                    (correlation_ref,))
        st = cur.fetchone()
        if st is not None and st["status"] == SETTLED_ABSENT:
            raise ProviderRejected("correlation-settled-absent-late-apply-refused")
        cur.execute(f"SELECT 1 AS x FROM {self._s}.cee_synth_provider_applied WHERE correlation_ref = %s",
                    (correlation_ref,))
        if cur.fetchone() is not None:
            raise ProviderRejected("correlation-already-applied")
        cur.execute(f"SELECT version, artifact_version FROM {self._s}.cee_synth_provider_target"
                    f" WHERE target_identity = %s FOR UPDATE", (target,))
        t = cur.fetchone()
        if t is None:
            raise ProviderRejected("unknown-target")
        if precondition is not None and t["version"] != precondition:
            raise ProviderRejected("precondition-version-mismatch")
        cur.execute(f"UPDATE {self._s}.cee_synth_provider_target SET version = version + 1, artifact_version = %s"
                    f" WHERE target_identity = %s", (artifact, target))
        cur.execute(f"INSERT INTO {self._s}.cee_synth_provider_applied (correlation_ref, target_identity, ops)"
                    f" VALUES (%s, %s, %s)", (correlation_ref, target, json.dumps(op_names)))
        cur.execute(f"DELETE FROM {self._s}.cee_synth_provider_settlement WHERE correlation_ref = %s", (correlation_ref,))

    def _apply(self, grant: Any) -> ProviderAck:
        mode = self.apply_script.pop(0) if self.apply_script else self.apply_mode
        if mode not in APPLY_MODES:
            raise ValueError("unknown-synthetic-mode")
        corr = grant.correlation_ref

        def log(cur: Any) -> None:
            cur.execute(f"INSERT INTO {self._s}.cee_synth_provider_call (correlation_ref, mode) VALUES (%s, %s)",
                        (corr, mode))
        self._tx.run(log)  # committed immediately: the oracle counts calls even if everything after fails
        self._hook("provider.before_effect")
        ops = [op.name for op in grant.operations]
        if mode == "fail_before_commit":
            raise ResponseLost("synthetic: no response, nothing was committed")
        if mode == "ack_without_commit":
            return ProviderAck(corr)
        if mode == "reject_definite":
            def settle(cur: Any) -> None:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (corr,))
                cur.execute(f"INSERT INTO {self._s}.cee_synth_provider_settlement (correlation_ref, status, evidence_ref)"
                            f" VALUES (%s, 'SETTLED_ABSENT', %s) ON CONFLICT (correlation_ref) DO UPDATE SET"
                            f" status = 'SETTLED_ABSENT', evidence_ref = EXCLUDED.evidence_ref", (corr, "SYNTH-PROVIDER-REJECTED"))
            self._tx.run(settle)
            raise ProviderRejected("synthetic: definite refusal, durably settled absent")
        if mode == "delay_commit":
            queued = json.dumps({"t": grant.target_identity, "p": grant.target_precondition_version,
                                 "a": grant.artifact_version, "o": ops})

            def queue(cur: Any) -> None:
                cur.execute(f"INSERT INTO {self._s}.cee_synth_provider_settlement (correlation_ref, status, evidence_ref,"
                            f" queued_grant) VALUES (%s, 'PENDING', %s, %s) ON CONFLICT DO NOTHING",
                            (corr, "SYNTH-PROVIDER-QUEUED", queued))
            self._tx.run(queue)
            raise ResponseLost("synthetic: accepted into a slow queue, no response")
        self._tx.run(lambda cur: self._effect(cur, corr, grant.target_identity, grant.target_precondition_version,
                                              grant.artifact_version, ops))  # the provider's OWN effect transaction
        self._hook("provider.after_commit")
        if mode == "commit_lose_response":
            raise ResponseLost("synthetic: committed, response lost")
        return ProviderAck(corr)

    def deliver(self, correlation_ref: str) -> None:
        """TEST CONTROL: the previously queued (slow) operation finally commits. Refused if the provider has settled
        the correlation absent, which is exactly how an explicit fence excludes a late commit."""
        def body(cur: Any) -> None:
            cur.execute(f"SELECT status, queued_grant FROM {self._s}.cee_synth_provider_settlement"
                        f" WHERE correlation_ref = %s FOR UPDATE", (correlation_ref,))
            r = cur.fetchone()
            if r is None or r["queued_grant"] is None:
                raise ProviderRejected("nothing-queued")
            if r["status"] == SETTLED_ABSENT:
                raise ProviderRejected("correlation-settled-absent-late-apply-refused")
            q = json.loads(r["queued_grant"])
            self._effect(cur, correlation_ref, q["t"], q["p"], q["a"], q["o"])
        self._tx.run(body)

    def settle_absent(self, correlation_ref: str) -> None:
        """TEST CONTROL: the provider's OWN explicit finality (its fence/tombstone): this correlation can never apply."""
        def body(cur: Any) -> None:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (correlation_ref,))
            cur.execute(f"SELECT 1 AS x FROM {self._s}.cee_synth_provider_applied WHERE correlation_ref = %s",
                        (correlation_ref,))
            if cur.fetchone() is not None:
                raise ProviderRejected("already-applied")
            cur.execute(f"INSERT INTO {self._s}.cee_synth_provider_settlement (correlation_ref, status, evidence_ref)"
                        f" VALUES (%s, 'SETTLED_ABSENT', %s) ON CONFLICT (correlation_ref) DO UPDATE SET"
                        f" status = 'SETTLED_ABSENT', evidence_ref = EXCLUDED.evidence_ref",
                        (correlation_ref, "SYNTH-PROVIDER-FENCE"))
        self._tx.run(body)

    def _read(self, target_identity: str) -> TargetObservation:
        if self.readback_mode == "unavailable":
            raise ConnectionError("synthetic: readback unavailable")

        def body(cur: Any) -> TargetObservation:
            cur.execute(f"SELECT version, artifact_version FROM {self._s}.cee_synth_provider_target"
                        f" WHERE target_identity = %s", (target_identity,))
            t = cur.fetchone()
            cur.execute(f"SELECT correlation_ref FROM {self._s}.cee_synth_provider_applied WHERE target_identity = %s"
                        f" ORDER BY applied_seq", (target_identity,))
            applied = tuple(r["correlation_ref"] for r in cur.fetchall())
            return TargetObservation(
                target_identity=target_identity, observed_version=t["version"], artifact_version=t["artifact_version"],
                applied_correlation_refs=applied, history_complete=self.readback_mode != "history_incomplete",
                authoritative=self.readback_mode != "non_authoritative")
        return self._tx.run(body)

    def _settlement(self, correlation_ref: str) -> SettlementEvidence:
        if self.readback_mode == "unavailable":
            raise ConnectionError("synthetic: readback unavailable")

        def body(cur: Any) -> SettlementEvidence:
            cur.execute(f"SELECT 1 AS x FROM {self._s}.cee_synth_provider_applied WHERE correlation_ref = %s",
                        (correlation_ref,))
            if cur.fetchone() is not None:
                return SettlementEvidence(correlation_ref, APPLIED, self.readback_mode != "non_authoritative",
                                          "SYNTH-PROVIDER-APPLIED", authenticated=True)
            cur.execute(f"SELECT status, evidence_ref FROM {self._s}.cee_synth_provider_settlement"
                        f" WHERE correlation_ref = %s", (correlation_ref,))
            r = cur.fetchone()
            auth = self.readback_mode != "non_authoritative"
            if r is None:
                return SettlementEvidence(correlation_ref, UNKNOWN, auth, None, authenticated=True)
            return SettlementEvidence(correlation_ref, PENDING if r["status"] == "PENDING" else SETTLED_ABSENT, auth,
                                      r["evidence_ref"], authenticated=True)
        return self._tx.run(body)

    # -- test inspection (not part of any executor-facing port) ------------------------------------------------------------
    def counts(self, correlation_ref: "str | None" = None) -> dict:
        def body(cur: Any) -> dict:
            where, params = ("", ()) if correlation_ref is None else (" WHERE correlation_ref = %s", (correlation_ref,))
            out = {}
            for key, table in (("calls", "cee_synth_provider_call"), ("applied", "cee_synth_provider_applied")):
                cur.execute(f"SELECT count(*) AS n FROM {self._s}.{table}{where}", params)
                out[key] = int(cur.fetchone()["n"])
            return out
        return self._tx.run(body)

    def snapshot(self, target_identity: str) -> dict:
        def body(cur: Any) -> dict:
            cur.execute(f"SELECT version, artifact_version FROM {self._s}.cee_synth_provider_target"
                        f" WHERE target_identity = %s", (target_identity,))
            t = cur.fetchone()
            return {"version": t["version"], "artifact_version": t["artifact_version"]}
        return self._tx.run(body)
