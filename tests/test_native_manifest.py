"""Native release contract and publisher boundary compatibility."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from gel_registry.contracts import ReleaseManifest, ReleaseRecord
from gel_registry.digest import canonical_json
from gel_registry.gather import GatherResult, gather_missing, load_repositories

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


@pytest.fixture
def gather(tmp_path: Path) -> Any:
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "github.json").write_bytes(
        canonical_json(
            {
                "schema_version": 2,
                "repositories": [REPOSITORY],
                "native_package_names": {REPOSITORY: ["^gel-cli$"]},
            }
        )
    )

    def collect(
        value: dict[str, Any], tag: str = TAG, asset: str = ASSET
    ) -> GatherResult:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/releases"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 1,
                            "tag_name": tag,
                            "draft": False,
                            "published_at": "2026-09-30T00:00:00Z",
                            "assets": [
                                {
                                    "name": asset,
                                    "url": "https://api.github.com/assets/1",
                                },
                                {
                                    "name": "gel-registry.json",
                                    "url": "https://api.github.com/assets/2",
                                },
                            ],
                        }
                    ],
                )
            return httpx.Response(200, content=canonical_json(value))

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            return gather_missing(tmp_path, client)

    return collect


def test_existing_records_round_trip_byte_for_byte() -> None:
    paths = list((ROOT / "releases").rglob("*.json"))
    assert paths
    for path in paths:
        raw = path.read_bytes()
        assert canonical_json(ReleaseRecord.model_validate_json(raw)) == raw


@pytest.mark.parametrize("asset", [ASSET, "gel-cli-8.0.0-1-x86_64.rpm"])
def test_native_only_manifest_binds_to_release_inventory(
    gather: Any, asset: str
) -> None:
    result = gather(manifest(asset=asset), asset=asset)
    assert not result.rejected
    record = result.records[0]
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
    gather: Any, character: str, component: str
) -> None:
    tag = f"pkg{character}version" if component == "tag" else TAG
    asset = f"gel{character}cli.deb" if component == "asset" else ASSET
    result = gather(manifest(tag, asset), tag, asset)
    assert not result.records and result.rejected


@pytest.mark.parametrize(
    "tag,repository", [("another-tag", REPOSITORY), (TAG, "gelstable/gel")]
)
def test_native_url_must_match_source(gather: Any, tag: str, repository: str) -> None:
    result = gather(manifest(tag=tag, repository=repository))
    assert not result.records and result.rejected


def test_native_asset_must_exist_in_inventory(gather: Any) -> None:
    result = gather(manifest(), asset="other.deb")
    assert not result.records
    assert "absent from the release" in result.rejected[0].reason


@pytest.mark.parametrize("asset", ["gel.zip", ".gel.deb", "-gel.rpm", "%67el.deb"])
def test_native_asset_requires_safe_package_filename(gather: Any, asset: str) -> None:
    result = gather(manifest(asset=asset), asset=asset)
    assert not result.records and result.rejected


@pytest.mark.parametrize("channel", ["stable", "testing"])
def test_publisher_channel_is_rejected(channel: str) -> None:
    value = manifest()
    value["native"]["channel"] = channel
    with pytest.raises(ValidationError, match="channel"):
        ReleaseManifest.model_validate(value)


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


def test_empty_native_section_preserves_portable_tag_acceptance(gather: Any) -> None:
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
    result = gather(data, tag=tag, asset="portable")
    assert not result.rejected
    record = result.records[0]
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
