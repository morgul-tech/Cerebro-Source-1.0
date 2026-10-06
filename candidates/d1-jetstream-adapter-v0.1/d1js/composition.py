"""Thin composition: historical D1HybridGateway + JetStream PullAckPort + lease + synthetic owner/return ports.

Public operations (all bounded, explicit calls -- no resident loop, no scheduler):
  D1Fixture.open(...)                  -> wire one receiver/one stream (default OFF unless config.enabled)
  fixture.commit_hint(...)             -> SYNTHETIC owner commit + outbox intent (owner-side seam), returns ids
  fixture.publisher.publish(message_id)-> PubAck recorded before optional wake
  fixture.receiver.drain_once()        -> at a safe idle boundary: lease, bounded pull, gateway, disposition, ACK
  fixture.reconcile_once()             -> owner-visible obligations (unqueued/unknown/expired/max-delivery/...)
  fixture.metrics()                    -> counters/bytes/time only
Each drain item returns D1Outcome: the gateway's ORIGINAL GatewayResult plus the D1 disposition, the transit ACK
state and the unresolved-obligation marker, kept separate. ACK is transit only; never work or effect.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .boundary import LeasedBoundaryReader, LeaseLost, SyntheticSafeBoundaryHost
from .config import D1Config
from .faults import Faults
from .journal import ReceiverJournal
from .owner import (OUTBOX_CAPACITY, OUTBOX_CONFLICT, OUTBOX_INTENDED, OUTBOX_PUBLISHED, OUTBOX_RECONCILE_HOLD,
                    OUTBOX_SEND_CLAIMED, OUTBOX_UNKNOWN, SyntheticProjectOwner)
from .owner_ports import ProjectReturnMapping, SyntheticOwnerEventReader
from .pointer import build_pointer, message_identity, parse_ts
from .port import SETTLING, JetStreamPullAckPort, PortEvent, d1_disposition
from .publisher import OwnerOutboxPublisher
from .reference_root import load_gateway
from .transport import StreamTransport

TENANT, WORKSPACE, PROJECT, RECEIVER = "tenant-syn-1", "workspace-syn-1", "project-syn-1", "receiver-syn-1"
END_TO_END_UNPROVEN = "LOCAL_TRANSPORT_PRESERVED_BUT_OWNER_END_TO_END_NO_LOSS_UNPROVEN"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class D1Outcome:
    gateway: Any                        # the gateway's original GatewayResult (status retained unchanged)
    d1_disposition: str | None
    transit_state: str
    unresolved_obligation: str          # SETTLED_FOR_RECEIVER | OPEN:<reason> | NONE
    delivery_id: str | None = None
    stream_seq: int | None = None
    num_delivered: int | None = None
    message_id: str | None = None
    receipt_ref: str | None = None
    port_event: str | None = None


@dataclass
class DrainReport:
    deferred: bool
    reason: str
    outcomes: list[D1Outcome] = field(default_factory=list)
    latency_ms: float = 0.0


class D1Receiver:
    def __init__(self, *, gw: Any, transport: StreamTransport, journal: ReceiverJournal, owner: SyntheticProjectOwner,
                 host: SyntheticSafeBoundaryHost, config: D1Config, faults: Faults,
                 clock: Callable[[], datetime] = utcnow, receiver_ref: str = RECEIVER,
                 interests: frozenset | None = None) -> None:
        self._gw, self._t, self.journal, self.owner, self.host = gw, transport, journal, owner, host
        self.config, self.faults, self.clock, self.receiver_ref = config, faults, clock, receiver_ref
        self.owner_reader = SyntheticOwnerEventReader(gw, owner, faults=faults)
        self.return_mapping = ProjectReturnMapping(gw, owner, faults=faults)
        self.port = JetStreamPullAckPort(gw=gw, transport=transport, journal=journal, owner_reader=self.owner_reader,
                                         return_mapping=self.return_mapping, config=config, receiver_ref=receiver_ref,
                                         clock=clock, faults=faults, host=host)
        self.interests = interests or frozenset({(TENANT, WORKSPACE, PROJECT, receiver_ref)})

    def _gateway(self, lease):
        return self._gw.D1HybridGateway(boundary_reader=LeasedBoundaryReader(self._gw, self.host, lease),
                                        pull_ack=self.port, owner_reader=self.owner_reader, dedupe=self.journal,
                                        return_port=self.return_mapping, receiver_ref=self.receiver_ref,
                                        interested_projects=self.interests, enabled=self.config.enabled)

    def drain_once(self, max_items: int | None = None) -> DrainReport:
        """One bounded callable drain at a safe idle boundary. Defers (pulls nothing) under an active Human turn,
        a local effect or a missing lease. Stops early when the lease is lost or the stream is quiet."""
        if not self.config.enabled:
            return DrainReport(True, "DEFAULT_OFF")
        t0 = time.monotonic()
        self._absorb_advisories()
        lease = self.host.acquire()
        if lease is None:
            self.journal.bump("active_turn_deferrals")
            return DrainReport(True, "DEFER_UNSAFE_BOUNDARY")
        self.port.lease = self.owner_reader.lease = self.return_mapping.lease = lease
        self.owner_reader.memo.clear()                       # coalescing memo is per drain + lease epoch only
        report = DrainReport(False, "DRAINED")
        try:
            for _ in range(max_items or self.config.drain_max_items):
                if not lease.valid():
                    report.reason = "STOPPED_LEASE_LOST"
                    self.journal.bump("active_turn_deferrals")
                    break
                before = len(self.port.events)
                result = self._gateway(lease).consume_one()
                ev = self.port.events[before:]
                outcome = self._outcome(result, ev[-1] if ev else None)
                report.outcomes.append(outcome)
                if result.disposition == "QUIET_NO_DELIVERY" and not ev:
                    report.reason = "QUIET"
                    break
                if result.disposition in ("DEFER_UNSAFE_BOUNDARY", "HOLD_PULL_UNAVAILABLE", "HOLD_SAFE_BOUNDARY_UNKNOWN"):
                    report.reason = "STOPPED_" + result.disposition
                    break
        finally:
            lease.release()
            self.port.lease = self.owner_reader.lease = self.return_mapping.lease = None
            self.port.inflight.clear()
            self._absorb_advisories()
        report.latency_ms = (time.monotonic() - t0) * 1000.0
        self.journal.sample("drain_latency_ms", report.latency_ms)
        return report

    def _outcome(self, result: Any, ev: PortEvent | None) -> D1Outcome:
        if ev is None:
            d1 = d1_disposition(result.disposition) if result.delivery_id else None
            return D1Outcome(result, d1, result.transit_state, "NONE", result.delivery_id)
        d1 = ev.d1_disposition
        transit = ev.transit_state
        if ev.kind == "GATEWAY" and result.transit_state not in ("ACKED", "NOT_TOUCHED") and transit == "ACKED":
            transit = result.transit_state
        settled = d1 in SETTLING and transit == "ACKED"
        if ev.kind == "DEFER":
            obligation = "OPEN:DEFERRED_PENDING_IN_STREAM"
        elif settled:
            obligation = "SETTLED_FOR_RECEIVER"
        else:
            obligation = f"OPEN:{d1}" if d1 else "OPEN:UNKNOWN"
        return D1Outcome(result, d1, transit, obligation, ev.delivery_id, ev.stream_seq, ev.num_delivered,
                         ev.message_id, ev.receipt_ref, ev.kind)

    def _absorb_advisories(self) -> None:
        try:
            for adv in self._t.poll_advisories():
                self.journal.record_advisory(adv)
                self.journal.bump("max_delivery_advisories")
        except Exception:
            self.journal.bump("advisory_poll_failures")


class D1Fixture:
    """One adapter / one receiver / one disposable stream, wired from explicit reference roots and paths."""

    def __init__(self, *, gateway_root: Path, state_dir: Path, transport: StreamTransport, config: D1Config,
                 faults: Faults | None = None, host: SyntheticSafeBoundaryHost | None = None,
                 clock: Callable[[], datetime] = utcnow, wake: Callable[[str, int], None] | None = None) -> None:
        self.gw = load_gateway(gateway_root)
        self.config = config.validate()
        self.faults = faults or Faults()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.owner = SyntheticProjectOwner(self.state_dir / "owner.sqlite", faults=self.faults)
        self.journal = ReceiverJournal(self.state_dir / "receiver.sqlite", faults=self.faults)
        self.transport = transport
        self.host = host or SyntheticSafeBoundaryHost()
        self.clock = clock
        self.ensure_info = transport.ensure(self.config) if config.enabled else None
        self.publisher = OwnerOutboxPublisher(owner=self.owner, transport=transport, config=self.config,
                                              faults=self.faults, counters=self.journal, wake=wake, clock=clock)
        self.receiver = D1Receiver(gw=self.gw, transport=transport, journal=self.journal, owner=self.owner,
                                   host=self.host, config=self.config, faults=self.faults, clock=clock)

    # ------------------------------------------------------------------ owner side (synthetic)
    def commit_hint(self, *, owner_event_key: str = "evt-syn-1", material: bool = True, kind: str = "HINT",
                    referent_type: str = "doc", referent_id: str = "doc-syn-1", payload: dict | None = None,
                    with_outbox: bool = True, receiver_ref: str = RECEIVER, tenant_ref: str = TENANT,
                    workspace_ref: str = WORKSPACE, project_ref: str = PROJECT,
                    pointer_overrides: dict | None = None) -> dict[str, Any]:
        """Owner commit + outbox intent in ONE owner transaction (the owner-side seam). The pointer bytes are fixed
        before the commit so the outbox holds the exact digest that will be published."""
        payload = payload or {"material": "synthetic owner material", "key": owner_event_key}
        revision = self.owner.peek_next_revision(tenant_ref, workspace_ref, project_ref)
        fingerprint = self.owner.fingerprint_for(owner_event_key=owner_event_key, owner_revision=revision,
                                                 payload=payload, material=material, kind=kind)
        created = self.clock()
        fields = dict(tenant_ref=tenant_ref, workspace_ref=workspace_ref, project_ref=project_ref,
                      owner_ref="PROJECT_ENGINE", owner_event_key=owner_event_key, owner_revision=revision,
                      event_fingerprint=fingerprint, referent_type=referent_type, referent_id=referent_id,
                      receiver_ref=receiver_ref, kind=kind, created_at=created, ttl_seconds=self.config.pointer_ttl_s)
        fields.update(pointer_overrides or {})
        raw = build_pointer(**fields)
        msg_id = message_identity(fields["tenant_ref"], fields["workspace_ref"], fields["project_ref"],
                                  fields["receiver_ref"], owner_event_key, fields["owner_revision"])
        import json as _json
        doc = _json.loads(raw)
        outbox = [(msg_id, receiver_ref, raw, doc["created_at"], doc["expires_at"])] if with_outbox else None
        facts = self.owner.commit_event(tenant_ref=tenant_ref, workspace_ref=workspace_ref, project_ref=project_ref,
                                        owner_event_key=owner_event_key, material=material, kind=kind,
                                        referent_type=referent_type, referent_id=referent_id, payload=payload,
                                        outbox=outbox)
        if facts["owner_revision"] != fields["owner_revision"] and not pointer_overrides:
            raise RuntimeError("owner revision moved between pointer build and commit")
        return {**facts, "message_id": msg_id, "pointer": raw, "with_outbox": with_outbox}

    # ------------------------------------------------------------------ owner-visible reconciliation
    def reconcile_once(self, *, publish_unqueued: bool = True) -> list[dict[str, Any]]:
        """Bounded: every outbox obligation not settled for the receiver is returned with its exact reason.
        INTENDED (unqueued) intents are published once; SEND_CLAIMED/UNKNOWN/HOLD ones go through bounded lookup
        only. One LookupBudget (lookup_bound calls, lookup_time_budget_s) is shared by every row of this call.
        Default OFF: owner-visible obligations are listed from the owner outbox only -- no transport call, no
        publication-state change."""
        enabled = self.config.enabled
        if enabled:
            self.receiver._absorb_advisories()
        budget = self.publisher.new_budget()
        out: list[dict[str, Any]] = []
        now = self.clock()
        exhausted = {a["stream_seq"] for a in self.journal.advisories()}
        for row in self.owner.outbox()[: self.config.lookup_bound]:
            terminals = self.journal.terminal_for_message(row["message_id"])
            if any(t["d1_disposition"] in SETTLING and t["pointer_sha256"] == row["pointer_sha256"] for t in terminals):
                continue
            expired = now >= parse_ts(row["expires_at"])
            state = row["state"]
            if not enabled:
                reason = "D1_DISABLED_OBLIGATION_OPEN:" + state
            elif state == OUTBOX_INTENDED:
                if expired:
                    reason = "EXPIRED_UNQUEUED"
                elif publish_unqueued:
                    state = self.publisher.publish(row["message_id"], budget=budget)
                    reason = "UNQUEUED_RECONCILED_PUBLISH:" + state
                else:
                    reason = "UNQUEUED"
            elif state in (OUTBOX_SEND_CLAIMED, OUTBOX_UNKNOWN, OUTBOX_RECONCILE_HOLD):
                if publish_unqueued:
                    state = self.publisher.reconcile_publication(row["message_id"], budget=budget)
                    reason = "PUBLICATION_" + state
                else:                                            # metrics/read-only: never a (re)publication
                    reason = "PUBLICATION_UNRESOLVED:" + state
            elif state == OUTBOX_CAPACITY:
                reason = "CAPACITY_REJECTED_OPEN"
            elif state == OUTBOX_CONFLICT:
                reason = "CONFLICT_HOLD"
            else:
                seq = row["stream_seq"]
                negative = [t["d1_disposition"] for t in terminals if t["d1_disposition"] not in SETTLING]
                if negative:
                    reason = "NEGATIVE_DISPOSITION_OBLIGATION_OPEN:" + ",".join(negative)
                elif seq in exhausted or self.journal.max_delivered(self.config.stream, seq) >= self.config.max_deliver:
                    reason = "MAX_DELIVERY_EXHAUSTED"
                elif expired:
                    reason = "EXPIRED_UNCLOSED"
                elif not budget.take():
                    reason = "PENDING_LOOKUP_BUDGET_EXHAUSTED"
                else:
                    try:
                        present = self.transport.get_msg(seq, timeout=budget.remaining_seconds()) is not None
                        budget.check()  # shared budget: no adoption of a late presence result
                    except Exception:
                        present = None
                    reason = {True: "PENDING_IN_STREAM", False: "MISSING_FROM_STREAM_UNCLOSED",
                              None: "PENDING_LOOKUP_UNAVAILABLE"}[present]
            out.append({"message_id": row["message_id"], "owner_event_key": row["owner_event_key"],
                        "owner_revision": row["owner_revision"], "outbox_state": state, "stream_seq": row["stream_seq"],
                        "reason": reason})
        for key in ("EXPIRED", "MAX_DELIVERY", "CAPACITY"):
            n = len([o for o in out if key in o["reason"]])
            if n:
                self.journal.bump(f"reconcile_{key.lower()}_open", 0)
        return out

    def owner_reconciliation_misses(self) -> list[dict[str, Any]]:
        """Receiver dispositions whose message_id the owner outbox does not know (forged/foreign/no-outbox)."""
        known = {r["message_id"] for r in self.owner.outbox()}
        misses = []
        for d in self._all_terminals():
            if d["message_id"] not in known:
                misses.append(d)
        return misses

    def _all_terminals(self) -> list[dict[str, Any]]:
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(self.journal.path))) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute("SELECT * FROM dispositions ORDER BY stream_seq")]

    def end_to_end_claim(self) -> str:
        """Owner-side no-loss is only claimed when every committed event has an outbox obligation."""
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(str(self.owner.path))) as con:
            events = {r[0] for r in con.execute("SELECT DISTINCT owner_event_key FROM event_history")}
            covered = {r[0] for r in con.execute("SELECT DISTINCT owner_event_key FROM outbox")}
        return "OWNER_OUTBOX_COVERS_EVERY_COMMITTED_EVENT" if events <= covered else END_TO_END_UNPROVEN

    # ------------------------------------------------------------------ metrics (counters/bytes/time only)
    def metrics(self) -> dict[str, Any]:
        c = self.journal.counters()
        open_rows = [r for r in self.reconcile_once(publish_unqueued=False)]
        now = self.clock()
        oldest = None
        if open_rows:
            created = [parse_ts(r["created_at"]) for r in self.owner.outbox()
                       if r["message_id"] in {o["message_id"] for o in open_rows}]
            oldest = (now - min(created)).total_seconds() if created else None
        cons = stream = None                                     # default OFF: metrics never touch the broker
        if self.config.enabled:
            try:
                cons = self.transport.consumer_state()
            except Exception:
                cons = None
            try:
                stream = self.transport.stream_state()
            except Exception:
                stream = None
        lat = self.journal.samples("puback_latency_ms")
        drain = self.journal.samples("drain_latency_ms")
        return {
            "unit_note": "counters, bytes and milliseconds only; no token or whole-arc savings claim",
            "stream": stream, "consumer": cons,
            "pending_obligations": len(open_rows), "oldest_open_obligation_age_s": oldest,
            "redeliveries": c.get("redeliveries", 0),
            "puback_latency_ms": {"n": len(lat), "max": max(lat) if lat else None,
                                  "mean": (sum(lat) / len(lat)) if lat else None},
            "duplicate_pubacks": c.get("duplicate_pubacks", 0), "conflict_rejects": c.get("conflict_rejects", 0),
            "active_turn_deferrals": c.get("active_turn_deferrals", 0) + self.host.deferrals,
            "drain_latency_ms": {"n": len(drain), "max": max(drain) if drain else None},
            "expired": c.get("expired", 0) + len([o for o in open_rows if "EXPIRED" in o["reason"]]),
            "max_delivery": len([o for o in open_rows if o["reason"] == "MAX_DELIVERY_EXHAUSTED"]),
            "max_delivery_advisories": c.get("max_delivery_advisories", 0),
            "capacity_rejects": c.get("capacity_rejects", 0),
            "owner_reconciliation_misses": len(self.owner_reconciliation_misses()),
            "coalesced": c.get("coalesced", 0), "holds": c.get("holds", 0),
            "materializations": self.owner.materializations(),
            "counters": c,
        }
