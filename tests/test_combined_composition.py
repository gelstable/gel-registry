"""End-to-end combined composition test with real committed bootstrap data.

Proves that one candidate contains all three populations simultaneously:
1. An untouched historical bootstrap package.
2. A rescued historical install reference whose only semantic change is its URL.
3. A net-new complete package entry from a different release record.

Verifies all assertions required by the consumer rules and registry contracts.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import httpx
from pytest_httpx import HTTPXMock

from gel_registry.candidate import build_candidate
from gel_registry.contracts import (
    InstallRef,
    PackageEntry,
    PackageIndex,
    RootManifest,
)
from gel_registry.digest import blob_id, canonical_json
from gel_registry.validation import validate_local
from gel_registry.validation.drift import check_render_drift
from gel_registry.validation.report import Collector


def _manifest_with_net_new(
    repository: str,
    tag: str,
    channel: str,
    platform: str,
    package: PackageEntry,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "indexes": [
            {
                "channel": channel,
                "platform": platform,
                "packages": [package.model_dump()],
            }
        ],
    }


def _manifest_with_rescue(
    repository: str, tag: str, sha256: str, asset_name: str
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "replacements": [
            {
                "sha256": sha256,
                "url": f"https://github.com/{repository}/releases/download/{tag}/{asset_name}",
            }
        ],
    }


def _release(
    repository: str,
    release_id: int,
    tag: str,
    asset_names: list[str],
) -> dict[str, object]:
    assets = [
        {
            "name": "gel-registry.json",
            "url": f"https://api.github.com/assets/{repository}/{release_id}/manifest",
        }
    ]
    for name in asset_names:
        assets.append(
            {
                "name": name,
                "url": f"https://api.github.com/assets/{repository}/{release_id}/{name}",
            }
        )
    return {
        "id": release_id,
        "tag_name": tag,
        "published_at": "2026-08-15T00:00:00Z",
        "draft": False,
        "assets": assets,
    }


def _select_server_installref(package: PackageEntry) -> InstallRef | None:
    """Server selects application/x-tar + zstd."""
    for ref in package.installrefs:
        if ref.type == "application/x-tar" and ref.encoding == "zstd":
            return ref
    return None


def _select_cli_installref(package: PackageEntry, platform: str) -> InstallRef | None:
    """CLI selects platform media type, preferring zstd, then identity."""
    if "linux" in platform:
        media_type = "application/x-pie-executable"
    elif "darwin" in platform or "apple" in platform:
        media_type = "application/x-mach-binary"
    elif "windows" in platform:
        media_type = "application/x-dosexec"
    else:
        media_type = "application/x-executable"
    for ref in package.installrefs:
        if ref.type == media_type and ref.encoding == "zstd":
            return ref
    for ref in package.installrefs:
        if ref.type == media_type and ref.encoding == "identity":
            return ref
    return None


def _select_extension_installref(package: PackageEntry) -> InstallRef | None:
    """Extension selects application/zip + identity."""
    for ref in package.installrefs:
        if ref.type == "application/zip" and ref.encoding == "identity":
            return ref
    return None


def test_combined_composition_mirror_rescue_and_net_new(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Prove candidate contains mirror, rescue, and net-new entries together."""
    repo_root = Path(__file__).parents[1]

    # 1. Setup repository with real committed bootstrap data and source allowlist
    shutil.copytree(repo_root / "upstream", tmp_path / "upstream")
    shutil.copytree(repo_root / "bootstrap", tmp_path / "bootstrap")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        (repo_root / "sources" / "github.json").read_bytes()
    )

    channel = "stable"
    platform = "x86_64-unknown-linux-musl"
    bootstrap_index_bytes = (
        tmp_path / "bootstrap" / f"{channel}-{platform}.json"
    ).read_bytes()
    initial_bootstrap = PackageIndex.model_validate_json(bootstrap_index_bytes)

    # Population 1 (Untouched historical bootstrap packages):
    untouched_canonical_original = next(
        p
        for p in initial_bootstrap.packages
        if p.basename == "edgedb-cli" and p.version == "1.0.0-dev.669+e3439bd"
    )
    untouched_server_original = next(
        p
        for p in initial_bootstrap.packages
        if p.basename == "edgedb-server" and p.version == "2.10+3f22435"
    )
    # Also find untouched extension package: edgedb-server-6-dev8882-postgis
    extension_original = next(
        p for p in initial_bootstrap.packages if "extension" in p.tags
    )

    # Population 2 (Rescued historical install reference):
    # edgedb-cli 1.0.0+5724c50 zstd installref
    rescued_target = next(
        p
        for p in initial_bootstrap.packages
        if p.basename == "edgedb-cli" and p.version == "1.0.0+5724c50"
    )
    # Find the zstd install reference to rescue
    zstd_ref_index = next(
        i for i, ref in enumerate(rescued_target.installrefs) if ref.encoding == "zstd"
    )
    rescue_sha256 = rescued_target.installrefs[zstd_ref_index].verification.sha256
    assert rescue_sha256 is not None

    # Verify the replacement digest uniquely matches the real bootstrap
    all_digests = [
        ref.verification.sha256
        for f in (tmp_path / "bootstrap").glob("*.json")
        for pkg in PackageIndex.model_validate_json(f.read_bytes()).packages
        for ref in pkg.installrefs
        if ref.verification.sha256 is not None
    ]
    assert all_digests.count(rescue_sha256) == 1

    # Population 3 (Net-new complete package entry from a different release record):
    # gel-server 8.0.0 in stable channel
    net_new_entry = PackageEntry.model_validate(
        {
            "basename": "gel-server",
            "name": "gel-server",
            "version": "8.0.0",
            "version_details": {
                "major": 8,
                "minor": 0,
                "patch": 0,
                "prerelease": [],
                "metadata": {},
            },
            "version_key": "8.0.0",
            "revision": "202609040000",
            "build_date": "2026-09-04T00:00:00Z",
            "architecture": "x86_64",
            "slot": "8",
            "tags": {},
            "installref": "https://github.com/gelstable/gel/releases/download/v8.0.0/gel-server-8.0.0.tar.zst",
            "installrefs": [
                {
                    "ref": "https://github.com/gelstable/gel/releases/download/v8.0.0/gel-server-8.0.0.tar.zst",
                    "type": "application/x-tar",
                    "encoding": "zstd",
                    "verification": {
                        "size": 50000000,
                        "sha256": "1" * 64,
                        "blake2b": "2" * 128,
                    },
                }
            ],
        }
    )

    # Mock GitHub releases and manifest assets
    rescue_repo = "gelstable/gel-cli"
    rescue_tag = "v1.0.0-rescue"
    rescue_asset = "edgedb-cli-1.0.0+5724c50.zst"
    rescue_rel = _release(rescue_repo, 1001, rescue_tag, [rescue_asset])
    rescue_manifest_data = _manifest_with_rescue(
        rescue_repo, rescue_tag, rescue_sha256, rescue_asset
    )

    netnew_repo = "gelstable/gel"
    netnew_tag = "v8.0.0"
    netnew_asset = "gel-server-8.0.0.tar.zst"
    netnew_rel = _release(netnew_repo, 2002, netnew_tag, [netnew_asset])
    netnew_manifest_data = _manifest_with_net_new(
        netnew_repo, netnew_tag, channel, platform, net_new_entry
    )

    httpx_mock.add_response(
        url=f"https://api.github.com/repos/{rescue_repo}/releases?per_page=100",
        json=[rescue_rel],
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=f"https://api.github.com/assets/{rescue_repo}/1001/manifest",
        content=json.dumps(rescue_manifest_data).encode(),
        is_reusable=True,
    )

    httpx_mock.add_response(
        url=f"https://api.github.com/repos/{netnew_repo}/releases?per_page=100",
        json=[netnew_rel],
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=f"https://api.github.com/assets/{netnew_repo}/2002/manifest",
        content=json.dumps(netnew_manifest_data).encode(),
        is_reusable=True,
    )

    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel-postgis/releases?per_page=100",
        json=[],
        is_reusable=True,
    )

    # 2. Build candidate
    with httpx.Client() as client:
        result = build_candidate(tmp_path, client)

    assert len(result.records) == 2
    assert result.rejected == ()

    # 3. Verify pointers and roots
    latest_pointer = json.loads((tmp_path / "pointers" / "latest.json").read_bytes())
    assert latest_pointer["snapshot"] == result.snapshot

    pinned_root_path = tmp_path / "public" / "s" / result.snapshot / "registry.json"
    assert pinned_root_path.exists()
    pinned_root = RootManifest.model_validate_json(pinned_root_path.read_bytes())

    moving_root_path = tmp_path / "public" / "registry.json"
    assert moving_root_path.exists()
    moving_root = RootManifest.model_validate_json(moving_root_path.read_bytes())

    # 4. Verify resulting root and blob references are valid and content-addressed
    for root, prefix in ((pinned_root, "../../i/"), (moving_root, "i/")):
        for index_ref in root.indexes:
            assert index_ref.ref.startswith(prefix) and index_ref.ref.endswith(".json")
            blob_name = Path(index_ref.ref).name
            blob_file = tmp_path / "public" / "i" / blob_name
            assert blob_file.exists()
            blob_bytes = blob_file.read_bytes()
            assert blob_id(blob_bytes) == blob_name.removesuffix(".json")
            # Ensure valid PackageIndex
            PackageIndex.model_validate_json(blob_bytes)

    # 5. Inspect the composed index for (channel, platform)
    target_root_index = next(
        idx
        for idx in moving_root.indexes
        if idx.channel == channel and idx.platform == platform
    )
    composed_blob_bytes = (
        tmp_path / "public" / "i" / Path(target_root_index.ref).name
    ).read_bytes()
    composed_index = PackageIndex.model_validate_json(composed_blob_bytes)

    # Assertion: The untouched package remains byte-equivalent in meaning
    from gel_registry.render.compose import canonical_package

    untouched_canonical_composed = next(
        p
        for p in composed_index.packages
        if p.basename == untouched_canonical_original.basename
        and p.version == untouched_canonical_original.version
    )
    assert canonical_json(untouched_canonical_composed) == canonical_json(
        untouched_canonical_original
    )

    untouched_server_composed = next(
        p
        for p in composed_index.packages
        if p.basename == untouched_server_original.basename
        and p.version == untouched_server_original.version
    )
    assert untouched_server_composed == canonical_package(untouched_server_original)

    # Assertion: The rescued package retains every field except the intended ref
    rescued_composed = next(
        p
        for p in composed_index.packages
        if p.basename == rescued_target.basename and p.version == rescued_target.version
    )
    expected_rescued_url = f"https://github.com/{rescue_repo}/releases/download/{rescue_tag}/{rescue_asset}"
    assert rescued_composed.installrefs[zstd_ref_index].ref == expected_rescued_url
    # Other installref (identity) still points to packages.geldata.com
    assert rescued_composed.installrefs[0].ref == rescued_target.installrefs[0].ref
    # Verification metadata remains the historical metadata
    assert (
        rescued_composed.installrefs[zstd_ref_index].verification
        == rescued_target.installrefs[zstd_ref_index].verification
    )
    assert (
        rescued_composed.installrefs[0].verification
        == rescued_target.installrefs[0].verification
    )
    # Metadata fields remain unchanged
    assert rescued_composed.basename == rescued_target.basename
    assert rescued_composed.version == rescued_target.version
    assert rescued_composed.version_details == rescued_target.version_details
    assert rescued_composed.version_key == rescued_target.version_key
    assert rescued_composed.revision == rescued_target.revision
    assert rescued_composed.build_date == rescued_target.build_date
    assert rescued_composed.architecture == rescued_target.architecture
    assert rescued_composed.slot == rescued_target.slot
    assert rescued_composed.tags == rescued_target.tags

    # Assertion: The net-new package appears only in its declared channel/platform
    net_new_composed = next(
        p
        for p in composed_index.packages
        if p.basename == net_new_entry.basename and p.version == net_new_entry.version
    )
    assert canonical_json(net_new_composed) == canonical_json(net_new_entry)

    for idx in moving_root.indexes:
        if idx.channel == channel and idx.platform == platform:
            continue
        other_blob = (tmp_path / "public" / "i" / Path(idx.ref).name).read_bytes()
        other_index = PackageIndex.model_validate_json(other_blob)
        assert not any(
            p.basename == net_new_entry.basename and p.version == net_new_entry.version
            for p in other_index.packages
        )

    # 6. Consumer rules assertions (Gel CLI behavior)
    # Rule a: Server selects application/x-tar + zstd
    server_ref = _select_server_installref(untouched_server_composed)
    assert server_ref is not None
    assert server_ref.type == "application/x-tar" and server_ref.encoding == "zstd"

    # Rule b: CLI selects platform media type, preferring zstd, then identity
    cli_ref = _select_cli_installref(rescued_composed, platform)
    assert cli_ref is not None
    assert cli_ref.encoding == "zstd"
    assert cli_ref.ref == expected_rescued_url

    # Rule c: Extension selects application/zip + identity
    extension_composed = next(
        p
        for p in composed_index.packages
        if p.basename == extension_original.basename
        and p.version == extension_original.version
    )
    ext_ref = _select_extension_installref(extension_composed)
    assert ext_ref is not None
    assert ext_ref.type == "application/zip" and ext_ref.encoding == "identity"

    # Rule d: Legacy package-root clients request
    # /archive/.jsonindexes/<platform>[.<channel>].json
    vercel_config = tomllib.loads(
        (tmp_path / "vercel.toml").read_text(encoding="utf-8")
    )
    rewrites = {
        r["source"]: r["destination"] for r in vercel_config.get("rewrites", [])
    }
    expected_blob_dest = f"/i/{Path(target_root_index.ref).name}"
    assert rewrites[f"/archive/.jsonindexes/{platform}.json"] == expected_blob_dest

    nightly_root_index = next(
        idx
        for idx in moving_root.indexes
        if idx.channel == "nightly" and idx.platform == platform
    )
    assert (
        rewrites[f"/archive/.jsonindexes/{platform}.nightly.json"]
        == f"/i/{Path(nightly_root_index.ref).name}"
    )

    # Rule e: Manifest-source clients load registry.json and resolve its index refs
    resolved_index_path = tmp_path / "public" / target_root_index.ref.lstrip("/")
    assert resolved_index_path.exists()
    resolved_index = PackageIndex.model_validate_json(resolved_index_path.read_bytes())
    assert len(resolved_index.packages) == len(composed_index.packages)

    # 7. Re-running candidate construction is idempotent
    with httpx.Client() as client:
        repeated = build_candidate(tmp_path, client)
    assert repeated.snapshot == result.snapshot
    assert repeated.records == ()
    assert repeated.rejected == ()

    # 8. Offline validation and render-drift checks pass
    local_report = validate_local(tmp_path)
    assert local_report.ok is True, local_report.errors
    assert local_report.errors == ()

    collector = Collector()
    check_render_drift(tmp_path, collector)
    drift_report = collector.report()
    assert drift_report.ok is True, drift_report.errors
    assert drift_report.errors == ()
