"""Direct, publisher-authored release records."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from datetime import datetime
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..digest import canonical_json
from .common import HEX64, MODEL_CONFIG, StrictString, digest_value, validate_utc
from .legacy import PackageEntry


class Replacement(BaseModel):
    """A verified artifact digest and its replacement release URL."""

    model_config = MODEL_CONFIG

    sha256: str
    url: StrictString

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return digest_value(value, HEX64, "sha256")


class IndexFragment(BaseModel):
    """The exact packages a release adds to one consumer index."""

    model_config = MODEL_CONFIG

    channel: StrictString
    platform: StrictString
    packages: tuple[PackageEntry, ...] = Field(min_length=1)

    @field_validator("packages")
    @classmethod
    def sort_packages(cls, value: tuple[PackageEntry, ...]) -> tuple[PackageEntry, ...]:
        return tuple(sorted(value, key=canonical_json))


RELEASE_MANIFEST_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    populate_by_name=True,
    json_schema_extra={
        "anyOf": [
            {
                "properties": {"replacements": {"minItems": 1}},
                "required": ["replacements"],
            },
            {
                "properties": {"indexes": {"minItems": 1}},
                "required": ["indexes"],
            },
        ]
    },
)


class ReleaseManifest(BaseModel):
    """The direct package changes produced for one upstream release."""

    model_config = RELEASE_MANIFEST_CONFIG

    schema_version: Literal[1] = 1
    replacements: tuple[Replacement, ...] = ()
    indexes: tuple[IndexFragment, ...] = ()

    @field_validator("replacements")
    @classmethod
    def sort_replacements(
        cls, value: tuple[Replacement, ...]
    ) -> tuple[Replacement, ...]:
        return tuple(sorted(value, key=lambda replacement: replacement.sha256))

    @field_validator("indexes")
    @classmethod
    def sort_indexes(
        cls, value: tuple[IndexFragment, ...]
    ) -> tuple[IndexFragment, ...]:
        return tuple(sorted(value, key=lambda index: (index.channel, index.platform)))

    @model_validator(mode="after")
    def validate_changes(self) -> ReleaseManifest:
        if not self.replacements and not self.indexes:
            raise ValueError("release manifest must contain a replacement or index")
        digests = [replacement.sha256 for replacement in self.replacements]
        if len(digests) != len(set(digests)):
            raise ValueError("release manifest contains duplicate replacement digests")
        keys = [(index.channel, index.platform) for index in self.indexes]
        if len(keys) != len(set(keys)):
            raise ValueError("release manifest contains duplicate index fragments")
        return self


_SAFE_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def validate_repository_name(repository: str) -> str:
    """Validate owner and repository as safe, nonempty GitHub path components."""
    if not isinstance(repository, str):
        raise ValueError("repository must be a string")
    if repository.count("/") != 1:
        raise ValueError(
            f"repository must be in '<owner>/<repo>' format, got: {repository!r}"
        )
    owner, name = repository.split("/", 1)
    for part, label in ((owner, "owner"), (name, "repository")):
        if not part:
            raise ValueError(f"repository {label} cannot be empty")
        if part in {".", ".."}:
            raise ValueError(f"repository {label} cannot be traversal: {part!r}")
        if any(c in "/\\" or ord(c) < 32 or ord(c) == 127 or c.isspace() for c in part):
            raise ValueError(
                f"repository {label} contains invalid or control characters: {part!r}"
            )
        if not _SAFE_COMPONENT_PATTERN.fullmatch(part):
            raise ValueError(
                f"repository {label} contains invalid characters: {part!r}"
            )
    return repository


def validate_release_url(url: str, repository: str, tag: str) -> str:
    """Validate that a release URL is structurally bound to repository and tag.

    Returns the unquoted asset filename.
    """
    if not isinstance(url, str):
        raise ValueError("release URL must be a string")
    if "?" in url:
        raise ValueError(f"release URL must not contain query parameters: {url}")
    if "#" in url:
        raise ValueError(f"release URL must not contain a fragment: {url}")
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"release URL must use HTTPS: {url}")
    if parsed.netloc != "github.com":
        raise ValueError(f"release URL must be hosted on github.com: {url}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"release URL must not contain userinfo: {url}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"release URL has an invalid port: {url}") from exc
    if port not in (None, 443):
        raise ValueError(f"release URL must use default HTTPS port: {url}")

    path = parsed.path
    if posixpath.normpath(path) != path:
        raise ValueError(f"release URL contains traversal or unnormalized path: {url}")

    segments = path.split("/")
    if len(segments) != 7 or segments[0] != "":
        raise ValueError(f"release URL has invalid path structure: {url}")

    expected_owner, expected_repo = repository.split("/", 1)
    if segments[1] != expected_owner or segments[2] != expected_repo:
        raise ValueError(
            f"release URL repository does not match source: "
            f"{segments[1]}/{segments[2]} != {repository}"
        )
    if segments[3] != "releases" or segments[4] != "download":
        raise ValueError(f"release URL must be under /releases/download/: {url}")
    if segments[5] != tag and unquote(segments[5]) != tag:
        raise ValueError(
            f"release URL tag does not match source: {segments[5]!r} != {tag!r}"
        )

    raw_asset_name = segments[6]
    if not raw_asset_name:
        raise ValueError(f"release URL asset name cannot be empty: {url}")
    asset_name = unquote(raw_asset_name)
    if asset_name in {".", ".."}:
        raise ValueError(f"release URL asset name cannot be traversal: {asset_name!r}")
    if any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in asset_name):
        raise ValueError(
            "release URL asset name contains invalid or control characters: "
            f"{asset_name!r}"
        )
    return asset_name


class ReleaseSource(BaseModel):
    """The GitHub release that authorizes direct index changes."""

    model_config = MODEL_CONFIG

    repository: StrictString
    release_id: int = Field(gt=0)
    tag: StrictString
    published_at: datetime

    @field_validator("repository")
    @classmethod
    def validate_source_repository(cls, value: str) -> str:
        return validate_repository_name(value)

    @field_validator("tag")
    @classmethod
    def validate_source_tag(cls, value: str) -> str:
        if not value:
            raise ValueError("tag cannot be empty")
        if value in {".", ".."}:
            raise ValueError(f"tag cannot be traversal: {value!r}")
        if any(
            c in "/\\" or ord(c) < 32 or ord(c) == 127 or c.isspace() for c in value
        ):
            raise ValueError(f"tag contains invalid or control characters: {value!r}")
        return value

    @field_validator("release_id", mode="before")
    @classmethod
    def reject_bool_release_id(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("release_id must be an integer")
        return value

    @field_validator("published_at")
    @classmethod
    def validate_published_at(cls, value: datetime) -> datetime:
        return validate_utc(value)


class ReleaseRecord(ReleaseManifest):
    """A direct release manifest bound to one GitHub release."""

    source: ReleaseSource

    @model_validator(mode="after")
    def validate_source_urls(self) -> ReleaseRecord:
        for url in release_urls(self):
            validate_release_url(url, self.source.repository, self.source.tag)
        return self


def release_urls(record: ReleaseRecord) -> Iterator[str]:
    """Yield each URL that a release record makes visible to clients."""
    yield from (replacement.url for replacement in record.replacements)
    for fragment in record.indexes:
        for package in fragment.packages:
            yield package.installref
            yield from (reference.ref for reference in package.installrefs)
