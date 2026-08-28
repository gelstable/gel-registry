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


def artifact_name(product: str, platform: str, encoding: str) -> str:
    """Return the canonical release asset filename for one artifact."""

    suffix = ".exe" if platform.endswith("-windows-msvc") else ""
    identity = f"{product}-{platform}{suffix}"
    return f"{identity}.zst" if encoding == "zstd" else identity


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

    @model_validator(mode="after")
    def validate_release_contract(self) -> ReleaseRecord:
        """Bind every artifact to exactly one canonical release asset URL.

        This lives on the contract rather than in the validator alone because
        rendering loads release records directly.  Enforcing provenance only in
        ``gel-registry validate`` would let ``build-snapshot`` publish an
        install reference to arbitrary ``github.com`` content whenever the
        validator had not been run first.
        """

        seen: set[tuple[str, str]] = set()
        for artifact in self.artifacts:
            key = (artifact.platform, artifact.encoding)
            if key in seen:
                raise ValueError(
                    f"duplicate {artifact.encoding} artifact for {artifact.platform}"
                )
            seen.add(key)

            name = artifact_name(self.product, artifact.platform, artifact.encoding)
            expected = (
                f"/{self.source.repository}/releases/download/"
                f"{self.source.release_tag}/{name}"
            )
            if urlsplit(artifact.url).path != expected:
                raise ValueError(
                    f"artifact URL is not the allowlisted release URL: {artifact.url}"
                )
        return self


def media_type_for_platform(platform: str) -> str:
    """Return the legacy CLI media type for compatibility callers."""

    if platform.endswith("-apple-darwin"):
        return "application/x-mach-binary"
    if platform.endswith("-windows-msvc"):
        return "application/x-dosexec"
    return "application/x-pie-executable"
