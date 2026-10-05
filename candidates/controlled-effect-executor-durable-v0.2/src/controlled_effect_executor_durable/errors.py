"""Typed errors of the durable adapter. Messages carry codes only: never DSNs, passwords, SQL text or row data."""
from __future__ import annotations

from controlled_effect_executor.errors import ExecutorError, LedgerBasisError


class StorageError(ExecutorError):
    """The database refused or could not be reached. ``sqlstate`` is '' for connection-level failures."""

    def __init__(self, code: str, sqlstate: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.sqlstate = sqlstate


class CommitNotConfirmed(StorageError):
    """COMMIT raised: the transaction may or may not be durable. NEVER treated as success."""


class ReservationNotConfirmed(LedgerBasisError):
    """The attempt reservation's COMMIT was not confirmed. The caller must NOT call the provider."""


class StateMoved(LedgerBasisError):
    """The admission's progress moved since the caller decided (a concurrent writer won); re-read, do not overwrite."""
