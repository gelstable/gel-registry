from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest
from support import copy_release, package, write_bootstrap

from gel_registry.constants import CLI_PLATFORMS, LEGACY_PLATFORMS
from gel_registry.contracts import PackageIndex
from gel_registry.digest import canonical_json, snapshot_id
from gel_registry.render import ContestedIdentityError, build_snapshot


def test_build_snapshot_is_pointer_independent_and_deduplicates_exact_entries(
    tmp_path: Path,
) -> None:
    for channel in ("stable", "testing", "nightly"):
        write_bootstrap(
            tmp_path,
            channel,
            LEGACY_PLATFORMS[0],
            [package(version="1.0.0", ref_suffix="shared")],
        )
    release = copy_release(tmp_path)
    duplicate = tmp_path / "releases" / "gel-cli" / "duplicate.json"
    duplicate.write_bytes(release.read_bytes())

    snapshot = build_snapshot(tmp_path)
    public = tmp_path / "public"
    index_paths = sorted((public / "s" / snapshot / "index").glob("*.json"))
    assert snapshot == snapshot_id(
        {
            PurePosixPath(
                path.relative_to(public / "s" / snapshot).as_posix()
            ): path.read_bytes()
            for path in index_paths
        }
    )
    assert (public / "s" / snapshot / "registry.json").exists()
    assert not (tmp_path / "pointers" / "latest.json").exists()
    assert not (public / "registry.json").exists()
    assert not (public / "v1" / "snapshots.json").exists()

    before = {
        path.relative_to(public / "s" / snapshot): path.read_bytes()
        for path in (public / "s" / snapshot).rglob("*")
        if path.is_file()
    }
    assert build_snapshot(tmp_path) == snapshot
    after = {
        path.relative_to(public / "s" / snapshot): path.read_bytes()
        for path in (public / "s" / snapshot).rglob("*")
        if path.is_file()
    }
    assert after == before


def test_release_is_added_only_to_cli_platform_indexes(tmp_path: Path) -> None:
    for platform in LEGACY_PLATFORMS:
        write_bootstrap(tmp_path, "stable", platform, [package(version="0.9.0")])
    copy_release(tmp_path)

    snapshot = build_snapshot(tmp_path)
    index_dir = tmp_path / "public" / "s" / snapshot / "index"
    for path in index_dir.glob("stable-*.json"):
        index = PackageIndex.model_validate_json(path.read_bytes())
        versions = [package.version for package in index.packages]
        if path.name.removeprefix("stable-").removesuffix(".json") in CLI_PLATFORMS:
            assert "1.0.0" in versions
        else:
            assert "1.0.0" not in versions


def test_contested_identity_rejects_changed_release_bytes(tmp_path: Path) -> None:
    copy_release(tmp_path)
    duplicate = tmp_path / "releases" / "gel-cli" / "duplicate.json"
    data = json.loads((tmp_path / "releases" / "gel-cli" / "1.0.0.json").read_text())
    data["artifacts"][0]["sha256"] = "e" * 64
    duplicate.write_bytes(canonical_json(data))

    with pytest.raises(ContestedIdentityError):
        build_snapshot(tmp_path)


def test_promoted_at_does_not_enter_rendered_package_or_snapshot_identity(
    tmp_path: Path,
) -> None:
    release = copy_release(tmp_path)
    first = build_snapshot(tmp_path)
    first_index = next((tmp_path / "public" / "s" / first / "index").glob("*.json"))
    first_bytes = first_index.read_bytes()
    data = json.loads(release.read_text())
    data["promoted_at"] = "2036-08-15T12:00:00Z"
    release.write_bytes(canonical_json(data))

    second = build_snapshot(tmp_path)
    assert second == first
    assert first_index.read_bytes() == first_bytes
    assert b"2026-08-15T12:00:00" not in first_bytes
    assert b"2036-08-15T12:00:00" not in first_bytes
