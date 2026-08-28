"""The published root, pointer and snapshot-listing contracts."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, field_validator, model_validator

from ..constants import CHANNELS, LEGACY_PLATFORMS
from .common import MODEL_CONFIG, SnapshotId, StrictString


class RootIndex(BaseModel):
    """A channel/platform index reference in a root manifest."""

    model_config = MODEL_CONFIG

    channel: StrictString
    platform: StrictString
    ref: StrictString

    @model_validator(mode="after")
    def validate_root_index(self) -> RootIndex:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        if self.platform not in LEGACY_PLATFORMS:
            raise ValueError(f"unknown platform {self.platform!r}")
        parsed = urlsplit(self.ref)
        if (
            parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("root index URL must not contain fragment or userinfo")
        if parsed.scheme or parsed.netloc:
            raise ValueError("root index URL must be relative")
        return self


class RootManifest(BaseModel):
    """A root document listing exactly the indexes in one snapshot."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1] = 1
    indexes: tuple[RootIndex, ...]

    @field_validator("indexes")
    @classmethod
    def sort_indexes(cls, value: tuple[RootIndex, ...]) -> tuple[RootIndex, ...]:
        order = {
            (channel, platform): index
            for index, (channel, platform) in enumerate(
                (channel, platform)
                for channel in CHANNELS
                for platform in LEGACY_PLATFORMS
            )
        }
        return tuple(
            sorted(value, key=lambda item: order[(item.channel, item.platform)])
        )

    @model_validator(mode="after")
    def validate_unique_indexes(self) -> RootManifest:
        identities = [(item.channel, item.platform) for item in self.indexes]
        if len(identities) != len(set(identities)):
            raise ValueError("root manifest contains duplicate indexes")
        return self


class Pointer(BaseModel):
    """The sole mutable source pointer selecting a published snapshot."""

    model_config = MODEL_CONFIG

    snapshot: SnapshotId


class SnapshotListing(BaseModel):
    """Published listing of every immutable snapshot and the selected one."""

    model_config = MODEL_CONFIG

    latest: SnapshotId
    snapshots: tuple[SnapshotId, ...]

    @field_validator("snapshots")
    @classmethod
    def sort_snapshots(cls, value: tuple[SnapshotId, ...]) -> tuple[SnapshotId, ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_snapshot_listing(self) -> SnapshotListing:
        if len(self.snapshots) != len(set(self.snapshots)):
            raise ValueError("snapshot listing contains duplicates")
        if self.latest not in self.snapshots:
            raise ValueError("latest snapshot must be present in the listing")
        return self


# The source and renderer use both names in prose; retain the explicit alias
# without defining a second model or changing the serialized contract.
ManifestIndex = RootIndex
