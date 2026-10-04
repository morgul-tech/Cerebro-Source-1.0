"""signalvev_client -- installable private Core NATS client for the existing Signalvev D0 implementation.

STATUS: isolated implementation candidate. authority: NONE. Not deployed; no live Cerebro node/EDGE-01 contact.
Supported import path (it binds the bundled, hash-pinned reference modules before the core is imported).
"""
__version__ = "0.1.0"

from . import _bootstrap  # noqa: E402,F401  must run before anything imports signalvev_sensing

from .config import ClientConfig, load_config, parse_config  # noqa: E402
from .errors import BindingError, ClientError, ConfigError, ConnectError, EvidenceBusyError  # noqa: E402
from .session import CLAIMS, ListenClient, SendClient, health, load_resolver, precheck_event  # noqa: E402
from .synthetic_owner import SyntheticOwnerResolver  # noqa: E402

__all__ = ["__version__", "ClientConfig", "load_config", "parse_config", "SendClient", "ListenClient", "health",
           "load_resolver", "precheck_event", "SyntheticOwnerResolver", "CLAIMS", "ClientError", "ConfigError",
           "ConnectError", "BindingError", "EvidenceBusyError"]
