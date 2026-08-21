"""Contracts for discovery results passed to the promotion transaction."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import MODEL_CONFIG, StrictString


class BlockedCategory(StrEnum):
    """Stable categories allowed in generated blocked-release entries."""

    INVALID_METADATA = "invalid-metadata"
    MISSING_ARTIFACTS = "missing-artifacts"
    UNSUPPORTED_PLATFORM = "unsupported-platform"
    CHECKSUM_MISMATCH = "checksum-mismatch"
    ARTIFACT_MISMATCH = "artifact-mismatch"
    ATTESTATION_FAILED = "attestation-failed"


class BlockedRelease(BaseModel):
    """One deterministic upstream release that could not be verified."""

    model_config = MODEL_CONFIG

    product: StrictString
    repository: StrictString
    release_id: int = Field(gt=0)
    tag: StrictString
    category: BlockedCategory
    diagnostic: StrictString

    @field_validator("release_id", mode="before")
    @classmethod
    def reject_bool_release_id(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("release_id must be an integer")
        return value


class BlockedManifest(BaseModel):
    """Canonical generated manifest of deterministic blocked discoveries."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1] = 1
    entries: tuple[BlockedRelease, ...] = Field(default=())

    @field_validator("entries")
    @classmethod
    def sort_entries(
        cls, value: tuple[BlockedRelease, ...]
    ) -> tuple[BlockedRelease, ...]:
        return tuple(
            sorted(
                value,
                key=lambda entry: (
                    entry.product,
                    entry.repository,
                    entry.release_id,
                    entry.tag,
                ),
            )
        )

    @model_validator(mode="after")
    def validate_unique_entries(self) -> BlockedManifest:
        identities = [
            (entry.product, entry.repository, entry.release_id, entry.tag)
            for entry in self.entries
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("blocked manifest contains duplicate releases")
        return self


__all__ = ["BlockedCategory", "BlockedManifest", "BlockedRelease"]
