"""The verified Gelstable release-record contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import (
    HEX64,
    HEX128,
    MODEL_CONFIG,
    StrictString,
    digest_value,
    parse_semver,
    url_parts,
    validate_utc,
)


class Artifact(BaseModel):
    """One independently verified Gelstable release artifact."""

    model_config = MODEL_CONFIG

    platform: StrictString
    encoding: Literal["identity", "zstd"]
    media_type: StrictString
    url: StrictString
    size: int = Field(ge=0)
    sha256: str
    blake2b: str

    @field_validator("size", mode="before")
    @classmethod
    def reject_bool_artifact_size(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("size must be an integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        return digest_value(value, HEX64, "sha256")

    @field_validator("blake2b")
    @classmethod
    def validate_artifact_blake2b(cls, value: str) -> str:
        return digest_value(value, HEX128, "blake2b")

    @model_validator(mode="after")
    def validate_artifact_contract(self) -> Artifact:
        url_parts(self.url, absolute=True, host="github.com")
        parsed = urlsplit(self.url)
        if parsed.hostname != "github.com":
            raise ValueError("release artifact URL must use github.com")
        if parsed.query:
            raise ValueError("release artifact URL must not contain a query")
        return self


class ReleaseSource(BaseModel):
    """GitHub release provenance recorded with a promoted version."""

    model_config = MODEL_CONFIG

    repository: StrictString
    release_tag: StrictString
    release_id: int = Field(gt=0)

    @field_validator("release_id", mode="before")
    @classmethod
    def reject_bool_release_id(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("release_id must be an integer")
        return value


class ReleaseRecord(BaseModel):
    """Append-only, structurally generic release record."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1] = 1
    product: StrictString
    channel: StrictString
    version: StrictString
    source: ReleaseSource
    promoted_at: datetime
    artifacts: tuple[Artifact, ...]

    @field_validator("artifacts")
    @classmethod
    def sort_artifacts(cls, value: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
        return tuple(
            sorted(
                value,
                key=lambda artifact: (artifact.platform, artifact.encoding),
            )
        )

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if value.startswith("v"):
            raise ValueError("release version must not have a leading v")
        if parse_semver(value) is None:
            raise ValueError("release version must be a SemVer")
        return value

    @field_validator("promoted_at")
    @classmethod
    def validate_promoted_time(cls, value: datetime) -> datetime:
        return validate_utc(value)


def media_type_for_platform(platform: str) -> str:
    """Return the legacy CLI media type for compatibility callers."""

    if platform.endswith("-apple-darwin"):
        return "application/x-mach-binary"
    if platform.endswith("-windows-msvc"):
        return "application/x-dosexec"
    return "application/x-pie-executable"
