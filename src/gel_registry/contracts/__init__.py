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
    IndexFragment,
    ReleaseManifest,
    ReleaseRecord,
    ReleaseSource,
    Replacement,
    validate_release_url,
    validate_repository_name,
)

__all__ = [
    "BlockedCategory",
    "BlockedManifest",
    "BlockedRelease",
    "CaptureEntry",
    "CaptureManifest",
    "InstallRef",
    "IndexFragment",
    "JsonValue",
    "ManifestIndex",
    "PackageEntry",
    "PackageIndex",
    "Pointer",
    "ReleaseRecord",
    "ReleaseManifest",
    "ReleaseSource",
    "Replacement",
    "RootIndex",
    "RootManifest",
    "SemVer",
    "SnapshotId",
    "SnapshotListing",
    "StrictString",
    "Verification",
    "parse_semver",
    "semver_key",
    "validate_release_url",
    "validate_repository_name",
]
