"""Typed errors. Messages never carry secrets; only references, codes and class names."""
from __future__ import annotations


class ExecutorError(Exception):
    """Base class of every error raised by this package."""


class BatchSpecError(ExecutorError, ValueError):
    """A BatchSpec / Operation / view is malformed (type, shape, bound or forbidden value)."""


class LedgerBasisError(ExecutorError):
    """A ledger write tried to rewrite or contradict the immutable admission basis, or an illegal transition."""


class ProviderBypassRefused(ExecutorError):
    """A provider adapter was invoked without an executor-minted ExecutionGrant."""


class ResponseLost(ExecutorError):
    """Raised by a (mock) provider adapter when the call may or may not have committed and no response arrived."""
