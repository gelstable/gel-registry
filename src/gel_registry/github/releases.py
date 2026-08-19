"""Discovery of publishable CLI releases from the GitHub API."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ..contracts import semver_key
from .models import (
    GITHUB_REPOSITORY,
    GitHubError,
    GitHubRelease,
    canonical_asset_names,
)
from .transport import API_HEADERS, GITHUB_API, REQUEST_TIMEOUT

_API_RELEASES_URL = f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/releases"
_MAX_PAGES = 1000


def discover_cli_releases(
    client: httpx.Client,
    known_versions: Iterable[object],
) -> tuple[GitHubRelease, ...]:
    """Discover unknown, published, stable CLI releases in SemVer order.

    This function performs only GET requests against the fixed GitHub releases
    endpoint. Ambiguous duplicate tags or release IDs fail closed.
    """

    known = _known_version_strings(known_versions)
    by_tag: dict[str, GitHubRelease] = {}
    by_id: dict[int, GitHubRelease] = {}
    page = 1
    next_url: str | None = None
    for _ in range(_MAX_PAGES):
        if next_url is None:
            payload, next_url = _fetch_release_page(client, page)
        else:
            try:
                response = client.get(
                    next_url,
                    headers=API_HEADERS,
                    follow_redirects=False,
                    timeout=REQUEST_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise GitHubError(f"GitHub release request failed: {exc}") from exc
            if response.status_code != 200:
                raise GitHubError(
                    f"GitHub release request returned HTTP {response.status_code}"
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise GitHubError("GitHub release response was not JSON") from exc
            if not isinstance(data, list):
                raise GitHubError("GitHub release response was not a JSON array")
            payload = cast(list[object], data)
            link = response.links.get("next")
            next_link = link.get("url") if link is not None else None
            if next_link is not None and not isinstance(next_link, str):
                raise GitHubError("GitHub pagination link is invalid")
            next_url = next_link
            if next_url is not None:
                _validate_pagination_url(next_url)
        for raw in payload:
            release, repositories = _parse_release(raw)
            if any(repository != GITHUB_REPOSITORY for repository in repositories):
                continue
            existing_tag = by_tag.get(release.tag_name)
            if existing_tag is not None and existing_tag.id != release.id:
                raise GitHubError(
                    f"ambiguous release tag {release.tag_name!r}: "
                    f"IDs {existing_tag.id} and {release.id}"
                )
            if existing_tag is not None and existing_tag != release:
                raise GitHubError(
                    f"ambiguous release tag {release.tag_name!r}: metadata changed"
                )
            existing_id = by_id.get(release.id)
            if existing_id is not None and existing_id.tag_name != release.tag_name:
                raise GitHubError(
                    f"ambiguous release ID {release.id}: tags "
                    f"{existing_id.tag_name!r} and {release.tag_name!r}"
                )
            if existing_tag is None:
                by_tag[release.tag_name] = release
                by_id[release.id] = release
        if next_url is None:
            break
        page += 1
    else:
        raise GitHubError("GitHub release pagination exceeded the safety limit")

    expected_names = set(canonical_asset_names())
    selected: list[GitHubRelease] = []
    for release in by_tag.values():
        if release.draft or release.prerelease:
            continue
        if release.version in known:
            continue
        names = {asset.name for asset in release.assets}
        if not expected_names.issubset(names):
            raise GitHubError(
                f"release {release.tag_name!r} is missing canonical CLI assets"
            )
        selected.append(release)
    selected.sort(key=_release_sort_key)
    return tuple(selected)


def _parse_release(value: object) -> tuple[GitHubRelease, frozenset[str]]:
    try:
        release = GitHubRelease.model_validate(value)
    except ValidationError as exc:
        raise GitHubError(f"invalid GitHub release: {exc}") from exc
    return release, release.repositories


def _known_version_strings(known_versions: Iterable[object]) -> frozenset[str]:
    versions: set[str] = set()
    for value in known_versions:
        if isinstance(value, str):
            versions.add(value.removeprefix("v"))
            continue
        version = getattr(value, "version", None)
        if isinstance(version, str):
            versions.add(version.removeprefix("v"))
            continue
        raise GitHubError(f"known version is not a SemVer string: {value!r}")
    return frozenset(versions)


def _release_sort_key(
    release: GitHubRelease,
) -> tuple[tuple[int, int, int, tuple[tuple[int, int | str], ...], int], str, int]:
    try:
        parsed = semver_key(release.version)
    except ValueError as exc:
        raise GitHubError(f"invalid release version {release.version!r}") from exc
    return (parsed, release.version, release.id)


def _fetch_release_page(
    client: httpx.Client, page: int
) -> tuple[list[object], str | None]:
    try:
        response = client.get(
            _API_RELEASES_URL,
            params={"per_page": "100", "page": str(page)},
            headers=API_HEADERS,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise GitHubError(f"GitHub release request failed: {exc}") from exc
    if response.status_code != 200:
        raise GitHubError(
            f"GitHub release request returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GitHubError("GitHub release response was not JSON") from exc
    if not isinstance(payload, list):
        raise GitHubError("GitHub release response was not a JSON array")
    next_link = response.links.get("next")
    next_url = next_link.get("url") if next_link is not None else None
    if next_url is not None and not isinstance(next_url, str):
        raise GitHubError("GitHub pagination link is invalid")
    if next_url is not None:
        _validate_pagination_url(next_url)
    return (cast(list[object], payload), next_url)


def _validate_pagination_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise GitHubError("GitHub pagination link is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path != f"/repos/{GITHUB_REPOSITORY}/releases"
    ):
        raise GitHubError("GitHub pagination link is outside the releases endpoint")
