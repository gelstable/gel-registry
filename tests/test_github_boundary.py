"""Mocked tests for the narrow GitHub draft-release write boundary."""

from __future__ import annotations

import json

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


def test_draft_release_operations_use_fixed_transport_and_blank_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "body": "",
            "draft": True,
            "prerelease": False,
            "tag_name": "legacy-v7",
            "target_commitish": "abc123",
        }
        return httpx.Response(201, json=_release())

    with _client(httpx.MockTransport(handler)) as client:
        assert get_release_by_tag(client, REPOSITORY, "legacy-v7") is None
        release = create_draft_release(client, REPOSITORY, "legacy-v7", "abc123")

    assert release.draft
    assert [request.url.path for request in seen] == [
        f"/repos/{REPOSITORY}/releases/tags/legacy-v7",
        f"/repos/{REPOSITORY}/releases",
    ]
    for request in seen:
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"


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


def test_lists_all_assets_and_validates_asset_repository() -> None:
    release_payload = _release()
    release_payload["assets"] = [_asset()]
    response = _asset()

    def handler(request: httpx.Request) -> httpx.Response:
        payload: object = (
            release_payload if request.url.path.endswith("legacy-v7") else [response]
        )
        return httpx.Response(200, json=payload)

    with _client(httpx.MockTransport(handler)) as client:
        release = get_release_by_tag(client, REPOSITORY, "legacy-v7")
        assert release is not None
        assets = list_release_assets(client, REPOSITORY, release)

    assert assets[0].size == 3
    assert assets[0].sha256 == "a" * 64


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


@pytest.mark.parametrize(
    ("stream", "message"),
    [
        (iter((b"a", b"b")), "length"),
        (iter((b"abc", "not-bytes")), "bytes"),
    ],
)
def test_upload_stream_under_length_is_safe_and_raises_plain_error(
    stream: object, message: str
) -> None:
    """A short stream never lets GitHub see a complete body, so the ordinary
    ``GitHubError`` is correct here -- there is nothing to recover."""

    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        request.read()
        return httpx.Response(201, json=_asset())

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(GitHubError, match=message),
    ):
        upload_release_asset(
            client,
            REPOSITORY,
            GitHubRelease.model_validate(_release()),
            "server.tar.zst",
            "application/zstd",
            3,
            stream,  # type: ignore[arg-type]
        )
    assert seen == 0


def test_upload_stream_over_length_raises_github_error() -> None:
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        request.read()
        return httpx.Response(201, json=_asset())

    with (
        _client(httpx.MockTransport(handler)) as client,
        pytest.raises(GitHubError, match="exceeds") as excinfo,
    ):
        upload_release_asset(
            client,
            REPOSITORY,
            GitHubRelease.model_validate(_release()),
            "server.tar.zst",
            "application/zstd",
            3,
            iter((b"abcd",)),
        )
    assert isinstance(excinfo.value, GitHubError)
    assert seen == 0


def test_request_settings_and_upload_headers_are_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []

    with _client(
        httpx.MockTransport(lambda _request: httpx.Response(201, json=_asset()))
    ) as client:
        original = client.request

        def request_spy(*args: object, **kwargs: object) -> httpx.Response:
            observed.append(kwargs)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(client, "request", request_spy)
        upload_release_asset(
            client,
            REPOSITORY,
            GitHubRelease.model_validate(_release()),
            "server.tar.zst",
            "application/zstd",
            3,
            iter((b"abc",)),
        )

    assert len(observed) == 1
    request = observed[0]
    assert request["follow_redirects"] is False
    assert request["timeout"] is not None
    headers = request["headers"]
    assert isinstance(headers, dict)
    assert headers == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gel-registry/1",
        "Content-Type": "application/zstd",
        "Content-Length": "3",
    }


def test_every_operation_uses_fixed_headers_timeout_and_no_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/assets"):
            return httpx.Response(200, json=[])
        if request.method == "GET":
            return httpx.Response(200, json=_release())
        if request.url.host == "uploads.github.com":
            return httpx.Response(201, json=_asset())
        return httpx.Response(201, json=_release())

    with _client(httpx.MockTransport(handler)) as client:
        original = client.request

        def request_spy(*args: object, **kwargs: object) -> httpx.Response:
            observed.append(kwargs)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(client, "request", request_spy)
        release = get_release_by_tag(client, REPOSITORY, "legacy-v7")
        assert release is not None
        create_draft_release(client, REPOSITORY, "legacy-v7")
        list_release_assets(client, REPOSITORY, release)
        upload_release_asset(
            client,
            REPOSITORY,
            release,
            "server.tar.zst",
            "application/zstd",
            3,
            iter((b"abc",)),
        )

    assert len(observed) == 4
    for request in observed:
        assert request["follow_redirects"] is False
        assert request["timeout"] is not None
        headers = request["headers"]
        assert isinstance(headers, dict)
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert headers["User-Agent"] == "gel-registry/1"


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
