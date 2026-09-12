"""Construction of canonical release manifests for legacy rescue drafts."""

from __future__ import annotations

import hashlib
from typing import Final

from ..contracts.release import ReleaseManifest, Replacement
from ..digest import canonical_json
from .models import RescueRelease

MANIFEST_NAME: Final = "gel-registry.json"


def build_release_manifest(release: RescueRelease) -> ReleaseManifest:
    """Build a release manifest containing replacements for all rescued assets."""

    replacements = tuple(
        Replacement(
            sha256=asset.expected_sha256,
            url=(
                f"https://github.com/{release.repository}/releases/download/"
                f"{release.tag}/{asset.destination_name}"
            ),
        )
        for asset in release.assets
    )
    return ReleaseManifest(
        schema_version=1,
        replacements=replacements,
        indexes=(),
    )


def release_manifest_bytes(release: RescueRelease) -> bytes:
    """Return the canonical JSON bytes for a rescue release's manifest."""

    return canonical_json(build_release_manifest(release))


def release_manifest_digest(manifest_bytes: bytes) -> str:
    """Return the hex sha256 of the canonical manifest bytes."""

    return hashlib.sha256(manifest_bytes).hexdigest()


__all__ = [
    "MANIFEST_NAME",
    "build_release_manifest",
    "release_manifest_bytes",
    "release_manifest_digest",
]
