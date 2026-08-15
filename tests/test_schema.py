from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gel_registry.constants import (
    CAPTURE_ID,
    CHANNELS,
    CLI_PLATFORMS,
    LEGACY_PLATFORMS,
    ORIGIN,
    capture_urls,
)
from gel_registry.schema import (
    Artifact,
    CaptureEntry,
    CaptureManifest,
    InstallRef,
    PackageIndex,
    Pointer,
    ReleaseRecord,
    SnapshotListing,
)


def test_capture_urls_are_the_fixed_channel_platform_matrix() -> None:
    urls = capture_urls()

    assert len(urls) == 24
    assert urls[0] == (
        "stable",
        LEGACY_PLATFORMS[0],
        f"{ORIGIN}/archive/.jsonindexes/{LEGACY_PLATFORMS[0]}.json",
    )
    assert urls[8] == (
        "testing",
        LEGACY_PLATFORMS[0],
        f"{ORIGIN}/archive/.jsonindexes/{LEGACY_PLATFORMS[0]}.testing.json",
    )
    assert urls[16] == (
        "nightly",
        LEGACY_PLATFORMS[0],
        f"{ORIGIN}/archive/.jsonindexes/{LEGACY_PLATFORMS[0]}.nightly.json",
    )
    assert [(channel, platform) for channel, platform, _ in urls] == [
        (channel, platform) for channel in CHANNELS for platform in LEGACY_PLATFORMS
    ]


def test_capture_entry_status_controls_byte_metadata() -> None:
    good = CaptureEntry(
        channel="stable",
        platform=LEGACY_PLATFORMS[0],
        url=capture_urls()[0][2],
        status=200,
        final_url=capture_urls()[0][2],
        size=3,
        sha256="a" * 64,
        blake2b="b" * 128,
        path="indexes/stable-x86_64-unknown-linux-gnu.json",
    )
    absent = CaptureEntry(
        channel="stable",
        platform=LEGACY_PLATFORMS[1],
        url=capture_urls()[1][2],
        status=404,
    )

    assert good.status == 200
    assert good.size == 3
    assert absent.status == 404
    assert absent.path is None

    with pytest.raises(ValidationError):
        CaptureEntry(
            channel="stable",
            platform=LEGACY_PLATFORMS[0],
            url=capture_urls()[0][2],
            status=200,
            final_url=capture_urls()[0][2],
            size=3,
            sha256="a" * 64,
            path="indexes/stable-x86_64-unknown-linux-gnu.json",
        )

    with pytest.raises(ValidationError):
        CaptureEntry(
            channel="stable",
            platform=LEGACY_PLATFORMS[1],
            url=capture_urls()[1][2],
            status=404,
            path="indexes/stable-x86_64-unknown-linux-musl.json",
        )


def test_capture_manifest_sorts_and_requires_exact_matrix() -> None:
    entries = [
        CaptureEntry(
            channel=channel,
            platform=platform,
            url=url,
            status=404,
        )
        for channel, platform, url in reversed(capture_urls())
    ]
    manifest = CaptureManifest(
        schema_version=1,
        capture=CAPTURE_ID,
        origin=ORIGIN,
        captured_at=datetime(2026, 8, 15, tzinfo=UTC),
        entries=entries,
    )

    assert [(entry.channel, entry.platform) for entry in manifest.entries] == [
        (channel, platform) for channel in CHANNELS for platform in LEGACY_PLATFORMS
    ]

    with pytest.raises(ValidationError):
        CaptureManifest(
            schema_version=1,
            capture=CAPTURE_ID,
            origin=ORIGIN,
            captured_at=datetime(2026, 8, 15, tzinfo=UTC),
            entries=entries[:-1],
        )


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"channel": "preview", "platform": LEGACY_PLATFORMS[0]},
        {"channel": "stable", "platform": "mips-unknown-linux-gnu"},
        {
            "channel": "stable",
            "platform": LEGACY_PLATFORMS[0],
            "url": "https://evil.example/index.json",
        },
        {
            "channel": "stable",
            "platform": LEGACY_PLATFORMS[0],
            "url": capture_urls()[0][2],
            "sha256": "not-a-digest",
        },
    ],
)
def test_capture_entry_rejects_unknown_or_unsafe_values(
    bad_entry: dict[str, object],
) -> None:
    data: dict[str, object] = {
        "channel": "stable",
        "platform": LEGACY_PLATFORMS[0],
        "url": capture_urls()[0][2],
        "status": 404,
    }
    data.update(bad_entry)
    with pytest.raises(ValidationError):
        CaptureEntry(**data)


