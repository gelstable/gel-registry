from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from gel_registry.constants import CLI_PLATFORMS, LEGACY_PLATFORMS
from gel_registry.digest import canonical_json, snapshot_id
from gel_registry.render import (
    ContestedIdentityError,
    RenderError,
    build_snapshot,
    render_schemas,
    select_snapshot,
)
from gel_registry.schema import PackageIndex, Pointer

FIXTURES = Path(__file__).parent / "fixtures"


def _verification(seed: str = "a") -> dict[str, object]:
    return {"size": 3, "sha256": seed * 64, "blake2b": "b" * 128}


def _package(
    basename: str = "gel-server",
    version: str = "1.0.0",
    *,
    ref_suffix: str = "server",
    tags: dict[str, object] | None = None,
) -> dict[str, object]:
    ref = f"https://packages.geldata.com/archive/{ref_suffix}-{version}"
    installref = {
        "ref": ref,
        "type": "application/octet-stream",
        "encoding": "identity",
        "verification": _verification(),
    }
    return {
        "basename": basename,
        "name": basename,
        "version": version,
        "version_details": {
            "major": int(version.split(".")[0]),
            "minor": int(version.split(".")[1]),
            "patch": int(version.split(".")[2]),
            "prerelease": [],
            "metadata": {},
        },
        "version_key": version,
        "revision": "1",
        "build_date": "2026-08-15T00:00:00+00:00",
        "architecture": "x86_64",
        "slot": "",
        "tags": tags or {},
        "installref": ref,
        "installrefs": [installref],
    }


def _write_bootstrap(
    repo: Path,
    channel: str,
    platform: str,
    packages: list[dict[str, object]],
) -> Path:
    path = repo / "bootstrap" / f"{channel}-{platform}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(PackageIndex(packages=packages)))
    return path


def _copy_release(repo: Path, version: str = "1.0.0") -> Path:
    path = repo / "releases" / "gel-cli" / f"{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    source = FIXTURES / "releases" / "gel-cli" / "1.0.0.json"
    data = json.loads(source.read_text())
    data["version"] = version
    data["source"]["release_tag"] = f"v{version}"
    data["artifacts"] = [
        {
            **artifact,
            "url": artifact["url"].replace("v1.0.0", f"v{version}"),
        }
        for artifact in data["artifacts"]
    ]
    path.write_bytes(canonical_json(json.loads(json.dumps(data))))
    return path


def _write_pointer(repo: Path, snapshot: str) -> None:
    path = repo / "pointers" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(Pointer(snapshot=snapshot)))


def _index_bytes(repo: Path, snapshot: str, name: str) -> bytes:
    return (repo / "public" / "s" / snapshot / "index" / name).read_bytes()


def test_build_snapshot_is_pointer_independent_and_deduplicates_exact_entries(
    tmp_path: Path,
) -> None:
    for channel in ("stable", "testing", "nightly"):
        _write_bootstrap(
            tmp_path,
            channel,
            LEGACY_PLATFORMS[0],
            [_package(version="1.0.0", ref_suffix="shared")],
        )
    # Two identical release records contribute the same package identity.
    release = _copy_release(tmp_path)
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
        _write_bootstrap(tmp_path, "stable", platform, [_package(version="0.9.0")])
    _copy_release(tmp_path)

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
    _copy_release(tmp_path)
    duplicate = tmp_path / "releases" / "gel-cli" / "duplicate.json"
    data = json.loads((tmp_path / "releases" / "gel-cli" / "1.0.0.json").read_text())
    data["artifacts"][0]["sha256"] = "e" * 64
    duplicate.write_bytes(canonical_json(data))

    with pytest.raises(ContestedIdentityError):
        build_snapshot(tmp_path)


def test_build_refuses_to_overwrite_mutated_existing_snapshot(tmp_path: Path) -> None:
    _copy_release(tmp_path)
    snapshot = build_snapshot(tmp_path)
    target = next((tmp_path / "public" / "s" / snapshot).rglob("*.json"))
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(RenderError, match="immutable|drift|mismatch"):
        build_snapshot(tmp_path)


def test_selection_is_pointer_driven_and_rollback_preserves_pinned_trees(
    tmp_path: Path,
) -> None:
    _copy_release(tmp_path, "1.0.0")
    old = build_snapshot(tmp_path)
    (tmp_path / "releases" / "gel-cli" / "1.0.0.json").unlink()
    _copy_release(tmp_path, "2.0.0")
    new = build_snapshot(tmp_path)
    assert old != new

    pinned_before = {
        path.relative_to(tmp_path / "public" / "s"): path.read_bytes()
        for path in (tmp_path / "public" / "s").rglob("*")
        if path.is_file()
    }
    _write_pointer(tmp_path, old)
    select_snapshot(tmp_path)
    first_root = (tmp_path / "public" / "registry.json").read_bytes()
    first_listing = (tmp_path / "public" / "v1" / "snapshots.json").read_bytes()
    assert all(
        entry["url"].startswith(f"s/{old}/index/")
        for entry in json.loads(first_root)["indexes"]
    )
    assert json.loads(first_listing)["latest"] == old
    assert set(json.loads(first_listing)["snapshots"]) == {old, new}

    _write_pointer(tmp_path, new)
    select_snapshot(tmp_path)
    second_root = (tmp_path / "public" / "registry.json").read_bytes()
    second_listing = (tmp_path / "public" / "v1" / "snapshots.json").read_bytes()
    assert first_root != second_root
    assert first_listing != second_listing
    assert json.loads(second_listing)["latest"] == new
    pinned_after = {
        path.relative_to(tmp_path / "public" / "s"): path.read_bytes()
        for path in (tmp_path / "public" / "s").rglob("*")
        if path.is_file()
    }
    assert pinned_after == pinned_before


@pytest.mark.parametrize(
    "pointer_bytes",
    [b"{}", b'{"snapshot":"0000000000000000"}', b'{"snapshot":true}'],
)
def test_selection_rejects_missing_or_malformed_selected_snapshots(
    tmp_path: Path, pointer_bytes: bytes
) -> None:
    pointer = tmp_path / "pointers" / "latest.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(pointer_bytes)
    with pytest.raises(RenderError):
        select_snapshot(tmp_path)


def test_render_schemas_and_health_are_canonical_support_files(tmp_path: Path) -> None:
    render_schemas(tmp_path)
    schema_dir = tmp_path / "public" / "v1" / "schema"
    assert {path.name for path in schema_dir.glob("*.json")} == {
        "capture.json",
        "package-index.json",
        "release-record.json",
        "pointer.json",
        "root.json",
        "snapshot-listing.json",
    }
    assert (tmp_path / "public" / "healthz").read_bytes() == b"ok\n"
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / "public").rglob("*")
        if path.is_file()
    }
    render_schemas(tmp_path)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / "public").rglob("*")
        if path.is_file()
    }
    assert before == after
