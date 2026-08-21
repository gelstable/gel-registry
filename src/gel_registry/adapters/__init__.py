"""Product adapters used to select and verify upstream releases."""

from __future__ import annotations

from ..policy import PolicyError
from .base import (
    Attestor,
    DeterministicVerificationError,
    ReleaseAdapter,
    VerificationError,
    stable_diagnostic,
    validate_record_policy,
)
from .cli import CliReleaseAdapter

_ADAPTERS: dict[str, ReleaseAdapter] = {"gel-cli": CliReleaseAdapter()}


def adapter_for(name: str) -> ReleaseAdapter:
    """Return the typed adapter registered under a policy adapter name."""

    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        raise PolicyError(f"unknown release adapter {name!r}") from exc


__all__ = [
    "Attestor",
    "CliReleaseAdapter",
    "DeterministicVerificationError",
    "ReleaseAdapter",
    "VerificationError",
    "adapter_for",
    "stable_diagnostic",
    "validate_record_policy",
]
