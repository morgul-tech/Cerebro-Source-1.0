"""controlled_effect_executor: local, default-off reference implementation.

OWNER DELEGATION -> ONE-USE ADMISSION FENCE -> CONTROLLED EXECUTOR -> PROVIDER RESULT/UNKNOWN -> READBACK.

STATUS: isolated implementation candidate, authority NONE. Provider-neutral; no network, no credentials, no daemon.
Within this package, ControlledEffectExecutor.execute is the only operation that drives a provider adapter. The
exported ExecutionGrant/require_grant are adapter-side helpers (a grant is one-use and only the executor mints it).
This is an API/candidate boundary, not production credential isolation proof. The synthetic fixtures live in
controlled_effect_executor.synthetic and are not re-exported here.
"""
from .errors import BatchSpecError, ExecutorError, LedgerBasisError, ProviderBypassRefused, ResponseLost
from .executor import ControlledEffectExecutor, classify_observation
from .grant import ExecutionGrant, require_grant
from .ports import (OwnerFencePort, OwnerSnapshot, ProviderAck, ProviderEffectPort, ProviderReadbackPort,
                    TargetObservation)
from .receipts import (AdmissionDecision, AdmissionReceipt, ExecutionResult, LedgerEvent, Provenance,
                       verify_provenance)
from .spec import BatchSpec, Operation
from .states import (COMMITTED_READBACK, DENIED, FENCED, IN_FLIGHT, NO_COMMIT, STATES, TERMINAL, TRANSITIONS,
                     UNKNOWN_EFFECT)
from .store import AdmissionRecord, InMemoryAdmissionStore
from .views import ApprovalView, DelegationView, TargetScopeEntry

__version__ = "0.1.0"

__all__ = [
    "AdmissionDecision", "AdmissionReceipt", "AdmissionRecord", "ApprovalView", "BatchSpec", "BatchSpecError",
    "COMMITTED_READBACK", "ControlledEffectExecutor", "DENIED", "DelegationView", "ExecutionGrant",
    "ExecutionResult", "ExecutorError", "FENCED", "IN_FLIGHT", "InMemoryAdmissionStore", "LedgerBasisError",
    "LedgerEvent", "NO_COMMIT", "Operation", "OwnerFencePort", "OwnerSnapshot", "Provenance",
    "ProviderAck", "ProviderBypassRefused", "ProviderEffectPort", "ProviderReadbackPort", "ResponseLost",
    "STATES", "TERMINAL", "TRANSITIONS", "TargetObservation", "TargetScopeEntry", "UNKNOWN_EFFECT",
    "classify_observation", "require_grant", "verify_provenance",
]
