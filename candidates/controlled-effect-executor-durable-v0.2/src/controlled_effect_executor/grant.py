"""ExecutionGrant: the only thing a provider adapter accepts. Minted solely by ControlledEffectExecutor; ONE-USE.

HONEST LIMIT: this is an API/candidate boundary inside one Python process. Anyone who can import this private
module can forge a grant. It is NOT credential isolation and is not proof of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ProviderBypassRefused

_SEAL = object()  # module-private; only executor.py imports it


@dataclass(frozen=True)
class ExecutionGrant:
    admission_ref: str
    attempt_ref: str
    batch_digest: str
    correlation_ref: str
    target_identity: str
    target_precondition_version: "str | None"
    artifact_version: str
    operations: tuple  # tuple of Operation, exact order
    _token: object = None
    _spent: list = field(default_factory=list, compare=False, repr=False)  # one-use latch (mutable cell)

    @classmethod
    def _mint(cls, seal: object, **fields: object) -> "ExecutionGrant":
        if seal is not _SEAL:
            raise ProviderBypassRefused("grant-can-only-be-minted-by-the-executor")
        return cls(_token=_SEAL, **fields)  # type: ignore[arg-type]


def require_grant(grant: object) -> ExecutionGrant:
    """Called by provider adapters: refuse anything that is not an executor-minted grant."""
    if not isinstance(grant, ExecutionGrant) or grant._token is not _SEAL:
        raise ProviderBypassRefused("provider-effect-requires-an-executor-grant")
    if grant._spent:  # a captured grant cannot be replayed through the adapter
        raise ProviderBypassRefused("grant-already-used")
    grant._spent.append(True)
    return grant
