from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from gel_registry.constants import CLI_PLATFORMS
from gel_registry.github import (
    GITHUB_REPOSITORY,
    GitHubError,
    discover_cli_releases,
    download_assets,
)


def _asset(name: str, release_id: int = 1) -> dict[str, object]:
    return {
        "id": release_id,
        "name": name,
        "url": f"https://api.github.com/repos/{GITHUB_REPOSITORY}/assets/{release_id}",
        "browser_download_url": (
            f"https://github.com/{GITHUB_REPOSITORY}/releases/download/v1.2.3/{name}"
        ),
    }


def _release(
    tag: str = "v1.2.3",
    release_id: int = 100,
    *,
    draft: bool = False,
    prerelease: bool = False,
    owner: str = GITHUB_REPOSITORY,
) -> dict[str, object]:
    assets = [
        _asset(
            f"gel-cli-{platform}{'.exe' if platform.endswith('-windows-msvc') else ''}",
            index,
        )
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


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discovery_filters_and_sorts_releases_across_pages() -> None:
    pages = {
        1: [
            _release("v2.0.0", 200),
            _release("v1.2.3", 123),
            _release("v9.0.0", 900, draft=True),
        ],
        2: [
            _release("v1.10.0", 110),
            _release("v3.0.0-rc.1", 301, prerelease=True),
            _release("v4.0", 400),
            _release("not-a-version", 401),
        ],
        3: [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "2"))
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        headers = (
            {
                "Link": (
                    "<https://api.github.com/repos/gelstable/gel-cli/releases"
                    '?per_page=100&page=2>; rel="next"'
                )
            }
            if page == 1
            else {}
        )
        return httpx.Response(200, headers=headers, json=pages[page])

    with _client(handler) as client:
        releases = discover_cli_releases(client, known_versions={"1.2.3"})

    assert [release.version for release in releases] == ["1.10.0", "2.0.0"]
    assert [release.release_id for release in releases] == [110, 200]


def test_discovery_rejects_ambiguous_release_ids_and_duplicate_assets() -> None:
    payloads = [[_release("v1.0.0", 1), _release("v1.0.0", 2)]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads[0])

    with _client(handler) as client, pytest.raises(GitHubError, match="ambiguous"):
        discover_cli_releases(client, known_versions=())

    duplicate = _release("v2.0.0", 2)
    duplicate_assets = duplicate["assets"]
    assert isinstance(duplicate_assets, list)
    duplicate["assets"] = [*duplicate_assets, duplicate_assets[0]]

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[duplicate])

    with (
        _client(duplicate_handler) as client,
        pytest.raises(GitHubError, match="duplicate"),
    ):
        discover_cli_releases(client, known_versions=())


def test_discovery_rejects_http_failure_and_fork_owned_release() -> None:
    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _client(failed) as client, pytest.raises(GitHubError, match="HTTP 503"):
        discover_cli_releases(client, known_versions=())

    def fork(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_release(owner="someone/gel-cli")])

    with _client(fork) as client:
        assert discover_cli_releases(client, known_versions=()) == ()


def test_download_assets_streams_installrefs_and_sidecars(tmp_path: Path) -> None:
    release = discover_cli_releases(
        _client(lambda request: httpx.Response(200, json=[_release()])),
        known_versions=(),
    )[0]
    payloads = {asset.name: f"bytes:{asset.name}".encode() for asset in release.assets}

    def handler(request: httpx.Request) -> httpx.Response:
        asset_id = int(request.url.path.rsplit("/", 1)[-1])
        name = next(asset.name for asset in release.assets if asset.id == asset_id)
        assert request.headers["Accept"] == "application/octet-stream"
        return httpx.Response(200, content=payloads[name])

    with _client(handler) as client:
        paths = download_assets(client, release, tmp_path / "assets")

    assert set(paths) == set(payloads)
    assert {name: path.read_bytes() for name, path in paths.items()} == payloads


def test_download_assets_rejects_http_failure_without_partial_destination(
    tmp_path: Path,
) -> None:
    release = discover_cli_releases(
        _client(lambda request: httpx.Response(200, json=[_release()])),
        known_versions=(),
    )[0]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(handler) as client, pytest.raises(GitHubError):
        download_assets(client, release, tmp_path / "assets")

    assert not (tmp_path / "assets").exists()
