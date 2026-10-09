"""Thin Signalvev caller for the versioned, service-owned Context notice bridge.

An authenticated host supplies one current PM or X9 MCP call port per
operation. This module never reads a token, joins server packages, assigns a
role, deposits custody itself, or consumes X9.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from signalvev_sensing.model import canonical, sha256_hex
from signalvev_sensing.resolver import ResolveRequest, ResolverResult, ResolverUnavailable

from .config import ClientConfig
from .pm_x9 import MODE_PRODUCTION, PM_READY_HINT, PmX9Settings
from .session import SendClient

PREPARE = "prepare_signalvev_notice_v1"
OBSERVE = "transport_resolve_signalvev_notice_v1"
PREPARE_SCHEMA = "cerebro-signalvev-notice-preparation/v1"
OBSERVE_SCHEMA = "cerebro-signalvev-transport-resolution/v1"


class ContextNoticeError(ValueError):
    pass


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise ContextNoticeError(code)


def _content(value: Any) -> Mapping[str, Any]:
    # The host's authenticated MCP call port returns the service result. No
    # JSON/file/CLI input is accepted as a provider result by this module.
    _need(isinstance(value, Mapping), "CONTEXT_SERVICE_RESULT_REQUIRED")
    result = value.get("structuredContent", value)
    _need(isinstance(result, Mapping), "CONTEXT_STRUCTURED_CONTENT_REQUIRED")
    return result


@dataclass(frozen=True)
class PreparedNotice:
    receipt_ref: str
    event_id: str
    pointer_sha256: str
    raw_event: dict[str, Any]


def parse_preparation(result: Any, *, receipt_ref: str, settings: PmX9Settings) -> PreparedNotice:
    """Check the exact service response before any NATS send."""
    _need(type(settings) is PmX9Settings and settings.mode == MODE_PRODUCTION,
          "PRODUCTION_SETTINGS_REQUIRED")
    _need(isinstance(receipt_ref, str) and receipt_ref.startswith("pm-receipt:"),
          "RECEIPT_REF_REQUIRED")
    value = _content(result)
    _need(value.get("schema") == PREPARE_SCHEMA and value.get("state") == "DEPOSITED_READBACK",
          "CONTEXT_PREPARATION_NOT_DEPOSITED")
    _need(value.get("subject") == "cerebro.v1.artifact.pointer", "CONTEXT_SUBJECT_MISMATCH")
    record, projection = value.get("record"), value.get("projection")
    _need(isinstance(record, Mapping) and isinstance(projection, Mapping),
          "CONTEXT_OWNER_RECORD_REQUIRED")
    _need(record.get("owner_ref") == settings.owner_ref,
          "CONTEXT_RECEIPT_OWNER_MISMATCH")
    _need(all(isinstance(record.get(key), str) and record[key] for key in
              ("claim_ref", "packet_ref", "queue_ref", "packet_sha256", "commit_ref")),
          "CONTEXT_RECEIPT_COORDINATES_REQUIRED")
    _need(record.get("receipt_ref") == receipt_ref and record.get("ready_state") == "MATERIAL_READY"
          and value.get("event_id") == record.get("event_id"), "CONTEXT_RECEIPT_EVENT_MISMATCH")
    snapshot_sha = record.get("snapshot_sha256")
    _need(isinstance(snapshot_sha, str) and len(snapshot_sha) == 64 and
          sha256_hex(canonical({k: v for k, v in record.items() if k != "snapshot_sha256"})) == snapshot_sha,
          "CONTEXT_RECEIPT_HASH_MISMATCH")
    pointer_sha = value.get("pointer_sha256")
    _need(isinstance(pointer_sha, str) and len(pointer_sha) == 64 and
          all(c in "0123456789abcdef" for c in pointer_sha), "CONTEXT_POINTER_READBACK_REQUIRED")
    _need(isinstance(projection.get("material_sha256"), str) and
          isinstance(projection.get("source_cut"), str), "CONTEXT_MATERIAL_PROJECTION_REQUIRED")
    _need(isinstance(record.get("event_id"), str) and record["event_id"].startswith("pm-event:")
          and isinstance(record.get("referent_id"), str) and record["referent_id"].startswith("pm-ready:")
          and type(record.get("owner_seq")) is int and record["owner_seq"] >= 1,
          "CONTEXT_EVENT_IDENTITY_INVALID")
    _need(isinstance(record.get("way_home"), list) and bool(record["way_home"])
          and all(isinstance(x, str) and x for x in record["way_home"]),
          "CONTEXT_WAY_HOME_INVALID")
    raw = {
        "event_id": record["event_id"], "owner_ref": record["owner_ref"],
        "source_ref": record["owner_ref"],
        "referent": {"type": PM_READY_HINT, "id": record["referent_id"]},
        "owner_seq": record["owner_seq"],
        "revision_basis": {"after": record["revision_after"], "before": record.get("revision_before")},
        "change_class": "SEMANTIC",
        "delta": {"kind": "POINTER", "ref": record["snapshot_ref"],
                  "expected_sha256": snapshot_sha},
        "commit": {"state": "COMMITTED_READBACK", "readback_ref": record["readback_ref"],
                   "observed_at": record["observed_at"]},
        "way_home": list(record["way_home"]), "requires_ack": False,
    }
    return PreparedNotice(receipt_ref, record["event_id"], pointer_sha, raw)


class PMNoticePublisher:
    """One PM session; host injects its current authenticated Context call port."""

    def __init__(self, *, settings: PmX9Settings, call_pm: Callable[[str, dict[str, str]], Any],
                 client_config: ClientConfig, connect_fn: Callable[..., Any] | None = None):
        _need(callable(call_pm) and isinstance(client_config, ClientConfig), "HOST_PM_PORT_REQUIRED")
        self.settings, self.call_pm = settings, call_pm
        self.sender = SendClient(client_config, connect_fn=connect_fn)

    def send_receipt(self, receipt_ref: str) -> Any:
        prepared = parse_preparation(self.call_pm(PREPARE, {"receipt_ref": receipt_ref}),
                                     receipt_ref=receipt_ref, settings=self.settings)
        self.sender.connect()
        try:
            return self.sender.send(prepared.raw_event)
        finally:
            self.sender.close()


class RuntimeContextResolver:
    """D0 resolver over one fresh, separately scoped local-runtime service read."""

    description = "RUNTIME_CONTEXT_NOTICE_RESOLVER_V1"

    def __init__(self, *, owner_ref: str,
                 call_runtime: Callable[[str, dict[str, str]], Any]):
        _need(isinstance(owner_ref, str) and owner_ref and callable(call_runtime), "HOST_RUNTIME_PORT_REQUIRED")
        self.owner_ref, self.call_runtime = owner_ref, call_runtime

    def resolve(self, req: ResolveRequest) -> ResolverResult:
        if req.owner_ref != self.owner_ref or req.referent_type != PM_READY_HINT or not req.pointer_ref:
            raise ResolverUnavailable("SIGNALVEV_HINT_BINDING_MISMATCH")
        try:
            value = _content(self.call_runtime(OBSERVE, {"event_id": req.event_id}))
            _need(value.get("schema") == OBSERVE_SCHEMA and value.get("state") == "SAME"
                  and value.get("event_id") == req.event_id, "X9_OBSERVATION_UNAVAILABLE")
            pointer, cut = value.get("pointer"), value.get("pm_current_cut")
            _need(isinstance(pointer, Mapping) and isinstance(cut, Mapping), "X9_OBSERVATION_INVALID")
            _need(value.get("pointer_sha256") == sha256_hex(canonical(pointer)),
                  "RUNTIME_POINTER_HASH_MISMATCH")
            _need((pointer.get("event_id"), pointer.get("owner_ref"), pointer.get("referent_id"),
                   pointer.get("revision"), pointer.get("expected_sha256"), pointer.get("owner_seq")) ==
                  (req.event_id, req.owner_ref, req.referent_id, req.expected_revision,
                   req.expected_sha256, req.owner_seq), "X9_POINTER_EVENT_MISMATCH")
            _need(cut.get("owner_ref") == req.owner_ref and cut.get("revision") == req.expected_revision
                  and cut.get("relation_to_hint") == "SAME"
                  and isinstance(cut.get("packet_sha256"), str)
                  and cut.get("authenticated") is True and cut.get("committed_readback") is True
                  and cut.get("material_ready") is True, "X9_CURRENT_PM_MISMATCH")
            return ResolverResult(
                source_ref=req.owner_ref, referent_type=req.referent_type, referent_id=req.referent_id,
                current_revision=req.expected_revision, revision_relation="SAME",
                observed_sha256=req.expected_sha256, owner_seq=req.owner_seq,
                grounding={"snapshot_ref": req.pointer_ref, "source_cut": cut["source_cut"],
                           "ready_state": "MATERIAL_READY"},
            )
        except (ContextNoticeError, KeyError, TypeError, ValueError) as exc:
            raise ResolverUnavailable("X9_CONTEXT_OBSERVATION_UNAVAILABLE") from exc
