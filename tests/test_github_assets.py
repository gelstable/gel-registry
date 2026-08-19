from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from support import github_release, mock_client

from gel_registry.github import GitHubError, discover_cli_releases, download_assets


def test_download_assets_streams_installrefs_and_sidecars(tmp_path: Path) -> None:
    release = discover_cli_releases(
        mock_client(lambda request: httpx.Response(200, json=[github_release()])),
        known_versions=(),
    )[0]
    payloads = {asset.name: f"bytes:{asset.name}".encode() for asset in release.assets}

    def handler(request: httpx.Request) -> httpx.Response:
        asset_id = int(request.url.path.rsplit("/", 1)[-1])
        name = next(asset.name for asset in release.assets if asset.id == asset_id)
        assert request.headers["Accept"] == "application/octet-stream"
        return httpx.Response(200, content=payloads[name])

    with mock_client(handler) as client:
        paths = download_assets(client, release, tmp_path / "assets")

    assert set(paths) == set(payloads)
    assert {name: path.read_bytes() for name, path in paths.items()} == payloads


def test_download_assets_rejects_http_failure_without_partial_destination(
    tmp_path: Path,
) -> None:
    release = discover_cli_releases(
        mock_client(lambda request: httpx.Response(200, json=[github_release()])),
        known_versions=(),
    )[0]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with mock_client(handler) as client, pytest.raises(GitHubError):
        download_assets(client, release, tmp_path / "assets")

    assert not (tmp_path / "assets").exists()


def test_download_assets_rejects_unsafe_redirects(
    tmp_path: Path,
) -> None:
    location = "https://evil.example/payload"
    release = discover_cli_releases(
        mock_client(lambda request: httpx.Response(200, json=[github_release()])),
        known_versions=(),
    )[0]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": location})

    with mock_client(handler) as client, pytest.raises(GitHubError, match="redirect"):
        download_assets(client, release, tmp_path / "assets")
    assert not (tmp_path / "assets").exists()


def test_download_assets_strips_credentials_on_cross_origin_redirect(
    tmp_path: Path,
) -> None:
    release = discover_cli_releases(
        mock_client(lambda request: httpx.Response(200, json=[github_release()])),
        known_versions=(),
    )[0]
    observations: list[tuple[str, str | None, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observations.append(
            (
                request.url.host or "",
                request.headers.get("Authorization"),
                request.headers.get("Proxy-Authorization"),
                request.headers.get("Cookie"),
            )
        )
        if request.url.host == "api.github.com":
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://release-assets.githubusercontent.com/downloaded-asset"
                    )
                },
            )
        if request.url.path == "/downloaded-asset":
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://release-assets.githubusercontent.com/"
                        "downloaded-asset-final"
                    )
                },
            )
        return httpx.Response(200, content=b"asset")

    with httpx.Client(
        headers={
            "Authorization": "Bearer secret",
            "Proxy-Authorization": "Basic proxy-secret",
            "Cookie": "session=secret",
        },
        transport=httpx.MockTransport(handler),
    ) as client:
        download_assets(client, release, tmp_path / "assets")

    api_observations = [item for item in observations if item[0] == "api.github.com"]
    cdn_observations = [
        item
        for item in observations
        if item[0] == "release-assets.githubusercontent.com"
    ]
    assert api_observations and cdn_observations
    assert all(
        item[1:] == ("Bearer secret", "Basic proxy-secret", "session=secret")
        for item in api_observations
    )
    assert all(item[1:] == (None, None, None) for item in cdn_observations)
