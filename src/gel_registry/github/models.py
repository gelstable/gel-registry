"""Role-specific models for GitHub release discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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


__all__ = ["DiscoveredAsset", "DiscoveredRelease"]
