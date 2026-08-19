from __future__ import annotations

import httpx
import pytest
from support import github_release, mock_client

from gel_registry.github import (
    GitHubError,
    GitHubRelease,
    discover_cli_releases,
)


def test_discovery_filters_and_sorts_releases_across_pages() -> None:
    pages = {
        1: [
            github_release("v2.0.0", 200),
            github_release("v1.2.3", 123),
            github_release("v9.0.0", 900, draft=True),
        ],
        2: [
            github_release("v1.10.0", 110),
            github_release("gel-server-v3.0.0-rc.1", 301, prerelease=True),
            github_release("gel-lsp-v4.0.0", 400),
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

    with mock_client(handler) as client:
        releases = discover_cli_releases(client, known_versions={"1.2.3"})

    assert [release.version for release in releases] == [
        "1.10.0",
        "2.0.0",
        "4.0.0",
    ]
    assert [release.release_id for release in releases] == [110, 200, 400]


@pytest.mark.parametrize(
    ("tag", "version"),
    (
        ("v8.0.0", "8.0.0"),
        ("gel-server-v8.0.0-rc1", "8.0.0-rc1"),
        ("gel-lsp-v8.0.0", "8.0.0"),
        ("vscode-v8.0.0", "8.0.0"),
    ),
)
def test_release_model_parses_reasonable_product_tags(tag: str, version: str) -> None:
    release = GitHubRelease.model_validate(github_release(tag))

    assert release.version == version


def test_discovery_rejects_unreasonable_release_tags() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[github_release("not-a-version")])

    with (
        mock_client(handler) as client,
        pytest.raises(GitHubError, match="invalid release tag"),
    ):
        discover_cli_releases(client, known_versions=())


def test_discovery_rejects_ambiguous_release_ids_and_duplicate_assets() -> None:
    payloads = [[github_release("v1.0.0", 1), github_release("v1.0.0", 2)]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads[0])

    with mock_client(handler) as client, pytest.raises(GitHubError, match="ambiguous"):
        discover_cli_releases(client, known_versions=())

    duplicate = github_release("v2.0.0", 2)
    duplicate_assets = duplicate["assets"]
    assert isinstance(duplicate_assets, list)
    duplicate["assets"] = [*duplicate_assets, duplicate_assets[0]]

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[duplicate])

    with (
        mock_client(duplicate_handler) as client,
        pytest.raises(GitHubError, match="duplicate"),
    ):
        discover_cli_releases(client, known_versions=())
