"""Bounded, atomic capture of the reviewed legacy package-index matrix."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import ValidationError

from .constants import CAPTURE_ID, ORIGIN, capture_urls
from .digest import Digests, canonical_json, hash_file
from .schema import CaptureEntry, CaptureManifest, PackageIndex

_USER_AGENT = "gel-registry-importer/1"
_STREAM_CHUNK_SIZE = 1024 * 1024
_MAX_REDIRECTS = 20
_REQUEST_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=60.0,
    write=60.0,
    pool=60.0,
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class CaptureError(RuntimeError):
    """Raised when a complete legacy capture cannot be trusted."""


@dataclass(frozen=True, slots=True)
class _Observed:
    status: int
    final_url: str
    headers: httpx.Headers
    digests: Digests | None = None
    body: bytes | None = None


def _origin_url(value: str | httpx.URL) -> httpx.URL:
    """Validate and return an HTTPS URL on the fixed package origin."""

    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise CaptureError(f"URL is malformed: {value!r}") from exc
    parsed = urlsplit(str(url))
    origin = urlsplit(ORIGIN)
    if parsed.scheme != "https" or parsed.hostname != origin.hostname:
        raise CaptureError(f"URL is outside the capture origin: {url}")
    if parsed.username is not None or parsed.password is not None:
        raise CaptureError(f"URL contains userinfo: {url}")
    if parsed.fragment:
        raise CaptureError(f"URL contains a fragment: {url}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise CaptureError(f"URL has an invalid port: {url}") from exc
    if port not in (None, 443):
        raise CaptureError(f"URL uses a non-default port: {url}")
    return url


def _entry_label(channel: str, platform: str) -> str:
    return f"{channel}/{platform}"


def _entry_error(
    channel: str,
    platform: str,
    message: str,
) -> CaptureError:
    return CaptureError(f"{_entry_label(channel, platform)}: {message}")


def _request_headers(conditional: tuple[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if conditional is not None:
        headers[conditional[0]] = conditional[1]
    return headers


def _stream_to_file(response: httpx.Response, path: Path) -> Digests:
    sha256 = hashlib.sha256()
    blake2b = hashlib.blake2b(digest_size=64)
    size = 0
    with path.open("wb") as stream:
        for chunk in response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
            stream.write(chunk)
            size += len(chunk)
            sha256.update(chunk)
            blake2b.update(chunk)
    return Digests(
        size=size,
        sha256=sha256.hexdigest(),
        blake2b=blake2b.hexdigest(),
    )


def _read_body(response: httpx.Response) -> bytes:
    chunks = bytearray()
    for chunk in response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
        chunks.extend(chunk)
    return bytes(chunks)


def _fetch(
    client: httpx.Client,
    url: str,
    *,
    conditional: tuple[str, str] | None = None,
    output_path: Path | None = None,
) -> _Observed:
    """Fetch one URL, manually following only same-origin HTTPS redirects."""

    current = _origin_url(url)
    for redirect_count in range(_MAX_REDIRECTS + 1):
        try:
            with client.stream(
                "GET",
                current,
                headers=_request_headers(conditional),
                follow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                response_url = _origin_url(response.url)
                current = response_url
                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise CaptureError(
                            f"redirect from {current} has no Location header"
                        )
                    if redirect_count == _MAX_REDIRECTS:
                        raise CaptureError(
                            f"redirect limit exceeded while fetching {url}"
                        )
                    current = _origin_url(urljoin(str(response_url), location))
                    continue

                final_url = str(current)
                headers = httpx.Headers(response.headers)
                if status == 200:
                    if output_path is None:
                        body = _read_body(response)
                        return _Observed(
                            status=status,
                            final_url=final_url,
                            headers=headers,
                            body=body,
                        )
                    digests = _stream_to_file(response, output_path)
                    return _Observed(
                        status=status,
                        final_url=final_url,
                        headers=headers,
                        digests=digests,
                    )
                return _Observed(status=status, final_url=final_url, headers=headers)
        except CaptureError:
            raise
        except httpx.HTTPError as exc:
            raise CaptureError(f"request failed for {current}: {exc}") from exc
        except OSError as exc:
            raise CaptureError(f"could not save response for {current}: {exc}") from exc

    raise CaptureError(f"redirect processing failed while fetching {url}")


def _same_file(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk = left_stream.read(_STREAM_CHUNK_SIZE)
            right_chunk = right_stream.read(_STREAM_CHUNK_SIZE)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _conditional_headers(entry: CaptureEntry) -> tuple[str, str] | None:
    if entry.etag is not None:
        return ("If-None-Match", entry.etag)
    if entry.last_modified is not None:
        return ("If-Modified-Since", entry.last_modified)
    return None


def _capture_entry(
    channel: str,
    platform: str,
    url: str,
    observed: _Observed,
    relative_path: str,
) -> CaptureEntry:
    if observed.status == 404:
        return CaptureEntry(
            channel=channel,
            platform=platform,
            url=url,
            status=404,
            etag=observed.headers.get("etag"),
            last_modified=observed.headers.get("last-modified"),
        )
    if observed.status != 200:
        raise _entry_error(
            channel,
            platform,
            f"expected status 200 or 404, observed {observed.status}",
        )
    if observed.digests is None:
        raise _entry_error(channel, platform, "successful response has no body")
    return CaptureEntry(
        channel=channel,
        platform=platform,
        url=url,
        status=200,
        final_url=observed.final_url,
        etag=observed.headers.get("etag"),
        last_modified=observed.headers.get("last-modified"),
        size=observed.digests.size,
        sha256=observed.digests.sha256,
        blake2b=observed.digests.blake2b,
        path=relative_path,
    )


def _recheck_entry(
    client: httpx.Client,
    root: Path,
    entry: CaptureEntry,
    index: int,
) -> None:
    temporary_path = root / f".recheck-{index}.json"
    try:
        observed = _fetch(
            client,
            entry.url,
            conditional=_conditional_headers(entry),
            output_path=temporary_path,
        )
    except CaptureError as exc:
        raise _entry_error(entry.channel, entry.platform, str(exc)) from exc

    try:
        if observed.status == 304 and entry.status == 200:
            return
        if observed.status != entry.status:
            raise _entry_error(
                entry.channel,
                entry.platform,
                f"expected status {entry.status}, observed {observed.status}",
            )
        if entry.status == 404:
            return
        if observed.status != 200 or not temporary_path.exists():
            raise _entry_error(
                entry.channel,
                entry.platform,
                f"expected status 200, observed {observed.status}",
            )
        expected_path = root / (entry.path or "")
        if not expected_path.is_file() or not _same_file(expected_path, temporary_path):
            raise _entry_error(
                entry.channel,
                entry.platform,
                "recheck body differs from captured bytes",
            )
    finally:
        temporary_path.unlink(missing_ok=True)


def capture_legacy(
    client: httpx.Client,
    destination: Path,
    captured_at: datetime,
) -> CaptureManifest:
    """Capture the fixed 24-entry legacy matrix into a new immutable root."""

    if destination.exists() or destination.is_symlink():
        raise CaptureError(f"capture destination already exists: {destination}")

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=str(parent))
    )
    committed = False
    try:
        indexes_root = temporary_root / "indexes"
        indexes_root.mkdir()
        entries: list[CaptureEntry] = []
        for channel, platform, url in capture_urls():
            relative_path = f"indexes/{channel}-{platform}.json"
            body_path = temporary_root / relative_path
            observed = _fetch(client, url, output_path=body_path)
            if observed.status == 200:
                try:
                    PackageIndex.model_validate_json(body_path.read_bytes())
                except (ValidationError, ValueError, OSError) as exc:
                    raise _entry_error(
                        channel,
                        platform,
                        f"successful body is not a package index: {exc}",
                    ) from exc
            entries.append(
                _capture_entry(channel, platform, url, observed, relative_path)
            )

        for index, entry in enumerate(entries):
            _recheck_entry(client, temporary_root, entry, index)

        manifest = CaptureManifest(
            capture=CAPTURE_ID,
            origin=ORIGIN,
            captured_at=captured_at,
            entries=tuple(entries),
        )
        (temporary_root / "capture.json").write_bytes(canonical_json(manifest))

        if destination.exists() or destination.is_symlink():
            raise CaptureError(f"capture destination already exists: {destination}")
        os.rename(temporary_root, destination)
        committed = True
        return manifest
    except CaptureError:
        raise
    except (OSError, ValidationError, ValueError) as exc:
        raise CaptureError(f"capture failed: {exc}") from exc
    finally:
        if not committed:
            shutil.rmtree(temporary_root, ignore_errors=True)


def verify_live_capture(
    client: httpx.Client,
    root: Path,
    manifest: CaptureManifest,
) -> None:
    """Re-fetch every capture entry and compare it with the candidate bytes."""

    for entry in manifest.entries:
        expected_body: bytes | None = None
        expected_path: Path | None = None
        if entry.status == 200:
            if entry.path is None:
                raise _entry_error(
                    entry.channel, entry.platform, "capture has no body path"
                )
            expected_path = root / entry.path
            if not expected_path.is_file():
                raise _entry_error(
                    entry.channel,
                    entry.platform,
                    f"expected status 200 body is missing at {entry.path}",
                )
            expected_digests = hash_file(expected_path)
            if (
                expected_digests.size != entry.size
                or expected_digests.sha256 != entry.sha256
                or expected_digests.blake2b != entry.blake2b
            ):
                raise _entry_error(
                    entry.channel,
                    entry.platform,
                    "candidate body does not match manifest digests",
                )
            expected_body = expected_path.read_bytes()

        try:
            observed = _fetch(client, entry.url)
        except CaptureError as exc:
            raise _entry_error(entry.channel, entry.platform, str(exc)) from exc
        if observed.status != entry.status:
            raise _entry_error(
                entry.channel,
                entry.platform,
                f"expected status {entry.status}, observed {observed.status}",
            )
        if entry.status == 200 and observed.body != expected_body:
            raise _entry_error(
                entry.channel,
                entry.platform,
                "live body differs from captured bytes",
            )


__all__ = ["CaptureError", "capture_legacy", "verify_live_capture"]