def test_installref_rejects_non_https_urls_fragments_and_userinfo(
    verification_data: dict[str, object],
) -> None:
    base = {
        "type": "application/x-pie-executable",
        "encoding": "identity",
        "verification": verification_data,
    }
    for ref in (
        "http://packages.geldata.com/archive/example",
        "https://packages.geldata.com/archive/example#fragment",
        "https://user:password@packages.geldata.com/archive/example",
    ):
        with pytest.raises(ValidationError):
            InstallRef(ref=ref, **base)


def test_package_index_rejects_duplicate_package_identity(
    package_data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PackageIndex(packages=[package_data, package_data])


def test_release_record_requires_semver_without_leading_v() -> None:
    artifact = Artifact(
        platform=CLI_PLATFORMS[0],
        encoding="identity",
        media_type="application/x-pie-executable",
        url="https://github.com/gelstable/gel-cli/releases/download/v1.2.3/cli",
        size=1,
        sha256="a" * 64,
        blake2b="b" * 128,
    )
    data = {
        "schema_version": 1,
        "product": "gel-cli",
        "channel": "stable",
        "version": "v1.2.3",
        "source": {
            "repository": "gelstable/gel-cli",
            "release_tag": "v1.2.3",
            "release_id": 1,
        },
        "promoted_at": datetime(2026, 8, 15, tzinfo=UTC),
        "artifacts": [artifact],
    }
    with pytest.raises(ValidationError):
        ReleaseRecord(**data)


def test_release_record_requires_the_ten_cli_installrefs() -> None:
    def artifact(platform: str, encoding: str) -> Artifact:
        media_type = (
            "application/x-mach-binary"
            if platform.endswith("-apple-darwin")
            else "application/x-dosexec"
            if platform.endswith("-windows-msvc")
            else "application/x-pie-executable"
        )
        return Artifact(
            platform=platform,
            encoding=encoding,
            media_type=media_type,
            url="https://github.com/gelstable/gel-cli/releases/download/v1.2.3/a",
            size=1,
            sha256="a" * 64,
            blake2b="b" * 128,
        )

    data = {
        "schema_version": 1,
        "product": "gel-cli",
        "channel": "stable",
        "version": "1.2.3",
        "source": {
            "repository": "gelstable/gel-cli",
            "release_tag": "v1.2.3",
            "release_id": 1,
        },
        "promoted_at": datetime(2026, 8, 15, tzinfo=UTC),
        "artifacts": [artifact(CLI_PLATFORMS[0], "identity")],
    }
    with pytest.raises(ValidationError):
        ReleaseRecord(**data)


def test_snapshot_identifiers_are_sixteen_lowercase_hex_characters() -> None:
    with pytest.raises(ValidationError):
        Pointer(snapshot="0123456789ABCDEf")
    with pytest.raises(ValidationError):
        SnapshotListing(latest="0123456789abcdef0", snapshots=[])


def test_models_are_frozen_and_reject_unknown_fields(
    package_index_data: dict[str, object],
) -> None:
    index = PackageIndex(**package_index_data)
    with pytest.raises(ValidationError):
        index.packages = ()
    with pytest.raises(ValidationError):
        PackageIndex(**package_index_data, unexpected=True)  # type: ignore[call-arg]
