"""Shared support for the BK11 owner-outbox integration batch (real PostgreSQL + real nats-server + nats-py).

Runs ONLY under fixture/run_owner_outbox_batch.py, which owns the disposable cluster and broker. Every test gets a
fresh database carrying the actual selected Context migrations (0001-0008) and a freshly reset disposable stream.
SYNTHETIC (labelled): seeded project/session/role rows, installation route row, per-run JWT key, fault seams.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any

CANDIDATE = Path(__file__).resolve().parents[2]
if str(CANDIDATE) not in sys.path:
    sys.path.insert(0, str(CANDIDATE))

from d1js.config import D1Config  # noqa: E402
from d1js.faults import Faults  # noqa: E402
from d1js.internal_binding import (CONTEXT_OWNER_MODE, InternalBinding, compose_context_publisher,  # noqa: E402
                                   compose_x9_receiver)
from d1js.nats_transport import NatsJetStreamTransport  # noqa: E402
from fixture import owner_runtime as rt  # noqa: E402
from fixture.context_pg import (PROJECT, RECEIVER_REF, ROUTE_REF, TENANT, WORKSPACE, FixtureTokens,  # noqa: E402
                                install_route)
from fixture.context_service import ContextServiceFixture, ContextToolClient  # noqa: E402

FAST = dict(ack_wait_s=1.0, hold_nak_delay_s=0.2, pull_timeout_s=0.5)
_DB_COUNTER = [0]


def require_runtime() -> None:
    if rt.PG is None or rt.BROKER is None:
        raise unittest.SkipTest("UNRUN: real PostgreSQL + broker only via fixture/run_owner_outbox_batch.py")


class CountingTransport:
    """Counts actual broker calls of a real transport (evidence for zero-call / no-blind-retry claims)."""

    def __init__(self, inner: NatsJetStreamTransport) -> None:
        self.inner, self.calls = inner, {}

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if not callable(target) or name.startswith("_"):
            return target

        def wrapped(*a, **k):
            self.calls[name] = self.calls.get(name, 0) + 1
            return target(*a, **k)
        return wrapped

    def total(self) -> int:
        return sum(v for k, v in self.calls.items() if k not in ("close",))


class OwnerCase(unittest.TestCase):
    route_kwargs: dict = {}
    cfg_over: dict = {}

    def setUp(self) -> None:
        require_runtime()
        self.pg, self.broker = rt.PG, rt.BROKER
        _DB_COUNTER[0] += 1
        self.dbname = f"t{_DB_COUNTER[0]:02d}_{self._testMethodName[:40].lower()}"
        self.migration = self.pg.install_context(dbname=self.dbname)
        self.pg.seed()
        if self.route_kwargs is not None:
            install_route(self.pg, **self.route_kwargs)
        self.tokens = FixtureTokens()
        self.fx = ContextServiceFixture(self.pg, self.tokens, d1_outbox=True)
        self.state = Path(rt.WORK) / "state" / self.dbname
        self.state.mkdir(parents=True)
        self.cfg = self.config()
        admin = NatsJetStreamTransport(self.broker.url, name="owner-test-admin").connect()
        admin._cfg = self.cfg
        admin.fixture_reset_stream(self.cfg.stream)
        admin.ensure(self.cfg)
        self.admin_t = admin
        self.addCleanup(admin.close)
        self.ev: dict[str, Any] = {"test": self.id(), "database": self.dbname, "synthetic_labels": [
            "seeded project/session/X9 role rows", "installation route row", "per-run JWT signing key"]}
        self.addCleanup(self._write_evidence)

    # ------------------------------------------------------------------ helpers
    def config(self, **over) -> D1Config:
        return D1Config(enabled=True, server_url=self.broker.url, **{**FAST, **self.cfg_over, **over}).validate()

    def transport(self, name: str) -> NatsJetStreamTransport:
        t = NatsJetStreamTransport(self.broker.url, name=name).connect()
        self.addCleanup(t.close)
        return t

    def sql(self, query: str, params: tuple = (), *, who: str = "admin", fetch: bool = True):
        with self.pg.connect(who) as db:
            cur = db.execute(query, params or None)
            rows = cur.fetchall() if fetch and cur.description else None
            db.commit()
            return rows

    def intents(self) -> list[tuple]:
        return self.sql("""SELECT message_id, event_id, receipt_ref, state, stream_seq, attempts, pointer_sha256,
            route_ref, receiver_ref, owner_seq, last_reason FROM cerebro_pm_owner_d1_outbox ORDER BY enqueued_at""")

    def dispositions(self) -> list[tuple]:
        return self.sql("""SELECT event_id, record_payload->>'disposition', content_sha256, provider_revision
            FROM cerebro_x9_channel_records WHERE record_kind='DISPOSITION' ORDER BY provider_revision""")

    def commit(self, referent: str, idem: str, *, expected: str | None = None, ready: str = "MATERIAL_READY",
               material: str = "synthetic material") -> dict[str, Any]:
        args = self.commit_args(referent, idem, expected=expected, ready=ready, material=material)
        res = self.fx.invoke("pm", "commit_pm_owner_ready", args)
        return res["structuredContent"]["record"]

    @staticmethod
    def commit_args(referent, idem, *, expected=None, ready="MATERIAL_READY", material="synthetic material"):
        # claim/packet/queue refs name the referent (stable across its revisions); bodies vary per commit
        return {"idempotency_key": idem, "referent_id": referent, "claim_ref": "claim:" + referent,
                "packet_ref": "packet:" + referent, "queue_ref": "queue:" + referent, "claim_body": {"syn": idem},
                "packet_body": {"material": {"synthetic": material, "idem": idem}}, "queue_body": {"syn": idem},
                "ready_state": ready, "way_home": ["way:syn-x2"], "expected_current_receipt_ref": expected}

    def deposit(self, record: dict[str, Any]) -> dict[str, Any]:
        """The EXISTING PM->X9 producer step (append_x9_pointer) for this receipt."""
        res = self.fx.invoke("pm", "append_x9_pointer", {"receipt_ref": record["receipt_ref"]})
        out = res["structuredContent"]
        self.assertEqual(out["state"], "DEPOSITED_READBACK", out)
        return out

    def publisher(self, *, client=None, transport=None, faults=None, **cfg_over):
        t = transport or CountingTransport(self.transport("owner-test-publisher"))
        if hasattr(t, "inner"):
            t.inner.ensure(self.config(**cfg_over))
        else:
            t.ensure(self.config(**cfg_over))
        pub = compose_context_publisher(
            binding=InternalBinding(enabled=True, owner_mode=CONTEXT_OWNER_MODE,
                                    authorization_ref="authz:syn-d1-publisher", role="publisher"),
            config=self.config(**cfg_over), transport=t, client=client or ContextToolClient(self.fx, "d1pub"),
            faults=faults or Faults())
        return pub, t

    def receiver(self, name: str = "r1", *, client=None, faults=None, **cfg_over):
        t = self.transport("owner-test-receiver-" + name)
        cfg = self.config(**cfg_over)
        t.ensure(cfg)
        binding = InternalBinding(enabled=True, owner_mode=CONTEXT_OWNER_MODE,
                                  authorization_ref="authz:syn-x9-receiver", role="receiver_only",
                                  receiver_ref=RECEIVER_REF, tenant_ref=TENANT, workspace_ref=WORKSPACE,
                                  project_ref=PROJECT)
        return compose_x9_receiver(binding=binding, config=cfg, transport=t,
                                   client=client or ContextToolClient(self.fx, "x9"),
                                   gateway_root=rt.GATEWAY_ROOT, journal_path=self.state / f"journal-{name}.sqlite",
                                   faults=faults or Faults())

    @staticmethod
    def outcomes(report) -> list[dict[str, Any]]:
        return [{"gateway": o.gateway.disposition, "gateway_transit": o.gateway.transit_state,
                 "d1": o.d1_disposition, "transit": o.transit_state, "obligation": o.unresolved_obligation,
                 "stream_seq": o.stream_seq, "num_delivered": o.num_delivered, "message_id": o.message_id,
                 "receipt_ref": o.receipt_ref, "port_event": o.port_event} for o in report.outcomes]

    def drain(self, receiver, max_items: int = 4) -> list[dict[str, Any]]:
        rep = receiver.drain_once(max_items=max_items)
        out = [o for o in self.outcomes(rep) if not (o["gateway"] == "QUIET_NO_DELIVERY" and o["port_event"] is None)]
        self.ev.setdefault("drains", []).append({"reason": rep.reason, "outcomes": out})
        return out

    def stream(self) -> dict[str, int]:
        return self.admin_t.stream_state(timeout=5)

    def child(self, job: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        path = self.state / f"job-{time.monotonic_ns()}.json"
        path.write_text(json.dumps({**job, "pg": self.pg.export_info()}), encoding="utf-8")
        p = subprocess.run([rt.PYTHON, "-B", str(CANDIDATE / "fixture" / "owner_child.py"), str(path)],
                           cwd=str(CANDIDATE), capture_output=True, text=True, timeout=180)
        path.unlink()
        with open(Path(rt.WORK) / "logs" / "owner_children.log", "a", encoding="utf-8") as fh:
            fh.write(f"=== {self.id()} {job['cmd']} rc={p.returncode}\n{p.stdout}\n{p.stderr[-4000:]}\n")
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        return p.returncode, (json.loads(lines[-1]) if lines else None)

    def _write_evidence(self) -> None:
        d = Path(rt.WORK) / "evidence" / "cases"
        d.mkdir(parents=True, exist_ok=True)
        (d / (self.dbname + ".json")).write_text(json.dumps(self.ev, indent=1, default=str) + "\n", encoding="utf-8")


def pointer_bytes(row: dict[str, Any]) -> bytes:
    p = row["pointer"]
    return p if isinstance(p, bytes) else base64.b64decode(p)


__all__ = ["OwnerCase", "CountingTransport", "pointer_bytes", "ContextToolClient", "FAST", "ROUTE_REF",
           "RECEIVER_REF", "TENANT", "WORKSPACE", "PROJECT", "install_route"]
