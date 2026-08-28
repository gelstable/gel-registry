"""Strict, immutable models for registry source and rendered documents."""

from __future__ import annotations

from .capture import CaptureEntry, CaptureManifest
from .common import (
    JsonValue,
    SemVer,
    SnapshotId,
    StrictString,
    parse_semver,
    semver_key,
)
from .legacy import InstallRef, PackageEntry, PackageIndex, Verification
from .promotion import BlockedCategory, BlockedManifest, BlockedRelease
from .public import (
    ManifestIndex,
    Pointer,
    RootIndex,
    RootManifest,
    SnapshotListing,
)
from .release import (
    Artifact,
    ReleaseRecord,
    ReleaseSource,
    media_type_for_platform,
)

__all__ = [
    "Artifact",
    "BlockedCategory",
    "BlockedManifest",
    "BlockedRelease",
    "CaptureEntry",
    "CaptureManifest",
    "InstallRef",
    "JsonValue",
    "ManifestIndex",
    "PackageEntry",
    "PackageIndex",
    "Pointer",
    "ReleaseRecord",
    "ReleaseSource",
    "RootIndex",
    "RootManifest",
    "SemVer",
    "SnapshotId",
    "SnapshotListing",
    "StrictString",
    "Verification",
    "media_type_for_platform",
    "parse_semver",
    "semver_key",
]
