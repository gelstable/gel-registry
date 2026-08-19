"""The verified Gelstable release-record contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from ..constants import CLI_PLATFORMS, PRODUCT_REPOSITORIES
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
        if self.platform not in CLI_PLATFORMS:
            raise ValueError(f"unsupported CLI platform {self.platform!r}")
        url_parts(self.url, absolute=True, host="github.com")
        parsed = urlsplit(self.url)
        if parsed.hostname != "github.com":
            raise ValueError("release artifact URL must use github.com")
        if parsed.query:
            raise ValueError("release artifact URL must not contain a query")
        expected_media_type = media_type_for_platform(self.platform)
        if self.media_type != expected_media_type:
            raise ValueError(
                f"{self.platform} artifacts must use {expected_media_type!r}"
            )
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
    """Append-only, complete stable Gel CLI release record."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1] = 1
    product: Literal["gel-cli"]
    channel: Literal["stable"]
    version: StrictString
    source: ReleaseSource
    promoted_at: datetime
    artifacts: tuple[Artifact, ...]

    @field_validator("artifacts")
    @classmethod
    def sort_artifacts(cls, value: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
        order = {platform: index for index, platform in enumerate(CLI_PLATFORMS)}
        return tuple(
            sorted(
                value,
                key=lambda artifact: (
                    order[artifact.platform],
                    0 if artifact.encoding == "identity" else 1,
                ),
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

    @model_validator(mode="after")
    def validate_release_contract(self) -> ReleaseRecord:
        expected_repository = PRODUCT_REPOSITORIES[self.product]
        if self.source.repository != expected_repository:
            raise ValueError("release source repository is not allowlisted")
        if self.source.release_tag != f"v{self.version}":
            raise ValueError("release tag must exactly be v<version>")
        expected = {
            (platform, encoding)
            for platform in CLI_PLATFORMS
            for encoding in ("identity", "zstd")
        }
        actual = {(artifact.platform, artifact.encoding) for artifact in self.artifacts}
        if actual != expected or len(actual) != len(self.artifacts):
            raise ValueError("release must contain exactly ten CLI installrefs")
        return self


def media_type_for_platform(platform: str) -> str:
    if platform.endswith("-apple-darwin"):
        return "application/x-mach-binary"
    if platform.endswith("-windows-msvc"):
        return "application/x-dosexec"
    return "application/x-pie-executable"
