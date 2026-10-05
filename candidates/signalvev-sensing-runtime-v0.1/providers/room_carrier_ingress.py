"""Default-off Room C adapter for the INSTALLED ListenClient sink and Codex app.

Executable route (no new endpoint, service, scheduler or truth store):
  qualified private receiver -> ListenClient's ClosureLogSink + this extra sink
  -> same existing closures.jsonl -> stopped-listener reconciliation in an
  authenticated agent-side Codex session -> send_message_to_thread once.

The only app carrier is the host-owned C3 mapping below. An event/closure cannot
choose an actor or supply authority. Episode is PM-frozen HOST configuration:
its d0_sha256 pins the entire existing D0, including source_ref and Way Home;
its case belongs in that D0's Way Home, not a new wire protocol field. This
adapter checks the existing cursor CLAIM/FINAL and closure readback, not a
manually pasted signal. Transport evidence proves the configured connection,
not publisher identity; existing private custody/credential/ACL qualification
is still required before enabling. Hashes do not authenticate a publisher.

Install/start recipe after PM binds the exact episode effect:
1. Existing custodian selects the qualified private receiver host, authenticates
   its existing owner resolver and scopes its existing NATS credential/ACL.
   Put this module on that host's Python path alongside signalvev-client0.1.0.
   Freeze episode.json from the ACTUAL artifact/event, with all Episode fields.
   Do not use synthetic resolver or guess artifact/payload hashes.
2. With the existing receiver config and separate Room C evidence directory:
   python room_carrier_ingress.py listen --config receiver.toml
     --episode episode.json --enable --duration 30
   This is a bounded existing listener (one frame), never a publisher. Default
   without --enable is refusal. The existing receiver lock covers both logs.
3. In X4's authenticated Codex support session, use read_thread for EXACT C3.
   Make app-status.json {thread_id:C3, state:idle|active|unavailable}. Execute
   prepare with --app-status app-status.json and the same host-owned config.
   The receiver must be stopped. No caller-supplied event enqueue operation.
   The existing owner resolver makes one fresh selective reread before intent.
4. Only if prepare returns APP_REQUEST, invoke the supported agent-side tool:
   await tools.mcp__codex_app__send_message_to_thread(request.arguments)
   request.arguments is the exact JSON returned by prepare, including pinned
   threadId and an ids/hashes-only prompt. This tool is NOT an EDGE HTTP API.
   Record the real tool-result SHA256 with finish --outcome APP_ACCEPT on
   verified acceptance, else UNKNOWN_APP_ACCEPT or APP_REJECT. Never resubmit
   an APP_INTENT, even if the support turn crashes before finish. Acceptance
   is not C3's consume, source read, work or return; those remain actor-owned.

Recovery: active/unavailable C3 keeps RECEIVE_PENDING in the existing log.
On a later existing support turn repeat only prepare with fresh app status;
expiry or owner mismatch closes it without waking. APP_INTENT/unknown blocks
replay; reconcile actual app evidence with PM, never infer rejection. A sink
failure does not reopen core dedupe: examine existing cursor/closures, no send
replay and no invented RECEIVE. No periodic job is introduced by this adapter.
Rollback: stop the bounded listener and omit its extra sink/--enable; retain
cursor/closure/outbox records. Do not delete dedupe or replay old events.

Current support surface: X4 Codex Local session
01a0d642-397d-7112-9f70-4d2851ff1d07 can read the real C3 app carrier. The
qualified transport chain is Liv10.77.0.2 -> EDGE10.77.0.1:4222. Co-location or
an existing custody-preserving evidence transfer to that support session must
be qualified by its custodian; this module does not invent that port. No live
install, subscription, app call or send is implied by the offline candidate.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from signalvev_client import ListenClient, load_config
from signalvev_client.lock import EvidenceLock
from signalvev_client.session import load_resolver
from signalvev_sensing import ClosureRecord, JsonlStore
from signalvev_sensing.model import canonical, sha256_hex
from signalvev_sensing.resolver import ResolveRequest, ResolverResult
from signalvev_sensing.store import StoreCorrupt

C3 = "6ac005d2-dd24-83eb-a5b0-f2d8298b5310"
CASE = "ROOMC-SIGNALVEV-FIRST-USE-20261005-02_REV2"
SERVER = "10.77.0.1:4222"
_ID = re.compile(r"[A-Za-z0-9_.:@/#=|+-]{1,160}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class RoomBridgeError(ValueError):
    pass


@dataclass(frozen=True)
class Episode:
    case: str
    event_id: str
    owner_ref: str
    source_ref: str
    referent_type: str
    artifact_id: str
    revision: str
    sha256: str
    d0_sha256: str
    owner_seq: int
    expires_at: int
    effect_binding_ref: str
    receiver_task_ref: str
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.case != CASE:
            raise RoomBridgeError("WRONG_CASE")
        for k in ("event_id", "owner_ref", "source_ref", "referent_type", "artifact_id", "revision",
                  "effect_binding_ref", "receiver_task_ref"):
            if not isinstance(getattr(self, k), str) or not _ID.fullmatch(getattr(self, k)):
                raise RoomBridgeError("HOST_EPISODE_ID_INVALID")
        if not all(isinstance(h, str) and _SHA.fullmatch(h) for h in (self.sha256, self.d0_sha256)):
            raise RoomBridgeError("HOST_EPISODE_HASH_INVALID")
        if type(self.owner_seq) is not int or self.owner_seq < 1 or type(self.expires_at) is not int or self.expires_at < 1:
            raise RoomBridgeError("HOST_EPISODE_BOUNDS_INVALID")
        if not (type(self.source_refs) is tuple and 1 <= len(self.source_refs) <= 8
                and all(isinstance(s, str) and _ID.fullmatch(s) for s in self.source_refs)):
            raise RoomBridgeError("HOST_SOURCE_REFS_INVALID")

    @property
    def digest(self) -> str:
        return sha256_hex(canonical(asdict(self)))


def _records(path: Path, name: str) -> list[dict[str, Any]]:
    """Read-only checksum validation; never repair/truncate an active receiver log."""
    data = path.read_bytes()
    if not data.endswith(b"\n"):
        raise StoreCorrupt("INCOMPLETE_RECEIVER_EVIDENCE")
    records = [JsonlStore._decode(line) for line in data.splitlines()]
    if not records or any(r is None for r in records) or records[0].get("store") != name:
        raise StoreCorrupt("WRONG_OR_CORRUPT_RECEIVER_EVIDENCE")
    return records


def _latest(records: list[dict[str, Any]], event_id: str) -> dict[str, Any] | None:
    return next((r for r in reversed(records) if r.get("event_id") == event_id
                 and str(r.get("kind", "")).startswith("ROOM_")), None)


def _append(path: Path, record: dict[str, Any]) -> None:
    store = JsonlStore(path, name="closure-log")
    try:
        store.append(record)
    finally:
        store.close()


def _entry(ep: Episode, stage: str, **extra: Any) -> dict[str, Any]:
    return {"kind": "ROOM_RECEIPT", "event_id": ep.event_id, "episode_sha256": ep.digest,
            "stage": stage, "case": ep.case, "carrier_ref": C3, "source_ref": ep.source_ref,
            "artifact_id": ep.artifact_id, "revision": ep.revision, "source_sha256": ep.sha256,
            "d0_sha256": ep.d0_sha256, "work_consumed": False, "effect": "NONE_CLAIMED", **extra}


class _RoomSink:
    """Installed-client callback only; not a manual enqueue or transport API."""

    def __init__(self, cfg: Any, ep: Episode) -> None:
        self.cfg, self.ep = cfg, ep
        self.client: ListenClient | None = None

    def deliver(self, closure: ClosureRecord) -> None:
        ep, cfg = self.ep, self.cfg
        if closure.event_id != ep.event_id:
            return
        if self.client is None:
            raise RoomBridgeError("UNBOUND_RECEIVER_CALLBACK")
        status = self.client.status()
        if (status.get("role"), status.get("state"), status.get("server")) != ("LISTEN", "CONNECTED", SERVER):
            raise RoomBridgeError("PRIVATE_RECEIVER_ORIGIN_UNPROVEN")
        cursor = _records(cfg.evidence_dir / "cursor.jsonl", "receiver-cursor")
        claim = next((r for r in cursor if r.get("kind") == "CLAIM" and r.get("event_id") == ep.event_id), None)
        final = next((r for r in reversed(cursor) if r.get("kind") == "FINAL" and r.get("event_id") == ep.event_id), None)
        path = cfg.evidence_dir / "closures.jsonl"
        rows = _records(path, "closure-log")  # built-in sink fsyncs BEFORE this sink
        logged = next((r for r in reversed(rows) if r.get("kind") == "CLOSURE"
                       and r.get("closure_id") == closure.closure_id), None)
        if not claim or not final or not logged:
            raise RoomBridgeError("RECEIVER_READBACK_INCOMPLETE")
        prior = _latest(rows, ep.event_id)
        if prior and prior.get("episode_sha256") != ep.digest:
            raise RoomBridgeError("HOST_EPISODE_CHANGED")
        valid = (claim.get("d0_hash") == ep.d0_sha256 and claim.get("owner_seq") == ep.owner_seq
                 and final.get("disposition") == "ACK_READ" and closure.disposition == "ACK_READ"
                 and closure.receipt_stage == "READ" and not closure.work_consumed
                 and closure.authority == "NONE" and closure.effect == "NONE_CLAIMED"
                 and (closure.owner_ref, closure.referent_type, closure.referent_id,
                      closure.revision_after, closure.observed_sha256)
                 == (ep.owner_ref, ep.referent_type, ep.artifact_id, ep.revision, ep.sha256)
                 and (logged.get("event_id"), logged.get("observed_sha256"), logged.get("disposition"))
                 == (ep.event_id, ep.sha256, "ACK_READ")
                 and ep.case in closure.way_home)
        if not valid:
            if not prior or prior.get("stage") == "RECEIVE_PENDING":
                _append(path, _entry(ep, "HOLD_RECEIVER_CONFLICT", transport_server=SERVER))
            return
        if prior:  # same event/hash is NOOP, including acceptance-unknown and blocked events
            return
        _append(path, _entry(ep, "RECEIVE_PENDING", closure_id=closure.closure_id,
                             transport_server=SERVER, origin="INSTALLED_LISTEN_CALLBACK",
                             receiver_owner_read=True, actor_source_read=False))


def make_room_listener(cfg: Any, ep: Episode, *, resolver: Any, enabled: bool = False) -> ListenClient:
    """Compose the real installed callback; the enable switch is NOT an authority credential."""
    if enabled is not True:
        raise RoomBridgeError("DEFAULT_OFF")
    if cfg.server not in ("nats://" + SERVER, "tls://" + SERVER) or cfg.resolver_kind != "factory":
        raise RoomBridgeError("QUALIFIED_PRIVATE_CONFIG_REQUIRED")
    if not (len(cfg.interests) == 1 and cfg.interests[0].owner_ref == ep.owner_ref
            and cfg.interests[0].referent_type == ep.referent_type
            and cfg.interests[0].referent_id == ep.artifact_id):
        raise RoomBridgeError("EXACT_ARTIFACT_INTEREST_REQUIRED")
    sink = _RoomSink(cfg, ep)
    client = ListenClient(cfg, resolver=resolver, sink=sink)
    sink.client = client
    return client


def prepare_app_request(cfg: Any, ep: Episode, *, app_status: dict[str, Any], resolver: Any,
                        now: int, enabled: bool = False) -> dict[str, Any]:
    """One stopped-receiver reconcile. Writes intent before returning the exact app request."""
    if enabled is not True:
        raise RoomBridgeError("DEFAULT_OFF")
    if app_status.get("thread_id") != C3:
        raise RoomBridgeError("WRONG_APP_CARRIER")
    if type(now) is not int or now < 1:
        raise RoomBridgeError("HOST_CLOCK_REQUIRED")
    lock = EvidenceLock(cfg.evidence_dir, "receiver").acquire()
    try:
        path = cfg.evidence_dir / "closures.jsonl"
        prior = _latest(_records(path, "closure-log"), ep.event_id)
        if not prior:
            return {"result": "NO_RECEIVED_EVENT"}
        if prior.get("episode_sha256") != ep.digest or prior.get("carrier_ref") != C3:
            raise RoomBridgeError("HOST_BINDING_CHANGED")
        if prior.get("stage") != "RECEIVE_PENDING":
            return {"result": "NO_REPLAY", "stage": prior.get("stage")}
        if now >= ep.expires_at:
            _append(path, _entry(ep, "HOLD_EXPIRED"))
            return {"result": "HOLD_EXPIRED"}
        if app_status.get("state") != "idle":
            return {"result": "PENDING", "reason": "CARRIER_BUSY_OR_UNAVAILABLE"}
        req = ResolveRequest(ep.event_id, ep.owner_ref, ep.referent_type, ep.artifact_id,
                             "POINTER_GROUND", ep.source_ref, ep.revision, ep.sha256, ep.owner_seq)
        read_started = time.monotonic()
        try:
            read = resolver.resolve(req)  # one current owner read, no loop or canonical write
            current = (isinstance(read, ResolverResult) and read.source_ref == ep.owner_ref
                       and read.referent_type == ep.referent_type and read.referent_id == ep.artifact_id
                       and read.revision_relation == "SAME" and read.current_revision == ep.revision
                       and read.observed_sha256 == ep.sha256 and read.owner_seq == ep.owner_seq)
        except Exception:
            current = False
        if not current:
            _append(path, _entry(ep, "HOLD_STALE_OR_UNREADABLE"))
            return {"result": "HOLD_STALE_OR_UNREADABLE"}
        # A slow authenticated owner read cannot extend the frozen event's life.
        if now + math.ceil(time.monotonic() - read_started) >= ep.expires_at:
            _append(path, _entry(ep, "HOLD_EXPIRED"))
            return {"result": "HOLD_EXPIRED"}
        payload = {"case": ep.case, "event_id": ep.event_id, "artifact_id": ep.artifact_id,
                   "source_ref": ep.source_ref, "revision": ep.revision, "sha256": ep.sha256,
                   "d0_sha256": ep.d0_sha256, "source_refs": ep.source_refs,
                   "origin": "INSTALLED_PRIVATE_SIGNALVEV_RECEIVER", "transport_server": SERVER,
                   "closure_id": prior["closure_id"], "effect_binding_ref": ep.effect_binding_ref,
                   "receiver_task_ref": ep.receiver_task_ref, "expires_at": ep.expires_at,
                   "receipt_stage": "RECEIVE", "actor_consume": False, "work_consumed": False}
        prompt = ("ROOM_C_RECEIVED_EVENT " + canonical(payload).decode() + "\n"
                  "Actual receiver evidence above is separate from this app acceptance. Under the exact PM-bound "
                  "C3 task, acknowledge ACTOR_CONSUME naming event_id before your own canonical source read. "
                  "Read exact artifact revision/hash and pinned originals, then independently return "
                  "PASS_SUPPORTED or HOLD_UNSUPPORTED with WORK/RETURN receipts. Do not call ACK or this prompt work.")
        args = {"threadId": C3, "prompt": prompt}
        request_sha = sha256_hex(canonical(args))
        _append(path, _entry(ep, "APP_INTENT", request_sha256=request_sha))
        return {"result": "APP_REQUEST", "tool": "mcp__codex_app__send_message_to_thread",
                "arguments": args, "request_sha256": request_sha}
    finally:
        lock.release()


def finish_app_request(cfg: Any, ep: Episode, *, request_sha256: str, outcome: str,
                       tool_result_sha256: str, enabled: bool = False) -> str:
    """Host records actual tool observation; never emits consume/read/work/effect receipts."""
    if enabled is not True:
        raise RoomBridgeError("DEFAULT_OFF")
    if outcome not in ("APP_ACCEPT", "UNKNOWN_APP_ACCEPT", "APP_REJECT"):
        raise RoomBridgeError("INVALID_APP_OUTCOME")
    if not all(isinstance(s, str) and _SHA.fullmatch(s) for s in (request_sha256, tool_result_sha256)):
        raise RoomBridgeError("INVALID_TOOL_RECEIPT_HASH")
    lock = EvidenceLock(cfg.evidence_dir, "receiver").acquire()
    try:
        path = cfg.evidence_dir / "closures.jsonl"
        prior = _latest(_records(path, "closure-log"), ep.event_id)
        if not prior or prior.get("episode_sha256") != ep.digest:
            raise RoomBridgeError("NO_BOUND_APP_INTENT")
        if prior.get("stage") != "APP_INTENT":
            return "NO_REPLAY"
        if prior.get("request_sha256") != request_sha256:
            raise RoomBridgeError("WRONG_APP_REQUEST_RECEIPT")
        _append(path, _entry(ep, outcome, request_sha256=request_sha256,
                             tool_result_sha256=tool_result_sha256))
        return outcome
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("listen", "prepare", "finish"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--app-status", type=Path)
    parser.add_argument("--request-sha256")
    parser.add_argument("--tool-result-sha256")
    parser.add_argument("--outcome")
    args = parser.parse_args()
    if not args.enable:
        parser.error("DEFAULT_OFF")
    cfg = load_config(args.config)
    data = json.loads(args.episode.read_text(encoding="utf-8"))
    data["source_refs"] = tuple(data["source_refs"])
    ep = Episode(**data)  # unknown fields (carrier/authority overrides) fail closed
    if args.action == "listen":
        if not 1 <= args.duration <= 60:
            parser.error("BOUNDED_DURATION_REQUIRED")
        client = make_room_listener(cfg, ep, resolver=load_resolver(cfg), enabled=True)
        with client:
            result = client.wait(duration=args.duration, max_frames=1)
        print(json.dumps({"result": result, "summary": client.summary()}))
    elif args.action == "prepare":
        if args.app_status is None or cfg.resolver_kind != "factory":
            parser.error("REAL_APP_STATUS_AND_OWNER_RESOLVER_REQUIRED")
        result = prepare_app_request(cfg, ep, app_status=json.loads(args.app_status.read_text(encoding="utf-8")),
                                     resolver=load_resolver(cfg), now=int(time.time()), enabled=True)
        print(json.dumps(result))
    else:
        result = finish_app_request(cfg, ep, request_sha256=args.request_sha256, outcome=args.outcome,
                                    tool_result_sha256=args.tool_result_sha256, enabled=True)
        print(json.dumps({"result": result}))


if __name__ == "__main__":
    main()
