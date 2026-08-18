"""Read-only discovery and streaming downloads for Gelstable CLI releases."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager, closing
from pathlib import Path
from typing import cast
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .constants import CLI_PLATFORMS, PRODUCT_REPOSITORIES
from .schema import parse_semver, semver_key

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
_MAX_PAGES = 1000
_STREAM_CHUNK_SIZE = 1024 * 1024
_MAX_REDIRECTS = 10
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ASSET_DELIVERY_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
        "github-cloud.s3.amazonaws.com",
    }
)
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_AT_FDCWD = -100
_TAG_PREFIX = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")


def _version_from_tag(tag: str) -> str | None:
    if tag.startswith("v"):
        version = tag[1:]
        if parse_semver(version) is not None:
            return version
    prefix, separator, version = tag.rpartition("-v")
    if not separator or not _TAG_PREFIX.fullmatch(prefix):
        return None
    return version if parse_semver(version) is not None else None


class GitHubError(RuntimeError):
    """Raised when GitHub data cannot be trusted or downloaded."""


_GITHUB_MODEL_CONFIG = ConfigDict(extra="ignore", frozen=True)


class GitHubAsset(BaseModel):
    """The release-asset metadata needed by discovery and verification."""

    model_config = _GITHUB_MODEL_CONFIG

    name: StrictStr
    url: StrictStr
    browser_download_url: StrictStr | None = None
    id: StrictInt | None = Field(default=None, gt=0)
    content_type: StrictStr | None = None
    size: StrictInt | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value:
            raise ValueError("asset has no valid name")
        if "/" in value or "\\" in value or value in {".", ".."} or "\x00" in value:
            raise ValueError(f"asset {value!r} has an unsafe name")
        return value

    @field_validator("url", "browser_download_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("asset URL must not be empty")
        return value

    @field_validator("size", mode="before")
    @classmethod
    def normalize_size(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @property
    def download_url(self) -> str:
        """Return the immutable browser URL used in release records."""

        return self.browser_download_url or self.url


class GitHubRelease(BaseModel):
    """A published GitHub release from the allowlisted repository."""

    model_config = _GITHUB_MODEL_CONFIG

    id: StrictInt = Field(gt=0)
    tag_name: StrictStr
    assets: tuple[GitHubAsset, ...]
    draft: StrictBool = False
    prerelease: StrictBool = False
    repository: StrictStr = GITHUB_REPOSITORY
    url: StrictStr | None = None
    html_url: StrictStr | None = None
    version: StrictStr = Field(default="", exclude=True, repr=False)
    repositories: frozenset[str] = Field(
        default_factory=frozenset, exclude=True, repr=False
    )

    @model_validator(mode="before")
    @classmethod
    def parse_payload(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        tag_name = data.get("tag_name")
        if isinstance(tag_name, str):
            version = _version_from_tag(tag_name)
            if version is None:
                raise ValueError(f"invalid release tag: {tag_name!r}")
            data["version"] = version
        repositories = _release_repositories(data)
        repository = data.get("repository")
        if not repositories and isinstance(repository, str) and "/" in repository:
            repositories = frozenset({repository})
        data["repositories"] = repositories
        data["repository"] = (
            sorted(repositories)[0] if repositories else GITHUB_REPOSITORY
        )
        return data

    @field_validator("tag_name")
    @classmethod
    def validate_tag_name(cls, value: str) -> str:
        if not value:
            raise ValueError("release tag must not be empty")
        return value

    @model_validator(mode="after")
    def validate_release(self) -> GitHubRelease:
        names = [asset.name for asset in self.assets]
        if len(names) != len(set(names)):
            raise ValueError(f"release {self.id} has duplicate asset names")
        return self

    @property
    def release_id(self) -> int:
        """Alias matching the registry's ``ReleaseSource`` field."""

        return self.id

    @property
    def tag(self) -> str:
        """Alias for the GitHub API's ``tag_name`` field."""

        return self.tag_name

