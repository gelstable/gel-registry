"""Tests for canonical rescue release manifest generation and composition."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from gel_registry.contracts import (
    PackageEntry,
    PackageIndex,
    ReleaseManifest,
    ReleaseRecord,
    ReleaseSource,
)
from gel_registry.digest import canonical_json
from gel_registry.render.compose import compose_indexes
from gel_registry.rescue.manifests import (
    build_release_manifest,
    release_manifest_bytes,
    release_manifest_digest,
)
from gel_registry.rescue.models import RescueAsset, RescueRelease

REPOSITORY = "gelstable/gel-cli"
TAG = "v3.0.0"
DIGEST_1 = "1" * 64
DIGEST_2 = "2" * 64


def _asset(
    name: str = "gel-cli-x86_64-unknown-linux-musl.tar.gz",
    digest: str = DIGEST_1,
    size: int = 1234,
) -> RescueAsset:
    return RescueAsset(
        source_url=f"https://packages.edgedb.com/archive/{name}",
        destination_name=name,
        content_type="application/gzip",
        expected_size=size,
        expected_sha256=digest,
    )


def _release(assets: tuple[RescueAsset, ...] | None = None) -> RescueRelease:
    if assets is None:
        assets = (
            _asset("gel-cli-aarch64-apple-darwin.tar.gz", DIGEST_1, 100),
            _asset("gel-cli-x86_64-unknown-linux-musl.tar.gz", DIGEST_2, 200),
        )
    return RescueRelease(
        repository=REPOSITORY,
        tag=TAG,
        target_commitish="abc1234",
        assets=assets,
    )


def test_build_release_manifest_produces_canonical_replacements() -> None:
    release = _release()
    manifest = build_release_manifest(release)

    assert manifest.schema_version == 1
    assert manifest.indexes == ()
    assert len(manifest.replacements) == 2

    first, second = manifest.replacements
    assert first.sha256 == DIGEST_1
    assert (
        first.url
        == f"https://github.com/{REPOSITORY}/releases/download/{TAG}/gel-cli-aarch64-apple-darwin.tar.gz"
    )
    assert second.sha256 == DIGEST_2
    assert (
        second.url
        == f"https://github.com/{REPOSITORY}/releases/download/{TAG}/gel-cli-x86_64-unknown-linux-musl.tar.gz"
    )


def test_manifest_bytes_and_digest_round_trip() -> None:
    release = _release()
    data = release_manifest_bytes(release)
    assert data == canonical_json(build_release_manifest(release))

    digest = release_manifest_digest(data)
    assert digest == hashlib.sha256(data).hexdigest()

    parsed = ReleaseManifest.model_validate_json(data)
    assert parsed == build_release_manifest(release)


def test_empty_assets_raises_validation_error() -> None:
    empty_release = RescueRelease.model_construct(
        repository=REPOSITORY,
        tag=TAG,
        target_commitish=None,
        assets=(),
    )
    with pytest.raises(ValueError, match="must contain a replacement or index"):
        build_release_manifest(empty_release)


def test_composed_rescue_manifest_replaces_legacy_urls_in_bootstrap() -> None:
    release = _release()
    manifest = build_release_manifest(release)

    p1 = PackageEntry.model_validate(
        {
            "basename": "gel-cli_3.0.0_musl",
            "name": "gel-cli",
            "version": "3.0.0",
            "version_details": {},
            "version_key": "3.0.0",
            "revision": "1",
            "build_date": "2026-08-15T00:00:00Z",
            "architecture": "x86_64",
            "slot": "",
            "installref": "https://packages.geldata.com/archive/gel-cli-3.0.0.tar.gz",
            "installrefs": [
                {
                    "ref": "https://packages.geldata.com/archive/gel-cli-3.0.0.tar.gz",
                    "type": "application/octet-stream",
                    "verification": {
                        "size": 200,
                        "sha256": DIGEST_2,
                        "blake2b": "a" * 128,
                    },
                }
            ],
        }
    )
    p2 = PackageEntry.model_validate(
        {
            "basename": "gel-cli_2.0.0_musl",
            "name": "gel-cli",
            "version": "2.0.0",
            "version_details": {},
            "version_key": "2.0.0",
            "revision": "1",
            "build_date": "2026-08-15T00:00:00Z",
            "architecture": "x86_64",
            "slot": "",
            "installref": "https://packages.geldata.com/archive/gel-cli-2.0.0.tar.gz",
            "installrefs": [
                {
                    "ref": "https://packages.geldata.com/archive/gel-cli-2.0.0.tar.gz",
                    "type": "application/octet-stream",
                    "verification": {
                        "size": 50,
                        "sha256": "9" * 64,
                        "blake2b": "b" * 128,
                    },
                }
            ],
        }
    )
    bootstrap_index = PackageIndex(packages=(p1, p2))

    p_darwin = PackageEntry.model_validate(
        {
            "basename": "gel-cli_3.0.0_darwin",
            "name": "gel-cli",
            "version": "3.0.0",
            "version_details": {},
            "version_key": "3.0.0",
            "revision": "1",
            "build_date": "2026-08-15T00:00:00Z",
            "architecture": "aarch64",
            "slot": "",
            "installref": "https://packages.geldata.com/archive/gel-cli-3.0.0-darwin.tar.gz",
            "installrefs": [
                {
                    "ref": "https://packages.geldata.com/archive/gel-cli-3.0.0-darwin.tar.gz",
                    "type": "application/octet-stream",
                    "verification": {
                        "size": 100,
                        "sha256": DIGEST_1,
                        "blake2b": "c" * 128,
                    },
                }
            ],
        }
    )
    bootstrap = {
        ("stable", "x86_64-unknown-linux-musl"): bootstrap_index,
        ("stable", "aarch64-apple-darwin"): PackageIndex(packages=(p_darwin,)),
    }

    record = ReleaseRecord(
        source=ReleaseSource(
            repository=REPOSITORY,
            tag=TAG,
            release_id=42,
            published_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        ),
        replacements=manifest.replacements,
        indexes=(),
    )

    rendered = compose_indexes(bootstrap, [record])
    key = ("stable", "x86_64-unknown-linux-musl")
    assert key in rendered

    updated_index = PackageIndex.model_validate_json(rendered[key])
    pkg_300 = next(p for p in updated_index.packages if p.version == "3.0.0")
    pkg_200 = next(p for p in updated_index.packages if p.version == "2.0.0")

    # Rescued artifact URL is updated to GitHub download URL
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/gel-cli-x86_64-unknown-linux-musl.tar.gz"
    assert pkg_300.installrefs[0].ref == expected_url
    assert pkg_300.installref == expected_url
    assert pkg_300.installrefs[0].verification.sha256 == DIGEST_2

    # Unrelated legacy artifact is untouched
    assert (
        pkg_200.installrefs[0].ref
        == "https://packages.geldata.com/archive/gel-cli-2.0.0.tar.gz"
    )
    assert (
        pkg_200.installref
        == "https://packages.geldata.com/archive/gel-cli-2.0.0.tar.gz"
    )
    assert pkg_200.installrefs[0].verification.sha256 == "9" * 64
