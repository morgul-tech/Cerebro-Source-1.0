"""Bindings of the gateway's owner/return ports to the SYNTHETIC Project owner.

* ``SyntheticOwnerEventReader`` -> gateway.ProjectOwnerEventReader (verify_pointer_route / read_current). It re-reads
  ONLY the selected owner event (no whole-world hydration), counts rereads and feeds the per-drain coalescing memo.
* ``ProjectReturnMapping``      -> gateway.DurableReturnPort. Explicit mapping of the reference Project return API:
      append(record)      -> owner.append_and_confirm(... closure_fingerprint=record.fingerprint,
                                                       owner_event_fingerprint=record.event_fingerprint)
                             PASS -> ReturnReceipt(provider_readback_verified=True); CONFLICT_HOLD -> ReturnConflict;
                             HOLD -> ReturnNotClosed (gateway: HOLD_RETURN_NOT_CLOSED / NOACK)
      read_exact(...)     -> owner.read_closure(...) on a NEW connection; owner_revision comes from owner-held history
  The pre-append owner head fence (revision AND last event) is inside append_and_confirm, as in the reference.
Every call first requires the current safe-boundary lease (lost lease => exception => NOACK, no selected work).
"""
from __future__ import annotations

from typing import Any

from .boundary import Lease
from .faults import Faults
from .owner import SyntheticProjectOwner


class ReturnNotClosed(RuntimeError):
    pass


class SyntheticOwnerEventReader:
    SYNTHETIC_TEST_ONLY = True

    def __init__(self, gw: Any, owner: SyntheticProjectOwner, *, faults: Faults) -> None:
        self._gw, self._owner, self.faults = gw, owner, faults
        self.lease: Lease | None = None
        self.memo: dict[tuple[str, str, str], tuple[int, str]] = {}    # (scope3, key) -> (revision, referent)
        self.rereads = 0
        self.route_checks = 0

    def verify_pointer_route(self, delivery: Any) -> bool:
        """Full-pointer route proof from the owner's own committed history (never from a transport flag)."""
        self.route_checks += 1
        if self.lease is not None:
            self.lease.require("owner.route")
        self.faults.hit("owner.route")
        ev = self._owner.event(delivery.owner_event_key)
        if ev is None:
            return False
        rev = self._owner.revision_of(delivery.owner_event_key, delivery.event_fingerprint)
        return (rev == delivery.owner_revision and ev["owner_ref"] == delivery.owner_ref
                and (ev["tenant_ref"], ev["workspace_ref"], ev["project_ref"]) == (
                    delivery.tenant_ref, delivery.workspace_ref, delivery.project_ref))

    def verify_full_pointer(self, pointer: Any) -> bool:
        """Receiver-addressed route proof: the owner's outbox holds exactly these bytes for exactly that receiver."""
        self.route_checks += 1
        self.faults.hit("owner.route")
        rows = self._owner.outbox(pointer.message_id)
        return bool(rows) and rows[0]["receiver_ref"] == pointer.receiver_ref and \
            rows[0]["pointer_sha256"] == pointer.raw_sha256

    def read_current(self, *, tenant_ref: str, workspace_ref: str, project_ref: str, owner_event_key: str):
        self.rereads += 1
        if self.lease is not None:
            self.lease.require("owner.read")
        self.faults.hit("owner.read_current")
        owner = self._owner.read_committed_owner_event(tenant_ref=tenant_ref, workspace_ref=workspace_ref,
                                                       project_ref=project_ref, owner_event_key=owner_event_key)
        ev = self._owner.event(owner_event_key)
        self.memo[(tenant_ref, workspace_ref, project_ref, owner_event_key)] = (owner["owner_revision"],
                                                                                ev["referent_id"])
        return self._gw.ProjectOwnerEvent(
            tenant_ref=tenant_ref, workspace_ref=workspace_ref, project_ref=project_ref, owner_ref=ev["owner_ref"],
            owner_event_key=owner_event_key, owner_revision=owner["owner_revision"],
            event_fingerprint=owner["owner_event_fingerprint"], event_kind=owner["event_state"],
            source_kind="PROJECT_ENGINE_OWNER_COMMIT", currentness=owner["currentness"],
            owner_commit_verified=owner["provider_readback_verified"] is True,
            authenticated_same_owner_readback=owner["authenticated_same_owner_readback"] is True,
            material=bool(ev["material"]))


class ProjectReturnMapping:
    SYNTHETIC_TEST_ONLY = True

    def __init__(self, gw: Any, owner: SyntheticProjectOwner, *, faults: Faults) -> None:
        self._gw, self._owner, self.faults = gw, owner, faults
        self.lease: Lease | None = None
        self.last_result: dict[str, Any] | None = None
        self.appends = 0

    def append(self, record: Any):
        if self.lease is not None:
            self.lease.require("return.append")
        self.faults.hit("return.append")
        self.appends += 1
        res = self._owner.append_and_confirm(
            tenant_ref=record.tenant_ref, workspace_ref=record.workspace_ref, project_ref=record.project_ref,
            receiver_ref=record.receiver_ref, closure_id=record.closure_id, closure_fingerprint=record.fingerprint,
            owner_event_key=record.owner_event_key, owner_event_fingerprint=record.event_fingerprint,
            critical=self.lease.critical if self.lease is not None else None)
        self.last_result = res
        if res["result"] == "CONFLICT_HOLD":
            raise self._gw.ReturnConflict(res["reason"])
        if res["result"] != "PASS":
            raise ReturnNotClosed(res["reason"])
        return self._gw.ReturnReceipt(closure_id=record.closure_id, fingerprint=record.fingerprint,
                                      receipt_ref=res["receipt_ref"], owner_revision=record.owner_revision,
                                      provider_readback_verified=True)

    def read_exact(self, *, tenant_ref: str, workspace_ref: str, project_ref: str, receiver_ref: str,
                   closure_id: str):
        row = self._owner.read_closure(tenant_ref=tenant_ref, workspace_ref=workspace_ref, project_ref=project_ref,
                                       receiver_ref=receiver_ref, closure_id=closure_id)
        if row is None:
            return None
        revision = self._owner.revision_of(row["owner_event_key"], row["owner_event_fingerprint"])
        return self._gw.ReturnReceipt(closure_id=closure_id, fingerprint=row["closure_fingerprint"],
                                      receipt_ref=row["receipt_ref"], owner_revision=revision,
                                      provider_readback_verified=True)

    def verify_receipt(self, receipt_ref: str) -> dict[str, Any] | None:
        """Independent exact receipt check used before CONSUMED_SELECTED_RETURN is journaled."""
        return self._owner.closure_by_receipt(receipt_ref)
