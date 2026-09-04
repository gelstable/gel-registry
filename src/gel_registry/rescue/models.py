"""Canonical, immutable contracts used by the legacy rescue workflow."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, Self
from urllib.parse import unquote

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.config import ExtraValues

from ..contracts.common import (
    HEX64,
    MODEL_CONFIG,
    StrictString,
    digest_value,
    url_parts,
    validate_utc,
)
from ..digest import canonical_json

_SOURCE_HOSTS = frozenset({"packages.edgedb.com", "packages.geldata.com"})


class _CanonicalModel(BaseModel):
    """A frozen model that only accepts its canonical JSON representation."""

    model_config = MODEL_CONFIG

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        model = super().model_validate_json(
            json_data,
            strict=strict,
            extra=extra,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )
        supplied = (
            json_data.encode() if isinstance(json_data, str) else bytes(json_data)
        )
        if supplied != canonical_json(model):
            raise ValueError("JSON must use the canonical JSON representation")
        return model


def _validate_source_url(value: str) -> str:
    _, parsed = url_parts(value, absolute=True)
    if parsed.hostname not in _SOURCE_HOSTS:
        raise ValueError("source URL must use an allowlisted package host")
    if parsed.query:
        raise ValueError("source URL must not contain a query")
    decoded_path = unquote(parsed.path)
    if not decoded_path.startswith("/") or decoded_path == "/" or "//" in decoded_path:
        raise ValueError("source URL must contain a safe package path")
    if any(
        part in {".", ".."} or "\\" in part or "\x00" in part
        for part in decoded_path.split("/")
        if part
    ):
        raise ValueError("source URL must contain a safe package path")
    return value


def _validate_size(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("size must be an integer")
    return value


def _validate_asset_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        value in {".", ".."}
        or path.name != value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("destination name must be a safe filename")
    return value


class RescueAsset(_CanonicalModel):
    """One direct original source asset to transfer into a draft release.

    ``expected_size`` and ``expected_sha256`` describe the exact identity
    source bytes at ``source_url``, which must match the exact SHA-256
    needed for a promotion replacement.
    """

    source_url: str
    """The legacy identity object to fetch, always uncompressed by this plan."""

    destination_name: StrictString
    """The asset filename created on the draft release."""

    content_type: StrictString
    """The media type recorded on GitHub."""

    expected_size: int = Field(ge=0)
    """Byte size of the source object at ``source_url``."""

    expected_sha256: str
    """SHA-256 of the source object at ``source_url``."""

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_source_url(value)

    @field_validator("destination_name")
    @classmethod
    def validate_destination_name(cls, value: str) -> str:
        return _validate_asset_name(value)

    @field_validator("expected_size", mode="before")
    @classmethod
    def reject_boolean_size(cls, value: object) -> object:
        return _validate_size(value)

    @field_validator("expected_sha256")
    @classmethod
    def validate_expected_sha256(cls, value: str) -> str:
        return digest_value(value, HEX64, "expected_sha256")


class RescueRelease(_CanonicalModel):
    """One immutable destination release and its ordered upload set."""

    repository: StrictString
    tag: StrictString
    target_commitish: str | None
    assets: tuple[RescueAsset, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_assets(self) -> Self:
        names = tuple(asset.destination_name for asset in self.assets)
        if len(names) != len(set(names)):
            raise ValueError("release contains duplicate destination asset names")
        if names != tuple(sorted(names)):
            raise ValueError("release assets must use canonical order")
        return self


class RescuePlan(_CanonicalModel):
    """The complete, deterministic release plan produced from one capture."""

    schema_version: Literal[1]
    capture: StrictString
    releases: tuple[RescueRelease, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_releases(self) -> Self:
        destinations = tuple(
            (release.repository, release.tag) for release in self.releases
        )
        if len(destinations) != len(set(destinations)):
            raise ValueError("plan contains duplicate destination repository/tag pairs")
        if destinations != tuple(sorted(destinations)):
            raise ValueError("plan releases must use canonical order")
        digests = tuple(
            asset.expected_sha256
            for release in self.releases
            for asset in release.assets
        )
        if len(digests) != len(set(digests)):
            raise ValueError(
                "plan contains duplicate asset expected_sha256 digests across releases"
            )
        return self


class RescueIndexCapture(_CanonicalModel):
    """Canonical evidence for one retrieved legacy package index."""

    source_url: str
    channel: StrictString
    platform: StrictString
    captured_at: datetime
    byte_size: int = Field(ge=0)
    sha256: str

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_source_url(value)

    @field_validator("captured_at")
    @classmethod
    def validate_capture_time(cls, value: datetime) -> datetime:
        return validate_utc(value)

    @field_validator("byte_size", mode="before")
    @classmethod
    def reject_boolean_size(cls, value: object) -> object:
        return _validate_size(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return digest_value(value, HEX64, "sha256")


class RescueAbsentIndex(_CanonicalModel):
    """Canonical evidence that one legacy index was looked for and was gone.

    An absence is recorded, never inferred: the capture still requests the
    URL and stores the status it observed. This is deliberately not an
    empty :class:`RescueIndexCapture` -- "upstream never published this"
    and "upstream published an index with no packages" are different
    facts, and only the former has no bytes to preserve.
    """

    source_url: str
    channel: StrictString
    platform: StrictString
    captured_at: datetime
    observed_status: int = Field(ge=400, le=599)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_source_url(value)

    @field_validator("captured_at")
    @classmethod
    def validate_capture_time(cls, value: datetime) -> datetime:
        return validate_utc(value)

    @field_validator("observed_status", mode="before")
    @classmethod
    def reject_boolean_status(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("observed status must be an integer")
        return value


__all__ = [
    "RescueAbsentIndex",
    "RescueAsset",
    "RescueIndexCapture",
    "RescuePlan",
    "RescueRelease",
]
