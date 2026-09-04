"""Repository-bound GitHub REST operations for draft rescue releases."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .models import (
    GitHubAsset,
    GitHubError,
    GitHubRelease,
    api_release_url,
    repository_from_payload,
    upload_asset_url,
    validate_repository,
)
from .transport import API_HEADERS, GITHUB_API, REQUEST_TIMEOUT


def get_release_by_tag(
    client: httpx.Client, repository: str, tag: str
) -> GitHubRelease | None:
    """Fetch one release by tag, returning ``None`` only for an exact 404."""

    repository = validate_repository(repository)
    if not tag:
        raise GitHubError("GitHub release tag must not be empty")
    endpoint = f"{GITHUB_API}/repos/{repository}/releases/tags/{quote(tag, safe='')}"
    response = _request(client, "GET", endpoint)
    if response.status_code == 404:
        return None
    _require_status(response, 200, "GitHub release lookup")
    release = _parse_release(response, repository)
    if release.tag_name != tag:
        raise GitHubError("GitHub release lookup returned an unexpected tag")
    return release


def create_draft_release(
    client: httpx.Client,
    repository: str,
    tag: str,
    target_commitish: str | None = None,
    *,
    body: str = "",
) -> GitHubRelease:
    """Create a draft release with an intentionally blank body."""

    repository = validate_repository(repository)
    if not tag:
        raise GitHubError("GitHub release tag must not be empty")
    if body:
        raise GitHubError("draft release bodies must be blank")
    payload: dict[str, object] = {
        "tag_name": tag,
        "draft": True,
        "prerelease": False,
        "body": "",
    }
    if target_commitish is not None:
        payload["target_commitish"] = target_commitish
    response = _request(
        client,
        "POST",
        f"{GITHUB_API}/repos/{repository}/releases",
        json=payload,
    )
    _require_status(response, 201, "GitHub draft release creation")
    release = _parse_release(response, repository)
    if not release.draft:
        raise GitHubError("GitHub created a release that is not a draft")
    if release.tag_name != tag:
        raise GitHubError("GitHub created a release with an unexpected tag")
    if release.body not in (None, ""):
        raise GitHubError("GitHub created a release with a nonblank body")
    return release


def list_release_assets(
    client: httpx.Client, repository: str, release: GitHubRelease
) -> tuple[GitHubAsset, ...]:
    """List every asset for a validated release within the requested repository."""

    repository = validate_repository(repository)
    _validate_release(release, repository)
    endpoint = f"{api_release_url(repository, release.id)}/assets"
    assets: list[GitHubAsset] = []
    for page in range(1, 10_001):
        response = _request(
            client,
            "GET",
            endpoint,
            params={"per_page": "100", "page": str(page)},
        )
        _require_status(response, 200, "GitHub release asset listing")
        payload = _json(response, "GitHub release asset listing")
        if not isinstance(payload, list):
            raise GitHubError("GitHub release asset listing was not a JSON array")
        assets.extend(_parse_asset(value, repository) for value in payload)
        if len(payload) < 100:
            return tuple(assets)
    raise GitHubError("GitHub release asset pagination exceeded the safety limit")


def upload_release_asset(
    client: httpx.Client,
    repository: str,
    release: GitHubRelease,
    name: str,
    content_type: str,
    content_length: int,
    stream: Iterable[bytes],
) -> GitHubAsset:
    """Stream one new asset to GitHub's separate, fixed upload endpoint."""

    repository = validate_repository(repository)
    _validate_release(release, repository)
    if not release.draft:
        raise GitHubError("GitHub release uploads require a draft release")
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise GitHubError("GitHub asset name must be a safe filename")
    if not content_type:
        raise GitHubError("GitHub asset content type must not be empty")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length < 0
    ):
        raise GitHubError("GitHub asset content length must be a non-negative integer")
    expected = upload_asset_url(repository, release.id)
    response = _request(
        client,
        "POST",
        expected,
        params={"name": name},
        headers={"Content-Type": content_type, "Content-Length": str(content_length)},
        content=_validated_stream(stream, content_length),
    )
    _require_status(response, 201, "GitHub release asset upload")
    return _parse_asset(_json(response, "GitHub release asset upload"), repository)


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    json: object | None = None,
    headers: Mapping[str, str] | None = None,
    content: Iterable[bytes] | None = None,
) -> httpx.Response:
    try:
        request_headers = {**API_HEADERS, **(headers or {})}
        return client.request(
            method,
            url,
            params=params,
            json=json,
            content=content,
            headers=request_headers,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub request failed: {exc}") from exc


def _require_status(response: httpx.Response, expected: int, operation: str) -> None:
    if response.status_code != expected:
        raise GitHubError(f"{operation} returned HTTP {response.status_code}")


def _json(response: httpx.Response, operation: str) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError(f"{operation} was not JSON") from exc


def _parse_release(response: httpx.Response, repository: str) -> GitHubRelease:
    payload = _json(response, "GitHub release response")
    if not isinstance(payload, Mapping):
        raise GitHubError("GitHub release response was not a JSON object")
    explicit_repository = repository_from_payload(payload)
    if explicit_repository is not None and explicit_repository != repository:
        raise GitHubError("GitHub release response switches repository")
    try:
        release = GitHubRelease.model_validate(payload)
    except ValidationError as exc:
        raise GitHubError(f"invalid GitHub release response: {exc}") from exc
    _validate_release(release, repository)
    return release.model_copy(update={"repository": repository})


def _parse_asset(value: object, repository: str) -> GitHubAsset:
    try:
        asset = GitHubAsset.model_validate(value)
    except ValidationError as exc:
        raise GitHubError(f"invalid GitHub asset response: {exc}") from exc
    _validate_asset(asset, repository)
    return asset


def _validate_release(release: GitHubRelease, repository: str) -> None:
    if release.repository is not None and release.repository != repository:
        raise GitHubError("GitHub release is outside the requested repository")
    for asset in release.assets:
        _validate_asset(asset, repository)


def _validate_asset(asset: GitHubAsset, repository: str) -> None:
    if asset.digest is not None and asset.sha256 is None:
        raise GitHubError("GitHub asset response has an unsupported digest")


def _validated_stream(stream: Iterable[bytes], content_length: int) -> Iterable[bytes]:
    """Yield only byte chunks and prove their exact declared length lazily."""

    total = 0
    for chunk in stream:
        if not isinstance(chunk, bytes):
            raise GitHubError("GitHub upload stream must yield bytes")
        total += len(chunk)
        if total > content_length:
            raise GitHubError("GitHub upload stream exceeds its declared length")
        yield chunk
    if total != content_length:
        raise GitHubError(
            "GitHub upload stream does not match its declared content length"
        )
