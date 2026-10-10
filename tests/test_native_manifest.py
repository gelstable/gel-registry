"""Native release contract and publisher boundary compatibility."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gel_registry.contracts import ReleaseManifest, ReleaseRecord
from gel_registry.digest import canonical_json
from gel_registry.gather import _bind_manifest, load_repositories
from gel_registry.github import DiscoveredAsset, DiscoveredRelease

ROOT = Path(__file__).parents[1]
REPOSITORY = "gelstable/gel-cli"
TAG = "pkg-gel-cli-8.0.0-1"
ASSET = "gel-cli-8.0.0-1-amd64.deb"


def manifest(
    tag: str = TAG, asset: str = ASSET, repository: str = REPOSITORY
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "native": {
            "packages": [
                {
                    "url": f"https://github.com/{repository}/releases/download/{tag}/{asset}",
                    "sha256": "a" * 64,
                    "size": 123,
                }
            ],
        },
    }


def release(tag: str = TAG, asset: str = ASSET) -> DiscoveredRelease:
    return DiscoveredRelease(
        REPOSITORY,
        1,
        tag,
        datetime(2026, 9, 30, tzinfo=UTC),
        False,
        (DiscoveredAsset(asset, "https://api.github.com/assets/1"),),
    )


def test_existing_records_round_trip_byte_for_byte() -> None:
    paths = list((ROOT / "releases").rglob("*.json"))
    assert paths
    for path in paths:
        raw = path.read_bytes()
        assert canonical_json(ReleaseRecord.model_validate_json(raw)) == raw


@pytest.mark.parametrize("asset", [ASSET, "gel-cli-8.0.0-1-x86_64.rpm"])
def test_native_only_manifest_binds_to_release_inventory(asset: str) -> None:
    record = _bind_manifest(canonical_json(manifest(asset=asset)), release(asset=asset))
    assert record.schema_version == 2
    assert record.model_dump()["native"]["packages"][0]["size"] == 123


def test_combined_manifest_preserves_portable_replacements() -> None:
    value = manifest()
    value["replacements"] = [
        {
            "sha256": "b" * 64,
            "url": f"https://github.com/{REPOSITORY}/releases/download/{TAG}/portable+binary",
        }
    ]
    parsed = ReleaseManifest.model_validate(value)
    assert len(parsed.replacements) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": 2},
        {"schema_version": 2, "native": {"packages": []}},
        {
            "schema_version": 1,
            "native": None,
            "replacements": [{"sha256": "a" * 64, "url": "x"}],
        },
    ],
)
def test_empty_changes_and_native_on_v1_are_rejected(value: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReleaseManifest.model_validate(value)


@pytest.mark.parametrize("character", ["+", "~", "%", "/"])
@pytest.mark.parametrize("component", ["tag", "asset"])
def test_native_tag_and_asset_reject_unsafe_characters(
    character: str, component: str
) -> None:
    tag = f"pkg{character}version" if component == "tag" else TAG
    asset = f"gel{character}cli.deb" if component == "asset" else ASSET
    with pytest.raises(ValueError):
        _bind_manifest(canonical_json(manifest(tag, asset)), release(tag, asset))


@pytest.mark.parametrize(
    "tag,repository", [("another-tag", REPOSITORY), (TAG, "gelstable/gel")]
)
def test_native_url_must_match_source(tag: str, repository: str) -> None:
    with pytest.raises(ValueError):
        _bind_manifest(
            canonical_json(manifest(tag=tag, repository=repository)), release()
        )


def test_native_asset_must_exist_in_inventory() -> None:
    with pytest.raises(ValueError, match="absent from the release"):
        _bind_manifest(canonical_json(manifest()), release(asset="other.deb"))


@pytest.mark.parametrize("asset", ["gel.zip", ".gel.deb", "-gel.rpm", "%67el.deb"])
def test_native_asset_requires_safe_package_filename(asset: str) -> None:
    with pytest.raises(ValueError):
        _bind_manifest(canonical_json(manifest(asset=asset)), release(asset=asset))


def test_source_allowlist_v2_loads_for_discovery(tmp_path: Path) -> None:
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources/github.json").write_bytes(
        canonical_json(
            {
                "schema_version": 2,
                "repositories": [REPOSITORY],
                "native_package_names": {REPOSITORY: ["^gel-cli$"]},
            }
        )
    )
    assert load_repositories(tmp_path) == (REPOSITORY,)


@pytest.mark.parametrize("name", ["release-manifest.json", "release-record.json"])
def test_approved_v1_schema_migration_requires_opt_in(
    tmp_path: Path, name: str
) -> None:
    from gel_registry.render import RenderError
    from gel_registry.render.schemas import render_schemas

    schema = tmp_path / "public/v1/schema" / name
    schema.parent.mkdir(parents=True)
    schema.write_bytes(
        (ROOT / "tests/fixtures/native-predecessors" / name).read_bytes()
    )
    with pytest.raises(RenderError, match="immutable support document mismatch"):
        render_schemas(tmp_path)
    render_schemas(tmp_path, allow_release_record_migration=True)
    model = ReleaseManifest if name == "release-manifest.json" else ReleaseRecord
    assert schema.read_bytes() == canonical_json(model.model_json_schema())


@pytest.mark.parametrize("channel", ["stable", "testing"])
def test_publisher_channel_is_rejected(channel: str) -> None:
    value = manifest()
    value["native"]["channel"] = channel
    with pytest.raises(ValidationError, match="channel"):
        ReleaseManifest.model_validate(value)


def test_native_manifest_needs_no_publisher_channel() -> None:
    value = manifest()
    assert ReleaseManifest.model_validate(value).native is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
        ("size", -1),
        ("size", True),
        ("size", "123"),
    ],
)
def test_native_metadata_is_validated(field: str, value: Any) -> None:
    data = manifest()
    data["native"]["packages"][0][field] = value
    with pytest.raises(ValidationError, match=field):
        ReleaseManifest.model_validate(data)


def test_version_two_accepts_portable_only_indexes(
    package_data: dict[str, object],
) -> None:
    data = {
        "schema_version": 2,
        "indexes": [
            {
                "channel": "stable",
                "platform": "x86_64-unknown-linux-musl",
                "packages": [package_data],
            }
        ],
    }
    assert ReleaseManifest.model_validate(data).indexes
    assert ReleaseManifest.model_validate(
        {
            **data,
            "native": {"packages": []},
        }
    ).indexes
    assert ReleaseManifest.model_validate(
        {**data, "native": manifest()["native"]}
    ).native


def test_unknown_native_channel_is_rejected() -> None:
    data = manifest()
    data["native"]["channel"] = "nightly"
    with pytest.raises(ValidationError, match="channel"):
        ReleaseManifest.model_validate(data)


def test_empty_native_section_preserves_portable_tag_acceptance() -> None:
    tag = "v8.0+build"
    data = {
        "schema_version": 2,
        "native": {"packages": []},
        "replacements": [
            {
                "sha256": "a" * 64,
                "url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/portable",
            }
        ],
    }
    record = _bind_manifest(canonical_json(data), release(tag=tag, asset="portable"))
    assert record.source.tag == tag


@pytest.mark.parametrize("version", [True, 1.0, 2.0])
def test_source_schema_version_requires_integer(tmp_path: Path, version: Any) -> None:
    (tmp_path / "sources").mkdir()
    data = {"schema_version": version, "repositories": [REPOSITORY]}
    if version == 2:
        data["native_package_names"] = {REPOSITORY: ["^gel-cli$"]}
    (tmp_path / "sources/github.json").write_bytes(canonical_json(data))
    with pytest.raises(ValueError, match="unsupported schema version"):
        load_repositories(tmp_path)
