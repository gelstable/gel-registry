"""Builders shared by more than one test module.

Repository and package-index fixtures live here so the tests for one lifecycle
phase can be read without the builders of another.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gel_registry.constants import CLI_PLATFORMS, capture_urls
from gel_registry.contracts import (
    CaptureEntry,
    CaptureManifest,
    IndexFragment,
    PackageIndex,
    Pointer,
    ReleaseRecord,
)
from gel_registry.digest import canonical_json, hash_bytes
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


def fixture_release(version: str = "1.0.0") -> ReleaseRecord:
    """A direct release record adding one publisher package per CLI platform."""

    tag = f"v{version}"
    prefix = f"https://github.com/gelstable/gel-cli/releases/download/{tag}/"
    fragments = []
    for platform in CLI_PLATFORMS:
        entry = package(basename="gel-cli", version=version, ref_suffix=platform)
        entry["architecture"] = platform.split("-", 1)[0]
        entry["installref"] = prefix + f"gel-cli-{platform}"
        entry["installrefs"] = [
            {
                **entry["installrefs"][0],
                "ref": entry["installref"],
            }
        ]
        fragments.append(
            IndexFragment(channel="stable", platform=platform, packages=(entry,))
        )
    return ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 100,
            "tag": tag,
            "published_at": CAPTURED_AT,
        },
        indexes=tuple(fragments),
    )


def copy_release(repo: Path, version: str = "1.0.0") -> Path:
    path = repo / "releases" / "gel-cli" / f"{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(fixture_release(version)))
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
    IndexFragment,
