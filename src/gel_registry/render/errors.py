"""Errors raised while composing or installing rendered registry trees."""

from __future__ import annotations


class RenderError(RuntimeError):
    """Raised when local registry inputs or rendered trees are invalid."""


class ContestedIdentityError(RenderError):
    """Raised when one package identity has different serialized entries."""

    def __init__(self, identity: tuple[str, ...]) -> None:
        self.identity = identity
        super().__init__(f"contested package identity {identity!r}")


__all__ = ["ContestedIdentityError", "RenderError"]
