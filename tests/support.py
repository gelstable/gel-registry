"""Builders shared by more than one test module.

Repository, package-index and GitHub payload fixtures live here so the tests
for one lifecycle phase can be read without the builders of another.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from gel_registry.constants import CLI_PLATFORMS, capture_urls
from gel_registry.contracts import (
    Artifact,
    CaptureEntry,
    CaptureManifest,
    PackageIndex,
    Pointer,
    ReleaseRecord,
    ReleaseSource,
)
from gel_registry.digest import canonical_json, hash_bytes
from gel_registry.github import GITHUB_REPOSITORY, asset_name
from gel_registry.normalize import normalize_capture
from gel_registry.render import build_snapshot, render_schemas, select_snapshot

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED_AT = datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)


def verification(seed: str = "a") -> dict[str, object]:
    return {"size": 3, "sha256": seed * 64, "blake2b": "b" * 128}


def package(
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
        "verification": verification(),
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


def write_bootstrap(
    repo: Path,
    channel: str,
    platform: str,
    packages: list[dict[str, object]],
) -> Path:
    path = repo / "bootstrap" / f"{channel}-{platform}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(PackageIndex(packages=packages)))
    return path


def copy_release(repo: Path, version: str = "1.0.0") -> Path:
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


def write_pointer(repo: Path, snapshot: str) -> None:
    path = repo / "pointers" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(Pointer(snapshot=snapshot)))


def complete_repository(root: Path, package_index_data: dict[str, object]) -> None:
    """Build a repository that passes every offline validation layer."""

    index = PackageIndex.model_validate(package_index_data)
    body = canonical_json(index)
    digests = hash_bytes(body)
    capture_root = (
        root / "upstream" / "packages.geldata.com" / "legacy-2026-08-bootstrap"
    )
    (capture_root / "indexes").mkdir(parents=True)
    entries: list[CaptureEntry] = []
    for channel, platform, url in capture_urls():
        path = f"indexes/{channel}-{platform}.json"
        (capture_root / path).write_bytes(body)
        entries.append(
            CaptureEntry(
                channel=channel,
                platform=platform,
                url=url,
                status=200,
                final_url=url,
                size=digests.size,
                sha256=digests.sha256,
                blake2b=digests.blake2b,
                path=path,
            )
        )
    manifest = CaptureManifest(
        capture="legacy-2026-08-bootstrap",
        origin="https://packages.geldata.com",
        captured_at=CAPTURED_AT,
        entries=tuple(entries),
    )
    (capture_root / "capture.json").write_bytes(canonical_json(manifest))

    normalize_capture(capture_root, root / "bootstrap")
    snapshot = build_snapshot(root)
    write_pointer(root, snapshot)
    select_snapshot(root)
    render_schemas(root)


def release_record(version: str = "1.2.3") -> ReleaseRecord:
    artifacts: list[Artifact] = []
    for platform in CLI_PLATFORMS:
        for encoding in ("identity", "zstd"):
            name = asset_name(platform, encoding)
            artifacts.append(
                Artifact(
                    platform=platform,
                    encoding=encoding,
                    media_type=(
                        "application/x-dosexec"
                        if platform.endswith("-windows-msvc")
                        else "application/x-mach-binary"
                        if platform.endswith("-apple-darwin")
                        else "application/x-pie-executable"
                    ),
                    url=(
                        "https://github.com/gelstable/gel-cli/releases/download/"
                        f"v{version}/{name}"
                    ),
                    size=3,
                    sha256="a" * 64,
                    blake2b="b" * 128,
                )
            )
    return ReleaseRecord(
        product="gel-cli",
        channel="stable",
        version=version,
        source=ReleaseSource(
            repository="gelstable/gel-cli",
            release_tag=f"v{version}",
            release_id=123,
        ),
        promoted_at=CAPTURED_AT,
        artifacts=tuple(artifacts),
    )


def github_asset(name: str, release_id: int = 1) -> dict[str, object]:
    return {
        "id": release_id,
        "name": name,
        "url": f"https://api.github.com/repos/{GITHUB_REPOSITORY}/assets/{release_id}",
        "browser_download_url": (
            f"https://github.com/{GITHUB_REPOSITORY}/releases/download/v1.2.3/{name}"
        ),
    }


def github_release(
    tag: str = "v1.2.3",
    release_id: int = 100,
    *,
    draft: bool = False,
    prerelease: bool = False,
    owner: str = GITHUB_REPOSITORY,
) -> dict[str, object]:
    assets = [
        github_asset(asset_name(platform, "identity"), index)
        for index, platform in enumerate(CLI_PLATFORMS, 1)
    ]
    assets.extend(
        {
            **asset,
            "id": index + 10,
            "url": (
                f"https://api.github.com/repos/{GITHUB_REPOSITORY}/assets/{index + 10}"
            ),
            "name": f"{asset['name']}.zst",
        }
        for index, asset in enumerate(assets[:], 1)
    )
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "url": f"https://api.github.com/repos/{owner}/releases/{release_id}",
        "html_url": f"https://github.com/{owner}/releases/tag/{tag}",
        "assets": assets,
    }


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))
