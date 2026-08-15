"""Read-only discovery and streaming downloads for Gelstable CLI releases."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx
from packaging.version import InvalidVersion, Version

from .constants import CLI_PLATFORMS, PRODUCT_REPOSITORIES

GITHUB_REPOSITORY = PRODUCT_REPOSITORIES["gel-cli"]
GITHUB_API = "https://api.github.com"
_API_RELEASES_URL = f"{GITHUB_API}/repos/{GITHUB_REPOSITORY}/releases"
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "gel-registry-promoter/1",
}
_ASSET_HEADERS = {
    **_API_HEADERS,
    "Accept": "application/octet-stream",
}
_REQUEST_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=60.0,
    write=60.0,
    pool=60.0,
)
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_MAX_PAGES = 1000
_STREAM_CHUNK_SIZE = 1024 * 1024


class GitHubError(RuntimeError):
    """Raised when GitHub data cannot be trusted or downloaded."""


@dataclass(frozen=True, slots=True)
class GitHubAsset:
    """The release-asset metadata needed by discovery and verification."""

    name: str
    url: str
    browser_download_url: str | None = None
    id: int | None = None
    content_type: str | None = None
    size: int | None = None

    @property
    def download_url(self) -> str:
        """Return the immutable browser URL used in release records."""

        return self.browser_download_url or self.url


@dataclass(frozen=True, slots=True)
class GitHubRelease:
    """A published GitHub release from the allowlisted repository."""

    id: int
    tag_name: str
    assets: tuple[GitHubAsset, ...]
    draft: bool = False
    prerelease: bool = False
    repository: str = GITHUB_REPOSITORY
    url: str | None = None
    html_url: str | None = None

    @property
    def release_id(self) -> int:
        """Alias matching the registry's ``ReleaseSource`` field."""

        return self.id

    @property
    def tag(self) -> str:
        """Alias for the GitHub API's ``tag_name`` field."""

        return self.tag_name

    @property
    def version(self) -> str:
        """Return the SemVer component without the leading ``v``."""

        if not self.tag_name.startswith("v"):
            raise GitHubError(f"release tag is not v<semver>: {self.tag_name!r}")
        return self.tag_name[1:]


def canonical_asset_names() -> tuple[str, ...]:
    """Return canonical identity/zstd asset names in registry order."""

    names: list[str] = []
    for platform in CLI_PLATFORMS:
        suffix = ".exe" if platform.endswith("-windows-msvc") else ""
        identity = f"gel-cli-{platform}{suffix}"
        names.extend((identity, f"{identity}.zst"))
    return tuple(names)


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise GitHubError(f"{label} is not a JSON object")
    return cast(Mapping[str, object], value)


