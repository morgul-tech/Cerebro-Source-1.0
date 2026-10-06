"""Child adapter process for restart/crash fixture tests: one explicit command per process, JSON on stdout.

python -B -m d1js.child --state DIR --url nats://127.0.0.1:PORT --gateway-root DIR --cmd CMD [--faults JSON]
                         [--config JSON] [--owner-event-key KEY]
CMD: commit (owner commit + outbox intent, no publish) | publish (INTENDED + recoverable ambiguous intents) |
     commit_publish | drain | reconcile | status
A fault mode ``exit`` makes this process die at that exact seam (exit 86) to model a crash.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .composition import D1Fixture
from .config import D1Config
from .faults import Faults
from .nats_transport import NatsJetStreamTransport


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--url", required=True)
    ap.add_argument("--gateway-root", required=True, type=Path)
    ap.add_argument("--cmd", required=True, choices=["commit", "publish", "commit_publish", "drain", "reconcile",
                                                     "status"])
    ap.add_argument("--faults", default="")
    ap.add_argument("--config", default="{}")
    ap.add_argument("--owner-event-key", default="evt-syn-1")
    a = ap.parse_args(argv)
    cfg = D1Config(enabled=True, server_url=a.url, **json.loads(a.config)).validate()
    faults = Faults.from_json(a.faults)
    transport = NatsJetStreamTransport(a.url, name="d1js-child").connect()
    out: dict = {"cmd": a.cmd}
    try:
        fx = D1Fixture(gateway_root=a.gateway_root, state_dir=a.state, transport=transport, config=cfg,
                       faults=faults)
        if a.cmd in ("commit", "commit_publish"):
            facts = fx.commit_hint(owner_event_key=a.owner_event_key)
            out["commit"] = {k: v for k, v in facts.items() if k != "pointer"}
        if a.cmd in ("publish", "commit_publish"):
            out["publish"] = {r["message_id"]: fx.publisher.publish(r["message_id"])      # incl. recovery
                              for r in fx.owner.outbox()
                              if r["state"] in ("INTENDED", "SEND_CLAIMED", "UNKNOWN_PENDING", "RECONCILE_HOLD")}
        if a.cmd == "drain":
            rep = fx.receiver.drain_once()
            out["drain"] = {"reason": rep.reason, "outcomes": [
                {"gateway": o.gateway.disposition, "gateway_transit": o.gateway.transit_state,
                 "d1": o.d1_disposition, "transit": o.transit_state, "obligation": o.unresolved_obligation,
                 "delivery_id": o.delivery_id, "stream_seq": o.stream_seq, "num_delivered": o.num_delivered,
                 "receipt_ref": o.receipt_ref, "port_event": o.port_event} for o in rep.outcomes]}
        if a.cmd == "reconcile":
            out["reconcile"] = fx.reconcile_once()
        out["materializations"] = fx.owner.materializations()
        out["publish_calls"], out["lookup_calls"] = fx.publisher.publish_calls, fx.publisher.lookup_calls
        out["outbox"] = [{k: r[k] for k in ("message_id", "state", "stream_seq", "attempts", "last_reason")}
                         for r in fx.owner.outbox()]
        out["fired"] = faults.fired
    finally:
        transport.close()
    sys.stdout.write(json.dumps(out, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
