"""The client-compatible legacy package-index contract."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import (
    HEX64,
    HEX128,
    MODEL_CONFIG,
    JsonValue,
    StrictString,
    digest_value,
    validate_installref_url,
)


class Verification(BaseModel):
    """Legacy or release artifact byte verification metadata."""

    model_config = MODEL_CONFIG

    size: int = Field(ge=0)
    sha256: str | None = None
    blake2b: str

    @field_validator("size", mode="before")
    @classmethod
    def reject_bool_size(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("size must be an integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return digest_value(value, HEX64, "sha256")

    @field_validator("blake2b")
    @classmethod
    def validate_blake2b(cls, value: str) -> str:
        return digest_value(value, HEX128, "blake2b")


class InstallRef(BaseModel):
    """An upstream package install reference."""

    model_config = MODEL_CONFIG

    ref: StrictString
    type: StrictString
    encoding: str | None = None
    verification: Verification

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        validate_installref_url(value)
        return value


class PackageEntry(BaseModel):
    """One package and all install references represented by an index."""

    model_config = MODEL_CONFIG

    basename: StrictString
    name: StrictString
    version: StrictString
    version_details: dict[str, JsonValue]
    version_key: StrictString
    revision: StrictString
    build_date: StrictString
    architecture: StrictString
    slot: str
    tags: dict[str, JsonValue] = Field(default_factory=dict)
    installref: StrictString
    installrefs: tuple[InstallRef, ...]

    @field_validator("installref")
    @classmethod
    def validate_installref(cls, value: str) -> str:
        validate_installref_url(value)
        return value


class PackageIndex(BaseModel):
    """The client-compatible legacy package-index document."""

    model_config = MODEL_CONFIG

    packages: tuple[PackageEntry, ...]

    @model_validator(mode="after")
    def reject_duplicate_identities(self) -> PackageIndex:
        identities = [
            (item.basename, item.version, item.slot) for item in self.packages
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("package identity (basename, version, slot) is duplicated")
        return self