def canonical_asset_names() -> tuple[str, ...]:
    """Return canonical identity/zstd asset names in registry order."""

    names: list[str] = []
    for platform in CLI_PLATFORMS:
        suffix = ".exe" if platform.endswith("-windows-msvc") else ""
        identity = f"gel-cli-{platform}{suffix}"
        names.extend((identity, f"{identity}.zst"))
    return tuple(names)


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


def _validate_delivery_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise GitHubError(f"asset redirect URL is malformed: {url!r}") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ASSET_DELIVERY_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise GitHubError(f"asset redirect leaves GitHub delivery origins: {url!r}")


def _request_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port == 443:
        port = None
    return (parsed.scheme, parsed.hostname or "", port)


def _safe_cross_origin_stream(
    client: httpx.Client,
    url: str,
) -> httpx.Response:
    """Prepare a request without credentials for a different origin."""

    request = client.build_request(
        "GET",
        url,
        headers=_ASSET_HEADERS,
        timeout=_REQUEST_TIMEOUT,
    )
    for header in ("Authorization", "Proxy-Authorization", "Cookie"):
        if header in request.headers:
            del request.headers[header]
    return client.send(
        request,
        stream=True,
        follow_redirects=False,
    )


def _asset_stream(
    client: httpx.Client,
    url: str,
    strip_credentials: bool,
) -> AbstractContextManager[httpx.Response]:
    if strip_credentials:
        return closing(_safe_cross_origin_stream(client, url))
    return client.stream(
        "GET",
        url,
        headers=_ASSET_HEADERS,
        follow_redirects=False,
        timeout=_REQUEST_TIMEOUT,
    )


def _rename_noreplace(source: Path, destination: Path) -> None:
    source_bytes = os.fsencode(str(source))
    destination_bytes = os.fsencode(str(destination))
    if b"\x00" in source_bytes or b"\x00" in destination_bytes:
        raise OSError(errno.EINVAL, "path contains an embedded NUL")
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, destination_bytes, _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


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
    if destination.exists() or destination.is_symlink():
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
            _validate_delivery_url(url)
            target = stage / asset.name
            try:
                original_origin = _request_origin(url)
                strip_credentials = False
                for redirect_count in range(_MAX_REDIRECTS + 1):
                    response_context = _asset_stream(client, url, strip_credentials)
                    with response_context as response:
                        response_url = str(response.url)
                        _validate_delivery_url(response_url)
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                raise GitHubError(
                                    f"asset {asset.name!r} redirect has no Location"
                                )
                            if redirect_count == _MAX_REDIRECTS:
                                raise GitHubError(
                                    f"asset {asset.name!r} redirect limit exceeded"
                                )
                            url = urljoin(response_url, location)
                            _validate_delivery_url(url)
                            strip_credentials = (
                                strip_credentials
                                or _request_origin(url) != original_origin
                            )
                            continue
                        if response.status_code != 200:
                            raise GitHubError(
                                f"asset {asset.name!r} returned HTTP "
                                f"{response.status_code}"
                            )
                        sha256 = hashlib.sha256()
                        blake2b = hashlib.blake2b(digest_size=64)
                        size = 0
                        with target.open("wb") as stream:
                            for chunk in response.iter_bytes(
                                chunk_size=_STREAM_CHUNK_SIZE
                            ):
                                stream.write(chunk)
                                size += len(chunk)
                                sha256.update(chunk)
                                blake2b.update(chunk)
                        if asset.size is not None and asset.size != size:
                            raise GitHubError(
                                f"asset {asset.name!r} size changed: expected "
                                f"{asset.size}, observed {size}"
                            )
                        break
                else:
                    raise GitHubError(f"asset {asset.name!r} redirect failed")
            except GitHubError:
                raise
            except (httpx.HTTPError, OSError) as exc:
                raise GitHubError(
                    f"could not download asset {asset.name!r}: {exc}"
                ) from exc
            result[asset.name] = destination / asset.name
        try:
            _rename_noreplace(stage, destination)
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
