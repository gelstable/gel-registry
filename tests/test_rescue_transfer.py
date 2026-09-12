"""Mocked tests for bounded-memory streaming transfer of rescued artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import httpx
import pytest

import gel_registry.rescue.transfer as transfer_module
from gel_registry.github import GitHubAsset, GitHubError, GitHubRelease
from gel_registry.rescue import RescueAsset
from gel_registry.rescue.transfer import (
    RescueSourceIntegrityError,
    RescueTransferError,
    RescueUploadIntegrityError,
    transfer_asset,
    transfer_original_asset,
    verify_uploaded_asset,
)

REPOSITORY = "gelstable/gel-cli"
SOURCE_URL = (
    "https://packages.edgedb.com/archive/x86_64-unknown-linux-gnu/gel-cli.tar.gz"
)
RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/42"
UPLOAD_URL = f"https://uploads.github.com/repos/{REPOSITORY}/releases/42/assets"


def _release() -> GitHubRelease:
    return GitHubRelease.model_validate(
        {
            "id": 42,
            "tag_name": "v1.0.0",
            "draft": True,
            "prerelease": False,
            "url": RELEASE_URL,
            "upload_url": f"{UPLOAD_URL}{{?name,label}}",
            "assets": [],
            "repository": REPOSITORY,
        }
    )


def _github_asset_payload(
    *,
    size: int | None,
    sha256: str | None,
    name: str = "gel-cli.tar.gz",
    state: str | None = "uploaded",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 9,
        "name": name,
        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/9",
    }
    if size is not None:
        payload["size"] = size
    if sha256 is not None:
        payload["digest"] = f"sha256:{sha256}"
    if state is not None:
        payload["state"] = state
    return payload


def _identity_asset(body: bytes, *, expected_sha256: str | None = None) -> RescueAsset:
    sha256 = (
        hashlib.sha256(body).hexdigest() if expected_sha256 is None else expected_sha256
    )
    return RescueAsset(
        source_url=SOURCE_URL,
        destination_name="gel-cli.tar.gz",
        content_type="application/gzip",
        expected_size=len(body),
        expected_sha256=sha256,
    )


def _handler_for(
    body: bytes,
    upload_response: httpx.Response,
    *,
    source_status: int = 200,
    calls: list[str] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.host)
        if request.url.host == "packages.edgedb.com":
            return httpx.Response(source_status, content=body)
        assert request.url.host == "uploads.github.com"
        return upload_response

    return httpx.MockTransport(handler)


def test_transfer_original_asset_streams_in_declared_chunk_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove real chunking, not just correct reassembly."""

    monkeypatch.setattr(transfer_module, "_CHUNK_SIZE", 4)
    observed_chunk_sizes: list[int] = []
    base_hashing_stream = transfer_module._HashingStream

    class _RecordingHashingStream(base_hashing_stream):  # type: ignore[misc, valid-type]
        def __iter__(self) -> Iterator[bytes]:
            for chunk in super().__iter__():
                observed_chunk_sizes.append(len(chunk))
                yield chunk

    monkeypatch.setattr(transfer_module, "_HashingStream", _RecordingHashingStream)

    body = b"abcdefghijklmno"  # 15 bytes over a 4-byte chunk size -> 4,4,4,3
    asset = _identity_asset(body)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "packages.edgedb.com":
            return httpx.Response(200, content=body)
        captured["content"] = request.content
        return httpx.Response(
            201,
            json=_github_asset_payload(
                size=len(body), sha256=hashlib.sha256(body).hexdigest()
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = transfer_original_asset(client, REPOSITORY, _release(), asset)

    assert observed_chunk_sizes == [4, 4, 4, 3]
    assert captured["content"] == body
    assert result.size == len(body)


def test_transfer_original_asset_rejects_short_source_as_ordinary_error() -> None:
    body = b"short"
    asset = _identity_asset(b"expected-longer-body")  # expected_size != len(body)
    calls: list[str] = []
    transport = _handler_for(body, httpx.Response(201), calls=calls)

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(GitHubError),
    ):
        transfer_original_asset(client, REPOSITORY, _release(), asset)


def test_transfer_original_asset_rejects_over_length_source_as_github_error() -> None:
    declared = b"short"
    actual = b"short-but-actually-longer"
    asset = _identity_asset(declared)
    transport = _handler_for(actual, httpx.Response(201))

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(GitHubError, match="exceeds"),
    ):
        transfer_original_asset(client, REPOSITORY, _release(), asset)


