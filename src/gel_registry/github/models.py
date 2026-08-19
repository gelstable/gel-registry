"""GitHub release payload models and canonical CLI asset naming."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..constants import CLI_PLATFORMS, PRODUCT_REPOSITORIES
from ..contracts import parse_semver

GITHUB_REPOSITORY = PRODUCT_REPOSITORIES["gel-cli"]

_TAG_PREFIX = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")


class GitHubError(RuntimeError):
    """Raised when GitHub data cannot be trusted or downloaded."""


_GITHUB_MODEL_CONFIG = ConfigDict(extra="ignore", frozen=True)


class GitHubAsset(BaseModel):
    """The release-asset metadata needed by discovery and verification."""

    model_config = _GITHUB_MODEL_CONFIG

    name: StrictStr
    url: StrictStr
    browser_download_url: StrictStr | None = None
    id: StrictInt | None = Field(default=None, gt=0)
    content_type: StrictStr | None = None
    size: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value:
            raise ValueError("asset has no valid name")
        if "/" in value or "\\" in value or value in {".", ".."} or "\x00" in value:
            raise ValueError(f"asset {value!r} has an unsafe name")
        return value

    @field_validator("url", "browser_download_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("asset URL must not be empty")
        return value

    @field_validator("size", mode="before")
    @classmethod
    def normalize_size(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @property
    def download_url(self) -> str:
        """Return the immutable browser URL used in release records."""

        return self.browser_download_url or self.url


class GitHubRelease(BaseModel):
    """A published GitHub release from the allowlisted repository."""

    model_config = _GITHUB_MODEL_CONFIG

    id: StrictInt = Field(gt=0)
    tag_name: StrictStr
    assets: tuple[GitHubAsset, ...]
    draft: StrictBool = False
    prerelease: StrictBool = False
    repository: StrictStr = GITHUB_REPOSITORY
    url: StrictStr | None = None
    html_url: StrictStr | None = None
    version: StrictStr = Field(default="", exclude=True, repr=False)
    repositories: frozenset[str] = Field(
        default_factory=frozenset, exclude=True, repr=False
    )

    @model_validator(mode="before")
    @classmethod
    def parse_payload(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        tag_name = data.get("tag_name")
        if isinstance(tag_name, str):
            version = _version_from_tag(tag_name)
            if version is None:
                raise ValueError(f"invalid release tag: {tag_name!r}")
            data["version"] = version
        repositories = _release_repositories(data)
        repository = data.get("repository")
        if not repositories and isinstance(repository, str) and "/" in repository:
            repositories = frozenset({repository})
        data["repositories"] = repositories
        data["repository"] = (
            sorted(repositories)[0] if repositories else GITHUB_REPOSITORY
        )
        return data

    @field_validator("tag_name")
    @classmethod
    def validate_tag_name(cls, value: str) -> str:
        if not value:
            raise ValueError("release tag must not be empty")
        return value

    @model_validator(mode="after")
    def validate_release(self) -> GitHubRelease:
        names = [asset.name for asset in self.assets]
        if len(names) != len(set(names)):
            raise ValueError(f"release {self.id} has duplicate asset names")
        return self

    @property
    def release_id(self) -> int:
        """Alias matching the registry's ``ReleaseSource`` field."""

        return self.id

    @property
    def tag(self) -> str:
        """Alias for the GitHub API's ``tag_name`` field."""

        return self.tag_name


def asset_name(platform: str, encoding: str) -> str:
    """Return the canonical release asset name for one platform target."""

    suffix = ".exe" if platform.endswith("-windows-msvc") else ""
    identity = f"gel-cli-{platform}{suffix}"
    return f"{identity}.zst" if encoding == "zstd" else identity


def canonical_asset_names() -> tuple[str, ...]:
    """Return canonical identity/zstd asset names in registry order."""

    names: list[str] = []
    for platform in CLI_PLATFORMS:
        names.extend((asset_name(platform, "identity"), asset_name(platform, "zstd")))
    return tuple(names)


def _version_from_tag(tag: str) -> str | None:
    if tag.startswith("v"):
        version = tag[1:]
        if parse_semver(version) is not None:
            return version
    prefix, separator, version = tag.rpartition("-v")
    if not separator or not _TAG_PREFIX.fullmatch(prefix):
        return None
    return version if parse_semver(version) is not None else None


def _repository_from_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    pieces = parsed.path.strip("/").split("/")
    if (
        parsed.hostname == "api.github.com"
        and len(pieces) >= 3
        and pieces[0] == "repos"
    ):
        return "/".join(pieces[1:3])
    if parsed.hostname == "github.com" and len(pieces) >= 2:
        return "/".join(pieces[:2])
    return None


def _release_repositories(data: Mapping[str, object]) -> frozenset[str]:
    repositories: set[str] = set()
    for key in ("repository", "repo"):
        value = data.get(key)
        if isinstance(value, str) and "/" in value:
            repositories.add(value)
        if isinstance(value, dict):
            full_name = value.get("full_name")
            if isinstance(full_name, str) and "/" in full_name:
                repositories.add(full_name)
    for key in ("url", "html_url", "assets_url", "upload_url"):
        value = data.get(key)
        if isinstance(value, str):
            repository = _repository_from_url(value)
            if repository is not None:
                repositories.add(repository)
    return frozenset(repositories)
