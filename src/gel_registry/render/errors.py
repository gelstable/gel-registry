"""Errors raised while composing or installing rendered registry trees."""

from __future__ import annotations


class RenderError(RuntimeError):
    """Raised when local registry inputs or rendered trees are invalid."""


class ContestedIdentityError(RenderError):
    """Raised when one package identity has different serialized entries."""

    def __init__(self, identity: tuple[str, ...]) -> None:
        self.identity = identity
        super().__init__(f"contested package identity {identity!r}")


class ContestedReplacementError(RenderError):
    """Raised when multiple release records claim the same replacement digest."""

    def __init__(self, sha256: str, sources: tuple[str, ...]) -> None:
        self.sha256 = sha256
        self.sources = sources
        sources_desc = ", ".join(sources)
        super().__init__(
            f"contested replacement digest {sha256}: claimed by {sources_desc}"
        )


__all__ = ["ContestedIdentityError", "ContestedReplacementError", "RenderError"]
