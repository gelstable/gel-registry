"""Streaming download of published release assets into scratch space."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import AbstractContextManager, closing
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..storage import rename_noreplace
from .models import GITHUB_REPOSITORY, GitHubAsset, GitHubError, GitHubRelease
from .transport import ASSET_HEADERS, REQUEST_TIMEOUT

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
            rename_noreplace(stage, destination)
        except OSError as exc:
            raise GitHubError(f"could not install downloaded assets: {exc}") from exc
        return result
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


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
        headers=ASSET_HEADERS,
        timeout=REQUEST_TIMEOUT,
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
        headers=ASSET_HEADERS,
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )
