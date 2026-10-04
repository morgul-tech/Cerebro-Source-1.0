"""Typed errors of the client package. Messages never carry secret material or file contents."""
from __future__ import annotations


class ClientError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code, self.detail = code, detail


class ConfigError(ClientError):
    """Configuration is missing, malformed, out of bounds or tries to carry a secret inline."""


class ConnectError(ClientError):
    """The private NATS connection could not be established (nothing was published)."""


class EvidenceBusyError(ClientError):
    """Another process already owns this evidence directory for this role."""


class BindingError(ClientError):
    """The nats-py binding is not usable (dependency missing, wrong role, wrong lifecycle)."""
