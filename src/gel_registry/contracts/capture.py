"""The frozen legacy capture evidence contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..constants import CAPTURE_ID, CHANNELS, LEGACY_PLATFORMS, ORIGIN, capture_urls
from .common import (
    HEX64,
    HEX128,
    MODEL_CONFIG,
    StrictString,
    digest_value,
    url_parts,
    validate_utc,
)


class CaptureEntry(BaseModel):
    """One attempted URL in the fixed legacy retrieval matrix."""

    model_config = MODEL_CONFIG

    channel: StrictString
    platform: StrictString
    url: StrictString
    status: Literal[200, 404]
    final_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    blake2b: str | None = None
    path: str | None = None

    @field_validator("size", mode="before")
    @classmethod
    def reject_bool_capture_size(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("size must be an integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_capture_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return digest_value(value, HEX64, "sha256")

    @field_validator("blake2b")
    @classmethod
    def validate_capture_blake2b(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return digest_value(value, HEX128, "blake2b")

    @model_validator(mode="after")
    def validate_matrix_entry(self) -> CaptureEntry:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        if self.platform not in LEGACY_PLATFORMS:
            raise ValueError(f"unknown platform {self.platform!r}")
        expected = next(
            url
            for channel, platform, url in capture_urls()
            if channel == self.channel and platform == self.platform
        )
        if self.url != expected:
            raise ValueError("capture URL does not match the fixed matrix")
        url_parts(self.url, absolute=True)

        body_fields = (
            self.final_url,
            self.size,
            self.sha256,
            self.blake2b,
            self.path,
        )
        if self.status == 200:
            if any(value is None for value in body_fields):
                raise ValueError(
                    "HTTP 200 capture entries require byte metadata and path"
                )
            assert self.final_url is not None
            url_parts(self.final_url, absolute=True, host="packages.geldata.com")
            if self.path != f"indexes/{self.channel}-{self.platform}.json":
                raise ValueError("capture body path does not match the matrix entry")
        elif any(value is not None for value in body_fields):
            raise ValueError("HTTP 404 capture entries must not carry byte metadata")
        return self


class CaptureManifest(BaseModel):
    """Immutable retrieval evidence for all 24 legacy matrix URLs."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1] = 1
    capture: str
    origin: str
    captured_at: datetime
    entries: tuple[CaptureEntry, ...]

    @field_validator("captured_at")
    @classmethod
    def validate_capture_time(cls, value: datetime) -> datetime:
        return validate_utc(value)

    @field_validator("entries")
    @classmethod
    def sort_entries(cls, value: tuple[CaptureEntry, ...]) -> tuple[CaptureEntry, ...]:
        expected = [(channel, platform) for channel, platform, _ in capture_urls()]
        return tuple(
            sorted(
                value,
                key=lambda entry: expected.index((entry.channel, entry.platform)),
            )
        )

    @model_validator(mode="after")
    def validate_matrix(self) -> CaptureManifest:
        if self.capture != CAPTURE_ID:
            raise ValueError(f"capture must be {CAPTURE_ID!r}")
        if self.origin != ORIGIN:
            raise ValueError(f"origin must be {ORIGIN!r}")
        expected = [(channel, platform) for channel, platform, _ in capture_urls()]
        actual = [(entry.channel, entry.platform) for entry in self.entries]
        if len(actual) != len(set(actual)):
            raise ValueError("capture matrix contains duplicate entries")
        if set(actual) != set(expected):
            raise ValueError("capture manifest must cover the exact 24-entry matrix")
        return self
