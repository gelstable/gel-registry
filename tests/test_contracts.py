from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from gel_registry.constants import (
    CAPTURE_ID,
    CLI_PLATFORMS,
    ORIGIN,
    capture_urls,
)
from gel_registry.contracts import (
    Artifact,
    CaptureEntry,
    CaptureManifest,
    InstallRef,
    PackageIndex,
    ReleaseRecord,
)


def _cli_artifacts() -> list[Artifact]:
    artifacts: list[Artifact] = []
    for platform in CLI_PLATFORMS:
        media_type = (
            "application/x-mach-binary"
            if platform.endswith("-apple-darwin")
            else "application/x-dosexec"
            if platform.endswith("-windows-msvc")
            else "application/x-pie-executable"
        )
        for encoding in ("identity", "zstd"):
            artifacts.append(
                Artifact(
                    platform=platform,
                    encoding=encoding,
                    media_type=media_type,
                    url="https://github.com/gelstable/gel-cli/releases/download/v1.2.3/a",
                    size=1,
                    sha256="a" * 64,
                    blake2b="b" * 128,
                )
            )
    return artifacts


def _release_data(version: str, artifacts: list[Artifact]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "gel-cli",
        "channel": "stable",
        "version": version,
        "source": {
            "repository": "gelstable/gel-cli",
            "release_tag": f"v{version}",
            "release_id": 1,
        },
        "promoted_at": datetime(2026, 8, 15, tzinfo=UTC),
        "artifacts": artifacts,
    }


def test_capture_manifest_requires_the_exact_capture_matrix() -> None:
    entries = [
        CaptureEntry(channel=channel, platform=platform, url=url, status=404)
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
        (channel, platform) for channel, platform, _ in capture_urls()
    ]
    with pytest.raises(ValidationError):
        CaptureManifest(
            schema_version=1,
            capture=CAPTURE_ID,
            origin=ORIGIN,
            captured_at=datetime(2026, 8, 15, tzinfo=UTC),
            entries=entries[:-1],
        )


def test_installref_rejects_unsafe_urls(verification_data: dict[str, object]) -> None:
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


def test_package_index_round_trips_the_legacy_package_shape(
    package_data: dict[str, object],
) -> None:
    index = PackageIndex.model_validate_json(json.dumps({"packages": [package_data]}))

    assert index.model_dump(mode="json") == {"packages": [package_data]}


def test_release_record_rejects_a_leading_v() -> None:
    with pytest.raises(ValidationError):
        ReleaseRecord(**_release_data("v1.2.3", _cli_artifacts()))


@pytest.mark.parametrize(
    ("version", "valid"),
    [("1.2.3-01", False), ("1.2.3-foo+bar", True)],
)
def test_release_record_applies_semver_prerelease_rules(
    version: str, valid: bool
) -> None:
    data = _release_data(version, _cli_artifacts())

    if valid:
        assert ReleaseRecord(**data).version == version
    else:
        with pytest.raises(ValidationError):
            ReleaseRecord(**data)


def test_release_record_requires_the_complete_cli_artifact_matrix() -> None:
    artifacts = _cli_artifacts()

    record = ReleaseRecord(**_release_data("1.2.3", artifacts))

    assert [
        (artifact.platform, artifact.encoding) for artifact in record.artifacts
    ] == [
        (platform, encoding)
        for platform in CLI_PLATFORMS
        for encoding in ("identity", "zstd")
    ]
    with pytest.raises(ValidationError):
        ReleaseRecord(**_release_data("1.2.3", artifacts[:-1]))
