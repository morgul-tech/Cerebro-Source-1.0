"""Shared fixture: a labelled SYNTHETIC world (owner + provider + store + executor + fake clock). No I/O."""
from __future__ import annotations

import os
import sys

sys.dont_write_bytecode = True
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from controlled_effect_executor import (ApprovalView, BatchSpec, ControlledEffectExecutor, DelegationView,  # noqa: E402
                                        InMemoryAdmissionStore, Operation, TargetScopeEntry)
from controlled_effect_executor.synthetic import FakeClock, SyntheticOwner, SyntheticProvider  # noqa: E402

T0 = 1_000_000
TARGET = "SYNTH-TARGET-A"
ART_OLD, ART_NEW = "SYNTH-ARTIFACT-V1", "SYNTH-ARTIFACT-V2"
OPS = (Operation.of("SYNTH-OP-SET", key="alpha", value=1), Operation.of("SYNTH-OP-TAG", tag="beta"))


def default_spec(**over) -> BatchSpec:
    base = dict(
        work_order_ref="SYNTH-WO-1", effect_ref="SYNTH-EFFECT-1", operations=OPS, target_identity=TARGET,
        target_precondition_version="3", artifact_version=ART_NEW, actor_ref="SYNTH-ACTOR-1", actor_generation=4,
        delegation_ref="SYNTH-DELEG-1", delegation_revision=1, delegation_expiry=T0 + 3600,
        human_approval_ref="SYNTH-APPROVAL-1", idempotency_key="SYNTH-IDEM-1", rollback_ref=None)
    base.update(over)
    return BatchSpec(**base)


class World:
    def __init__(self) -> None:
        self.clock = FakeClock(T0)
        self.owner = SyntheticOwner()
        self.provider = SyntheticProvider({TARGET: ("3", ART_OLD)})
        self.store = InMemoryAdmissionStore(self.owner, clock=self.clock)
        self.executor = ControlledEffectExecutor(
            store=self.store, provider=self.provider.adapter, readback=self.provider.readback, clock=self.clock)
        self.owner.put_delegation(DelegationView(
            delegation_ref="SYNTH-DELEG-1", delegation_revision=1, status="ACTIVE", actor_ref="SYNTH-ACTOR-1",
            actor_generation=4, allowed_operations=frozenset({"SYNTH-OP-SET", "SYNTH-OP-TAG"}),
            target_scope=(TargetScopeEntry(TARGET, frozenset({ART_NEW})),), expires_at=T0 + 3600,
            owner_currentness=0))
        self.approve(default_spec())

    def approve(self, spec: BatchSpec, **over) -> None:
        fields = dict(approval_ref=spec.human_approval_ref, status="APPROVED", target_identity=spec.target_identity,
                      artifact_version=spec.artifact_version, operations_digest=spec.operations_digest)
        fields.update(over)
        self.owner.put_approval(ApprovalView(**fields))

    def admit(self, spec: "BatchSpec | None" = None, digest: "str | None" = None):
        spec = spec or default_spec()
        return self.store.admit(spec, spec.digest if digest is None else digest)

    def fence(self, spec: "BatchSpec | None" = None):
        spec = spec or default_spec()
        decision = self.admit(spec)
        assert decision.state == "FENCED", decision
        return spec, decision.receipt

    def run(self, spec: "BatchSpec | None" = None):
        spec, receipt = self.fence(spec)
        return spec, receipt, self.executor.execute(receipt, spec)
