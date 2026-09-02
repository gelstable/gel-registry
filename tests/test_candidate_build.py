"""Candidate construction writes source records and publication output together."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from pytest_httpx import HTTPXMock
from support import package, write_bootstrap

from gel_registry.candidate import build_candidate
from gel_registry.contracts import RootManifest
from gel_registry.digest import canonical_json


def _release(repository: str, release_id: int, tag: str) -> dict[str, object]:
    return {
        "id": release_id,
        "tag_name": tag,
        "published_at": "2026-08-15T00:00:00Z",
        "draft": False,
        "assets": [
            {
                "name": "gel-registry.json",
                "url": f"https://api.github.com/assets/{release_id}",
            },
            {
                "name": "artifact",
                "url": f"https://api.github.com/assets/{release_id}/artifact",
            },
        ],
    }


def _manifest(
    repository: str, tag: str, package_data: dict[str, object]
) -> dict[str, object]:
    url = f"https://github.com/{repository}/releases/download/{tag}/artifact"
    package_data["installref"] = url
    package_data["installrefs"] = [
        {
            "ref": url,
            "type": "application/octet-stream",
            "encoding": "identity",
            "verification": {"size": 3, "sha256": "a" * 64, "blake2b": "b" * 128},
        }
    ]
    return {
        "schema_version": 1,
        "indexes": [
            {
                "channel": "stable",
                "platform": "x86_64-unknown-linux-gnu",
                "packages": [package_data],
            }
        ],
    }


def _rescue_manifest(repository: str, tag: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "replacements": [
            {
                "sha256": "a" * 64,
                "url": f"https://github.com/{repository}/releases/download/{tag}/artifact",
            }
        ],
    }


def _record_paths(repo: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(repo).as_posix()
            for path in (repo / "releases").rglob("*.json")
        )
    )


def test_build_candidate_publishes_rescue_and_net_new_records_together(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """A candidate keeps the manifest inputs and selected output in one state."""
    rescue = package("gel-server", "1.0.0", ref_suffix="old")
    write_bootstrap(tmp_path, "stable", "x86_64-unknown-linux-gnu", [rescue])
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json(
            {
                "repositories": ["gelstable/gel", "gelstable/gel-cli"],
                "schema_version": 1,
            }
        )
    )
    for repository, release in (
        ("gelstable/gel", _release("gelstable/gel", 202, "v2")),
        ("gelstable/gel-cli", _release("gelstable/gel-cli", 101, "v1")),
    ):
        httpx_mock.add_response(
            url=f"https://api.github.com/repos/{repository}/releases?per_page=100",
            json=[release],
            is_reusable=True,
        )
    httpx_mock.add_response(
        url="https://api.github.com/assets/202",
        content=json.dumps(_rescue_manifest("gelstable/gel", "v2")).encode(),
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/101",
        content=json.dumps(
            _manifest(
                "gelstable/gel-cli", "v1", package("gel-cli", "2.0.0", ref_suffix="new")
            )
        ).encode(),
    )

    with httpx.Client() as client:
        result = build_candidate(tmp_path, client)

    assert _record_paths(tmp_path) == (
        "releases/gelstable/gel-cli/101.json",
        "releases/gelstable/gel/202.json",
    )
    assert (
        json.loads((tmp_path / "pointers" / "latest.json").read_bytes())["snapshot"]
        == result.snapshot
    )
    manifest = RootManifest.model_validate_json(
        (tmp_path / "public" / "s" / result.snapshot / "registry.json").read_bytes()
    )
    blob = tmp_path / "public" / "i" / Path(manifest.indexes[0].ref).name
    packages = json.loads(blob.read_bytes())["packages"]
    assert {entry["version"] for entry in packages} == {"1.0.0", "2.0.0"}
    assert (
        next(entry for entry in packages if entry["version"] == "1.0.0")["installref"]
        == "https://github.com/gelstable/gel/releases/download/v2/artifact"
    )

    with httpx.Client() as client:
        repeated = build_candidate(tmp_path, client)
    assert repeated.snapshot == result.snapshot