def test_transfer_original_asset_rejects_source_digest_mismatch() -> None:
    body = b"correct-length-wrong-digest"
    asset = RescueAsset(
        source_url=SOURCE_URL,
        destination_name="gel-cli.tar.gz",
        content_type="application/gzip",
        expected_size=len(body),
        expected_sha256="a" * 64,
    )
    transport = _handler_for(
        body,
        httpx.Response(
            201,
            json=_github_asset_payload(
                size=len(body), sha256=hashlib.sha256(body).hexdigest()
            ),
        ),
    )

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(RescueSourceIntegrityError),
    ):
        transfer_original_asset(client, REPOSITORY, _release(), asset)


def test_transfer_original_asset_rejects_github_reported_digest_mismatch() -> None:
    body = b"good-body"
    asset = _identity_asset(body)
    transport = _handler_for(
        body,
        httpx.Response(
            201, json=_github_asset_payload(size=len(body), sha256="a" * 64)
        ),
    )

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(RescueUploadIntegrityError),
    ):
        transfer_original_asset(client, REPOSITORY, _release(), asset)


@pytest.mark.parametrize(
    "payload",
    [
        _github_asset_payload(size=None, sha256="a" * 64),
        _github_asset_payload(size=9, sha256=None),
        _github_asset_payload(size=9, sha256="a" * 64, state="starter"),
        _github_asset_payload(size=9, sha256="a" * 64, state=None),
        _github_asset_payload(size=9, sha256="a" * 64, name="different-name.tar.gz"),
    ],
)
def test_transfer_original_asset_rejects_missing_or_mismatched_github_metadata(
    payload: dict[str, object],
) -> None:
    body = b"good-body"
    asset = _identity_asset(body)
    transport = _handler_for(body, httpx.Response(201, json=payload))

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(RescueUploadIntegrityError),
    ):
        transfer_original_asset(client, REPOSITORY, _release(), asset)


def test_transfer_original_asset_rejects_redirected_source() -> None:
    asset = _identity_asset(b"x")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "packages.edgedb.com":
            return httpx.Response(302, headers={"location": "https://evil.example/x"})
        pytest.fail("uploaded despite a redirected source")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RescueTransferError, match="HTTP 302"),
    ):
        transfer_original_asset(client, REPOSITORY, _release(), asset)

    assert calls == ["packages.edgedb.com"]


def test_transfer_original_asset_retries_after_failure_with_no_retained_state() -> None:
    body = b"good-body"
    asset = _identity_asset(body)
    attempt = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "packages.edgedb.com":
            attempt["count"] += 1
            if attempt["count"] == 1:
                return httpx.Response(200, content=b"wrong")
            return httpx.Response(200, content=body)
        return httpx.Response(
            201,
            json=_github_asset_payload(
                size=len(body), sha256=hashlib.sha256(body).hexdigest()
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubError):
            transfer_original_asset(client, REPOSITORY, _release(), asset)
        result = transfer_original_asset(client, REPOSITORY, _release(), asset)

    assert result.size == len(body)
    assert attempt["count"] == 2


def test_transfer_original_asset_retries_after_upload_failure() -> None:
    body = b"good-body"
    asset = _identity_asset(body)
    attempt = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "packages.edgedb.com":
            return httpx.Response(200, content=body)
        attempt["count"] += 1
        if attempt["count"] == 1:
            return httpx.Response(500)
        return httpx.Response(
            201,
            json=_github_asset_payload(
                size=len(body), sha256=hashlib.sha256(body).hexdigest()
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubError):
            transfer_original_asset(client, REPOSITORY, _release(), asset)
        result = transfer_original_asset(client, REPOSITORY, _release(), asset)

    assert result.size == len(body)
    assert attempt["count"] == 2


def test_transfer_asset_delegates_to_transfer_original_asset() -> None:
    body = b"direct-bytes"
    asset = _identity_asset(body)
    transport = _handler_for(
        body,
        httpx.Response(
            201,
            json=_github_asset_payload(
                size=len(body), sha256=hashlib.sha256(body).hexdigest()
            ),
        ),
    )
    with httpx.Client(transport=transport) as client:
        result = transfer_asset(client, REPOSITORY, _release(), asset)
    assert result.size == len(body)


def test_verify_uploaded_asset_success() -> None:
    asset = GitHubAsset.model_validate(
        _github_asset_payload(size=123, sha256="a" * 64, name="gel-cli.tar.gz")
    )
    verify_uploaded_asset("gel-cli.tar.gz", asset, 123, "a" * 64)
