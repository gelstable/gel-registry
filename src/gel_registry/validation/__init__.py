"""Offline integrity gates and explicitly-scoped remote checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .local import validate_capture_local, validate_local
from .report import ValidationReport

if TYPE_CHECKING:
    from .candidate import validate_candidate
    from .remote import validate_blocked_remotes, validate_release_remotes


def __getattr__(name: str) -> object:
    """Load candidate/remote layers without widening offline imports."""

    if name == "validate_candidate":
        from .candidate import validate_candidate

        globals()[name] = validate_candidate
        return validate_candidate
    if name in {"validate_blocked_remotes", "validate_release_remotes"}:
        from .remote import validate_blocked_remotes, validate_release_remotes

        globals()["validate_blocked_remotes"] = validate_blocked_remotes
        globals()["validate_release_remotes"] = validate_release_remotes
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ValidationReport",
    "validate_capture_local",
    "validate_candidate",
    "validate_blocked_remotes",
    "validate_local",
    "validate_release_remotes",
]
