"""Direct, publisher-authored release records."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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
    packages: tuple[PackageEntry, ...]

    @field_validator("packages")
    @classmethod
    def sort_packages(cls, value: tuple[PackageEntry, ...]) -> tuple[PackageEntry, ...]:
        return tuple(sorted(value, key=canonical_json))


class ReleaseManifest(BaseModel):
    """The direct package changes produced for one upstream release."""

    model_config = MODEL_CONFIG

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


class ReleaseSource(BaseModel):
    """The GitHub release that authorizes direct index changes."""

    model_config = MODEL_CONFIG

    repository: StrictString
    release_id: int = Field(gt=0)
    tag: StrictString
    published_at: datetime

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
        prefix = f"https://github.com/{self.source.repository}/releases/download/{self.source.tag}/"
        for url in release_urls(self):
            if not url.startswith(prefix):
                raise ValueError(f"release URL is not bound to its source: {url}")
        return self


def release_urls(record: ReleaseRecord) -> Iterator[str]:
    """Yield each URL that a release record makes visible to clients."""
    yield from (replacement.url for replacement in record.replacements)
    for fragment in record.indexes:
        for package in fragment.packages:
            yield package.installref
            yield from (reference.ref for reference in package.installrefs)
