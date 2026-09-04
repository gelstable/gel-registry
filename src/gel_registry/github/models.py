"""Role-specific models for GitHub release discovery and repository operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class GitHubError(RuntimeError):
    """Raised when GitHub returns an unexpected or erroneous response."""


def validate_repository(repository: str) -> str:
    """Require a simple ``owner/repository`` identity for every request."""

    parts = repository.split("/")
    if len(parts) != 2 or any(
        part in {".", ".."} or _REPOSITORY_COMPONENT.fullmatch(part) is None
        for part in parts
    ):
        raise GitHubError("GitHub repository must be an owner/repository identity")
    return repository


@dataclass(frozen=True, slots=True)
class DiscoveredAsset:
    """One asset returned in a GitHub release object."""

    name: str
    api_url: str


@dataclass(frozen=True, slots=True)
class DiscoveredRelease:
    """A published or draft release returned by a repository's release API."""

    repository: str
    release_id: int
    tag: str
    published_at: datetime
    draft: bool
    assets: tuple[DiscoveredAsset, ...]


__all__ = [
    "DiscoveredAsset",
    "DiscoveredRelease",
    "GitHubError",
    "validate_repository",
]
