"""controlled_effect_executor_durable: PostgreSQL-backed, restart-recoverable continuation of the V0.1 candidate.

STATUS: isolated implementation candidate, authority NONE, local default-off. Reuses the pinned V0.1 package
(``controlled_effect_executor``: BatchSpec, receipts, states, grant, ports) unchanged and adds only:

* ``PgAdmissionStore``: admission fence + attempt ledger in one real database transaction boundary, with a bounded,
  read-only ``list_unresolved`` recovery listing;
* ``DurableControlledEffectExecutor``: V0.1 execution semantics on that store, plus ``recover``;
* ``settlement``: explicit provider settlement evidence required for a FINAL NO_COMMIT;
* ``pg_libpq`` (stdlib ctypes driver), ``schema`` (disposable-schema DDL), ``synthetic_pg`` (SYNTH owner/provider).

Proves local durable-adapter behaviour against a disposable PostgreSQL. It does NOT prove real provider credentials,
live authority, a production owner store, or credential isolation: this is an API/candidate boundary, not production
credential isolation proof.
"""
from .errors import CommitNotConfirmed, ReservationNotConfirmed, StateMoved, StorageError
from .executor_pg import DurableControlledEffectExecutor
from .pg_libpq import PgError, make_connection_factory
from .schema import create_schema
from .settlement import (ProviderSettlementReadbackPort, SettlementEvidence, classify_durable)
from .store_pg import (MAX_PAGE_LIMIT, OwnerTransactionPort, PgAdmissionStore, Progress, RecoveryPage,
                       UnresolvedAdmission)
from .verify import verify_provenance_durable

__version__ = "0.2.0"

__all__ = [
    "CommitNotConfirmed", "DurableControlledEffectExecutor", "MAX_PAGE_LIMIT", "OwnerTransactionPort", "PgAdmissionStore",
    "PgError", "Progress", "ProviderSettlementReadbackPort", "RecoveryPage", "ReservationNotConfirmed",
    "SettlementEvidence", "StateMoved", "StorageError", "UnresolvedAdmission", "classify_durable", "create_schema",
    "make_connection_factory", "verify_provenance_durable",
]
