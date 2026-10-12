"""Child process for the owner-outbox batch: one explicit command, JSON on stdout, may die at a named seam.

python -B fixture/owner_child.py <job.json>
job: {"cmd": "commit_then_die" | "publish", "pg": <ContextPostgres.export_info()>, "commit_args": {...},
      "url": "nats://127.0.0.1:P", "faults": {...}, "config": {...}}
commit_then_die: authenticated PM owner commit through the unchanged Context service, print, os._exit(86) BEFORE
                 any publisher/broker activity (process death after COMMIT).
publish:         compose the internal Context publisher and run ONE bounded publish_pending_once (a fault mode
                 ``exit`` kills the process at that seam with exit 86).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE))

from fixture.context_pg import FixtureTokens, PgInfo  # noqa: E402
from fixture.context_service import ContextServiceFixture, ContextToolClient  # noqa: E402


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    pg = PgInfo(job["pg"])
    fx = ContextServiceFixture(pg, FixtureTokens(), d1_outbox=True)
    out: dict = {"cmd": job["cmd"], "pid": os.getpid()}
    if job["cmd"] == "commit_then_die":
        res = fx.invoke("pm", "commit_pm_owner_ready", job["commit_args"])
        out["record"] = res["structuredContent"]["record"]
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
        os._exit(86)
    if job["cmd"] == "publish":
        from d1js.config import D1Config
        from d1js.faults import Faults
        from d1js.internal_binding import CONTEXT_OWNER_MODE, InternalBinding, compose_context_publisher
        from d1js.nats_transport import NatsJetStreamTransport
        cfg = D1Config(enabled=True, server_url=job["url"], **job.get("config", {})).validate()
        transport = NatsJetStreamTransport(job["url"], name="owner-child").connect()
        try:
            transport.ensure(cfg)
            pub = compose_context_publisher(
                binding=InternalBinding(enabled=True, owner_mode=CONTEXT_OWNER_MODE,
                                        authorization_ref="authz:syn-d1-publisher", role="publisher"),
                config=cfg, transport=transport, client=ContextToolClient(fx, "d1pub"),
                faults=Faults(job.get("faults") or {}))
            out["result"] = pub.publish_pending_once(limit=8)
            out["publish_calls"], out["lookup_calls"] = pub.publisher.publish_calls, pub.publisher.lookup_calls
        finally:
            transport.close()
        sys.stdout.write(json.dumps(out) + "\n")
        return 0
    raise SystemExit("UNKNOWN_CMD")


if __name__ == "__main__":
    sys.exit(main())
