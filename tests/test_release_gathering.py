"""Gather direct release manifests from the GitHub allowlist."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from gel_registry.contracts import ReleaseRecord
from gel_registry.digest import canonical_json
from gel_registry.gather import gather_missing


def _manifest(repository: str, tag: str, name: str = "artifact") -> dict[str, object]:
    return {
        "schema_version": 1,
        "indexes": [
            {
                "channel": "stable",
                "platform": "x86_64-unknown-linux-gnu",
                "packages": [
                    {
                        "basename": "gel-cli",
                        "name": "gel-cli",
                        "version": "1.2.3",
                        "version_details": {
                            "major": 1,
                            "minor": 2,
                            "patch": 3,
                            "prerelease": [],
                            "metadata": {},
                        },
                        "version_key": "1.2.3",
                        "revision": "1",
                        "build_date": "2026-08-15T00:00:00Z",
                        "architecture": "x86_64",
                        "slot": "",
                        "tags": {},
                        "installref": (
                            f"https://github.com/{repository}/releases/download/{tag}/{name}"
                        ),
                        "installrefs": [
                            {
                                "ref": (
                                    f"https://github.com/{repository}/releases/download/{tag}/{name}"
                                ),
                                "type": "application/octet-stream",
                                "encoding": "identity",
                                "verification": {
                                    "size": 3,
                                    "sha256": "a" * 64,
                                    "blake2b": "b" * 128,
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _release(
    repository: str,
    release_id: int,
    tag: str,
    *,
    draft: bool = False,
    manifest: bool = True,
) -> dict[str, object]:
    assets: list[dict[str, str]] = []
    if manifest:
        assets.append(
            {
                "name": "gel-registry.json",
                "url": f"https://api.github.com/assets/{repository}/{release_id}",
            }
        )
    assets.append(
        {
            "name": "artifact",
            "url": f"https://api.github.com/assets/{repository}/{release_id}/artifact",
        }
    )
    return {
        "id": release_id,
        "tag_name": tag,
        "published_at": "2026-08-15T00:00:00Z",
        "draft": draft,
        "assets": assets,
    }


def _add_release_list(
    httpx_mock: HTTPXMock, repository: str, releases: list[dict[str, object]]
) -> None:
    httpx_mock.add_response(
        url=f"https://api.github.com/repos/{repository}/releases?per_page=100",
        json=releases,
    )


def test_gather_collects_every_valid_manifest_missing_from_committed_main(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Committed identities are skipped; every other valid manifest is returned."""
    committed = _manifest("gelstable/gel", "v1")
    committed["source"] = {
        "repository": "gelstable/gel",
        "release_id": 1,
        "tag": "v1",
        "published_at": "2026-08-15T00:00:00Z",
    }
    path = tmp_path / "releases" / "gelstable" / "gel" / "1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(ReleaseRecord.model_validate(committed)))
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json(
            {
                "repositories": ["gelstable/gel", "gelstable/gel-cli"],
                "schema_version": 1,
            }
        )
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel",
        [
            _release("gelstable/gel", 1, "v1"),
            _release("gelstable/gel", 2, "v2"),
            _release("gelstable/gel", 3, "v3", manifest=False),
        ],
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel-cli",
        [_release("gelstable/gel-cli", 4, "v4", draft=True)],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/2",
        content=json.dumps(_manifest("gelstable/gel", "v2")).encode(),
    )

    with httpx.Client() as client:
        result = gather_missing(tmp_path, client)

    assert [(r.source.repository, r.source.release_id) for r in result.records] == [
        ("gelstable/gel", 2)
    ]
    assert result.records[0].source.tag == "v2"
    assert result.rejected == ()


def test_bad_manifest_rejects_one_release_without_hiding_later_valid_releases(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """A publisher defect is visible but cannot wedge promotion for its peers."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": ["gelstable/gel"], "schema_version": 1})
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel",
        [_release("gelstable/gel", 10, "bad"), _release("gelstable/gel", 11, "good")],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/10", content=b"not json"
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/11",
        content=json.dumps(_manifest("gelstable/gel", "good")).encode(),
    )

    with httpx.Client() as client:
        result = gather_missing(tmp_path, client)

    assert [(r.source.repository, r.source.release_id) for r in result.records] == [
        ("gelstable/gel", 11)
    ]
    assert [(r.repository, r.release_id) for r in result.rejected] == [
        ("gelstable/gel", 10)
    ]


def test_github_transport_failure_aborts_gathering(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """An outage must not be recorded as a durable judgment about a release."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": ["gelstable/gel"], "schema_version": 1})
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
        status_code=503,
    )

    with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
        gather_missing(tmp_path, client)
