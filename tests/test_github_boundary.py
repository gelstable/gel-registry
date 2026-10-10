"""Mocked tests for the narrow GitHub draft-release write boundary."""

from __future__ import annotations

import httpx
import pytest

from gel_registry.github import (
    GitHubError,
    GitHubRelease,
    create_draft_release,
    get_release_by_tag,
    list_release_assets,
    upload_release_asset,
)

REPOSITORY = "gelstable/gel"
RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/42"


def _release(*, upload_url: str | None = None) -> dict[str, object]:
    return {
        "id": 42,
        "tag_name": "legacy-v7",
        "draft": True,
        "prerelease": False,
        "url": RELEASE_URL,
        "upload_url": upload_url
        or f"https://uploads.github.com/repos/{REPOSITORY}/releases/42/assets{{?name,label}}",
        "assets": [],
    }


def _asset() -> dict[str, object]:
    return {
        "id": 9,
        "name": "server.tar.zst",
        "size": 3,
        "digest": "sha256:" + "a" * 64,
        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/9",
    }


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_get_release_by_tag_finds_a_draft_in_the_paginated_release_listing() -> None:
    requests: list[httpx.Request] = []
    other_release = _release()
    other_release["tag_name"] = "other-tag"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/tags/" in request.url.path:
            return httpx.Response(404)
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(200, json=[other_release] * 100)
        return httpx.Response(200, json=[_release()])

    with _client(httpx.MockTransport(handler)) as client:
        release = get_release_by_tag(client, REPOSITORY, "legacy-v7")

    assert release is not None
    assert release.draft
    assert release.tag_name == "legacy-v7"
    observed_pages = [
        (request.url.path, request.url.params.get("page")) for request in requests
    ]
    assert observed_pages == [
        (f"/repos/{REPOSITORY}/releases/tags/legacy-v7", None),
        (f"/repos/{REPOSITORY}/releases", "1"),
        (f"/repos/{REPOSITORY}/releases", "2"),
    ]
    assert all(request.url.params.get("per_page") == "100" for request in requests[1:])


def test_get_release_by_tag_rejects_duplicate_drafts_in_release_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/tags/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json=[_release(), _release()])

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(GitHubError, match="multiple releases"),
    ):
        get_release_by_tag(client, REPOSITORY, "legacy-v7")


def test_create_rejects_nonblank_body_and_non_draft_response() -> None:
    with (
        _client(httpx.MockTransport(lambda _request: httpx.Response(201))) as client,
        pytest.raises(GitHubError, match="blank"),
    ):
        create_draft_release(client, REPOSITORY, "legacy-v7", body="not blank")

    response = _release()
    response["draft"] = False
    with (
        _client(
            httpx.MockTransport(lambda _request: httpx.Response(201, json=response))
        ) as client,
        pytest.raises(GitHubError, match="draft"),
    ):
        create_draft_release(client, REPOSITORY, "legacy-v7")


@pytest.mark.parametrize(
    "repo_payload",
    [
        "other/repo",
        {"full_name": "other/repo"},
    ],
)
def test_response_release_cannot_switch_repository(repo_payload: object) -> None:
    response = _release()
    response["repository"] = repo_payload
    with (
        _client(
            httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
        ) as client,
        pytest.raises(GitHubError, match="repository"),
    ):
        get_release_by_tag(client, REPOSITORY, "legacy-v7")


def test_upload_uses_only_the_upload_host_and_release_path() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content"] = request.content
        captured["headers"] = request.headers
        return httpx.Response(201, json=_asset())

    with _client(httpx.MockTransport(handler)) as client:
        release = GitHubRelease.model_validate(
            _release(), context={"repository": REPOSITORY}
        )
        asset = upload_release_asset(
            client,
            REPOSITORY,
            release,
            "server.tar.zst",
            "application/zstd",
            3,
            iter((b"a", b"bc")),
        )

    assert captured["url"] == (
        f"https://uploads.github.com/repos/{REPOSITORY}/releases/42/assets?"
        "name=server.tar.zst"
    )
    assert captured["content"] == b"abc"
    assert asset.digest == "sha256:" + "a" * 64


def test_upload_requires_a_draft_release_before_any_request() -> None:
    release = GitHubRelease.model_validate(_release() | {"draft": False})
    with (
        _client(
            httpx.MockTransport(lambda _request: pytest.fail("made HTTP request"))
        ) as client,
        pytest.raises(GitHubError, match="draft"),
    ):
        upload_release_asset(
            client,
            REPOSITORY,
            release,
            "server.tar.zst",
            "application/zstd",
            3,
            iter((b"abc",)),
        )


@pytest.mark.parametrize(
    "repository",
    ["owner/repo?x=1", "owner/repo#x", "owner%2Frepo/name", "owner:pw/repo"],
)
def test_malformed_repository_never_makes_a_request(repository: str) -> None:
    with (
        _client(
            httpx.MockTransport(lambda _request: pytest.fail("made HTTP request"))
        ) as client,
        pytest.raises(GitHubError, match="owner/repository"),
    ):
        get_release_by_tag(client, repository, "legacy-v7")


def test_asset_listing_fetches_every_page() -> None:
    requests: list[str] = []
    first = [{**_asset(), "id": asset_id} for asset_id in range(1, 101)]
    for asset in first:
        asset["url"] = (
            f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset['id']}"
        )
    final = {**_asset(), "id": 101}
    final["url"] = f"https://api.github.com/repos/{REPOSITORY}/releases/assets/101"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        page = request.url.params.get("page")
        return httpx.Response(200, json=first if page == "1" else [final])

    release = GitHubRelease.model_validate(_release())
    with _client(httpx.MockTransport(handler)) as client:
        assets = list_release_assets(client, REPOSITORY, release)

    assert len(assets) == 101
    assert [url.rsplit("page=", 1)[-1] for url in requests] == ["1", "2"]