def _required_string(data: Mapping[str, object], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitHubError(f"{label} has no valid {key}")
    return value


def _required_integer(data: Mapping[str, object], key: str, label: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GitHubError(f"{label} has no valid positive integer {key}")
    return value


def _optional_integer(data: Mapping[str, object], key: str, label: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GitHubError(f"{label} has an invalid {key}")
    return value


def _repository_from_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    pieces = parsed.path.strip("/").split("/")
    if (
        parsed.hostname == "api.github.com"
        and len(pieces) >= 3
        and pieces[0] == "repos"
    ):
        return "/".join(pieces[1:3])
    if parsed.hostname == "github.com" and len(pieces) >= 2:
        return "/".join(pieces[:2])
    return None


def _release_repositories(data: Mapping[str, object]) -> frozenset[str]:
    repositories: set[str] = set()
    for key in ("repository", "repo"):
        value = data.get(key)
        if isinstance(value, str) and "/" in value:
            repositories.add(value)
        if isinstance(value, dict):
            full_name = value.get("full_name")
            if isinstance(full_name, str) and "/" in full_name:
                repositories.add(full_name)
    for key in ("url", "html_url", "assets_url", "upload_url"):
        value = data.get(key)
        if isinstance(value, str):
            repository = _repository_from_url(value)
            if repository is not None:
                repositories.add(repository)
    return frozenset(repositories)


def _release_repository(data: Mapping[str, object]) -> str | None:
    repositories = _release_repositories(data)
    return sorted(repositories)[0] if repositories else None


def _parse_asset(value: object, release_label: str) -> GitHubAsset:
    data = _as_mapping(value, f"asset in {release_label}")
    name = _required_string(data, "name", f"asset in {release_label}")
    if "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name:
        raise GitHubError(f"asset {name!r} has an unsafe name")
    url = _required_string(data, "url", f"asset {name!r}")
    browser_url = data.get("browser_download_url")
    if browser_url is not None and (
        not isinstance(browser_url, str) or not browser_url
    ):
        raise GitHubError(f"asset {name!r} has an invalid browser URL")
    parsed_size = data.get("size")
    if (
        isinstance(parsed_size, bool)
        or not isinstance(parsed_size, int)
        or parsed_size < 0
    ):
        parsed_size = None
    return GitHubAsset(
        name=name,
        url=url,
        browser_download_url=browser_url if isinstance(browser_url, str) else None,
        id=_optional_integer(data, "id", f"asset {name!r}"),
        content_type=(
            cast(str, data["content_type"])
            if isinstance(data.get("content_type"), str)
            else None
        ),
        size=parsed_size,
    )


def _parse_release(value: object) -> tuple[GitHubRelease, frozenset[str]]:
    data = _as_mapping(value, "release")
    release_id = _required_integer(data, "id", "release")
    tag_name = _required_string(data, "tag_name", f"release {release_id}")
    draft = data.get("draft", False)
    prerelease = data.get("prerelease", False)
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        raise GitHubError(f"release {release_id} has invalid publication flags")
    assets_value = data.get("assets")
    if not isinstance(assets_value, list):
        raise GitHubError(f"release {release_id} has no asset list")
    assets = tuple(
        _parse_asset(asset, f"release {release_id}") for asset in assets_value
    )
    names = [asset.name for asset in assets]
    if len(names) != len(set(names)):
        raise GitHubError(f"release {release_id} has duplicate asset names")
    repositories = _release_repositories(data)
    repository = sorted(repositories)[0] if repositories else None
    return (
        GitHubRelease(
            id=release_id,
            tag_name=tag_name,
            assets=assets,
            draft=draft,
            prerelease=prerelease,
            repository=repository or GITHUB_REPOSITORY,
            url=(cast(str, data["url"]) if isinstance(data.get("url"), str) else None),
            html_url=(
                cast(str, data["html_url"])
                if isinstance(data.get("html_url"), str)
                else None
            ),
        ),
        repositories,
    )


def _version_from_tag(tag: str) -> str | None:
    if not tag.startswith("v"):
        return None
    version = tag[1:]
    if _SEMVER.fullmatch(version) is None:
        return None
    return version


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


def _release_sort_key(release: GitHubRelease) -> tuple[Version, str, int]:
    try:
        parsed = Version(release.version)
    except InvalidVersion as exc:
        raise GitHubError(f"invalid release version {release.version!r}") from exc
    return (parsed, release.version, release.id)


def _fetch_release_page(
    client: httpx.Client, page: int
) -> tuple[list[object], str | None]:
    try:
        response = client.get(
            _API_RELEASES_URL,
            params={"per_page": "100", "page": str(page)},
            headers=_API_HEADERS,
            follow_redirects=False,
            timeout=_REQUEST_TIMEOUT,
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
                    headers=_API_HEADERS,
                    follow_redirects=False,
                    timeout=_REQUEST_TIMEOUT,
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
        version = _version_from_tag(release.tag_name)
        if version is None or version in known:
            continue
        names = {asset.name for asset in release.assets}
        if not expected_names.issubset(names):
            raise GitHubError(
                f"release {release.tag_name!r} is missing canonical CLI assets"
            )
        selected.append(release)
    selected.sort(key=_release_sort_key)
    return tuple(selected)


def _allowed_asset_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.fragment or parsed.query:
        return False
    prefix = f"/repos/{GITHUB_REPOSITORY}/assets/"
    return parsed.path.startswith(prefix) and parsed.path.removeprefix(prefix).isdigit()


def _download_url_for_asset(asset: GitHubAsset) -> str:
    if _allowed_asset_api_url(asset.url):
        return asset.url
    # Hand-built test fixtures sometimes put the browser URL in ``url``.
    parsed = urlsplit(asset.url)
    if (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.startswith(f"/{GITHUB_REPOSITORY}/releases/download/")
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    ):
        return asset.url
    raise GitHubError(f"asset {asset.name!r} is not an allowlisted GitHub asset URL")


def download_assets(
    client: httpx.Client,
    release: GitHubRelease,
    destination: Path,
) -> Mapping[str, Path]:
    """Stream every published release asset into a new temporary destination.

    The destination is caller-owned scratch space. It must not already exist;
    this makes a failed download incapable of leaving a partial asset set.
    """

    destination = Path(destination)
    if destination.exists():
        raise GitHubError(f"asset destination already exists: {destination}")
    names = [asset.name for asset in release.assets]
    if len(names) != len(set(names)):
        raise GitHubError("release contains duplicate asset names")
    parent = destination.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    except OSError as exc:
        raise GitHubError(f"could not create asset staging directory: {exc}") from exc

    try:
        result: dict[str, Path] = {}
        for asset in release.assets:
            url = _download_url_for_asset(asset)
            target = stage / asset.name
            try:
                with client.stream(
                    "GET",
                    url,
                    headers=_ASSET_HEADERS,
                    follow_redirects=True,
                    timeout=_REQUEST_TIMEOUT,
                ) as response:
                    if response.status_code != 200:
                        raise GitHubError(
                            f"asset {asset.name!r} returned HTTP {response.status_code}"
                        )
                    sha256 = hashlib.sha256()
                    blake2b = hashlib.blake2b(digest_size=64)
                    size = 0
                    with target.open("wb") as stream:
                        for chunk in response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
                            stream.write(chunk)
                            size += len(chunk)
                            sha256.update(chunk)
                            blake2b.update(chunk)
                    if asset.size is not None and asset.size != size:
                        raise GitHubError(
                            f"asset {asset.name!r} size changed: expected "
                            f"{asset.size}, observed {size}"
                        )
            except GitHubError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                raise GitHubError(
                    f"could not download asset {asset.name!r}: {exc}"
                ) from exc
            result[asset.name] = destination / asset.name
        try:
            stage.rename(destination)
        except OSError as exc:
            raise GitHubError(f"could not install downloaded assets: {exc}") from exc
        return result
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


__all__ = [
    "GITHUB_API",
    "GITHUB_REPOSITORY",
    "GitHubAsset",
    "GitHubError",
    "GitHubRelease",
    "canonical_asset_names",
    "discover_cli_releases",
    "download_assets",
]
