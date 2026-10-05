"""Synthetic fixtures. LABELLED, REFERENCE-ONLY, NOT EXPORTED from the package root, NOT production."""
from .clock import FakeClock
from .owner import SyntheticOwner
from .provider import ProviderRejected, SyntheticProvider

__all__ = ["FakeClock", "ProviderRejected", "SyntheticOwner", "SyntheticProvider"]
