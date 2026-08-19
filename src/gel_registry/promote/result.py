"""The error and result types of a publication transaction."""

from __future__ import annotations

from dataclasses import dataclass


class PromotionError(RuntimeError):
    """Raised when a publication transaction cannot be committed safely."""


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """The deterministic result of a staged publication transaction."""

    snapshot: str
    branch: str
    changed_paths: tuple[str, ...]

    @property
    def snapshot_id(self) -> str:
        """Compatibility alias for callers that name snapshots explicitly."""

        return self.snapshot

    @property
    def branch_name(self) -> str:
        """Compatibility alias for callers that name branches explicitly."""

        return self.branch

    @property
    def paths(self) -> tuple[str, ...]:
        """Compatibility alias for the git-style changed-path report."""

        return self.changed_paths

    @property
    def noop(self) -> bool:
        """Whether the transaction already matched every resulting byte."""

        return not self.changed_paths
