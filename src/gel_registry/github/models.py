"""Role-specific models for GitHub release discovery and repository operations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

_MODEL_CONFIG = ConfigDict(extra="ignore", frozen=True)
_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
_REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class GitHubError(RuntimeError):
    """Raised when GitHub returns an unexpected or erroneous response."""


class GitHubAsset(BaseModel):
    """The verification data returned for one uploaded release asset."""

    model_config = _MODEL_CONFIG

    id: StrictInt = Field(gt=0)
    name: StrictStr
    url: StrictStr | None = None
    size: StrictInt | None = Field(default=None, ge=0)
    digest: StrictStr | None = None
    content_type: StrictStr | None = None
    state: StrictStr | None = None
    """GitHub's upload state: ``"uploaded"`` when complete, ``"starter"``
    while an upload is incomplete. ``None`` when GitHub omitted it."""

    @property
    def sha256(self) -> str | None:
        """Return GitHub's SHA-256 digest without its algorithm prefix."""

        if self.digest is None:
            return None
        match = _SHA256.fullmatch(self.digest)
        return match.group(1) if match is not None else None


class GitHubRelease(BaseModel):
    """A GitHub release returned by a repository-scoped REST endpoint."""

    model_config = _MODEL_CONFIG

    id: StrictInt = Field(gt=0)
    tag_name: StrictStr
    draft: StrictBool = False
    prerelease: StrictBool = False
    body: StrictStr | None = None
    url: StrictStr | None = None
    upload_url: StrictStr | None = None
    assets: tuple[GitHubAsset, ...] = ()
    repository: StrictStr | None = None

    @property
    def release_id(self) -> int:
        return self.id

    @property
    def tag(self) -> str:
        return self.tag_name


def validate_repository(repository: str) -> str:
    """Require a simple ``owner/repository`` identity for every request."""

    parts = repository.split("/")
    if len(parts) != 2 or any(
        part in {".", ".."} or _REPOSITORY_COMPONENT.fullmatch(part) is None
        for part in parts
    ):
        raise GitHubError("GitHub repository must be an owner/repository identity")
    return repository


def api_release_url(repository: str, release_id: int) -> str:
    return f"https://api.github.com/repos/{repository}/releases/{release_id}"


def upload_asset_url(repository: str, release_id: int) -> str:
    return f"https://uploads.github.com/repos/{repository}/releases/{release_id}/assets"


def repository_from_payload(value: object) -> str | None:
    """Extract an explicit repository identity when GitHub supplied one."""

    if not isinstance(value, Mapping):
        return None
    repository = value.get("repository")
    if isinstance(repository, str):
        return repository
    if isinstance(repository, Mapping):
        full_name = repository.get("full_name")
        if isinstance(full_name, str):
            return full_name
    return None


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
    "GitHubAsset",
    "GitHubError",
    "GitHubRelease",
    "api_release_url",
    "repository_from_payload",
    "upload_asset_url",
    "validate_repository",
]
