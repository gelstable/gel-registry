"""Bounded-memory streaming transfer of rescued artifacts into draft releases.

Every original asset is streamed directly from its source response into the
GitHub upload request: no whole-artifact buffering in memory or on disk.
Neither function retains the fetched source bytes -- a retry simply calls
the function again, which performs a fresh source request.

Handoff obligation for callers
------------------------------
Digest verification here is inherently post-hoc: the SHA-256 of what was
sent to GitHub can only be known once every byte has already been streamed
into the upload request, and this module never deletes or replaces an
asset it (or a prior attempt) has created. That means a retry is not
always as simple as "call the function again" -- failed uploads can leave
a real, already-created asset sitting on the destination draft release.
Callers must re-list the release's assets and reconcile before retrying.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator

import httpx

from ..github import GitHubAsset, GitHubRelease, upload_release_asset
from .models import RescueAsset

_CHUNK_SIZE = 1024 * 1024
_SOURCE_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)


class RescueTransferError(RuntimeError):
    """Raised when a rescued asset cannot be safely transferred."""


class RescueSourceIntegrityError(RescueTransferError):
    """Raised when bytes fetched from the source do not match trusted metadata.

    The source was never a complete, corrupt-but-accepted upload: this is
    raised before, or independently of, whatever was already sent to
    GitHub.
    """


class RescueUploadIntegrityError(RescueTransferError):
    """Raised when GitHub's reported asset metadata does not match what was sent.

    Because uploads are never replaced or deleted by this module, the
    mismatched asset remains on the draft release; the caller must decide
    how to remediate it (for example, by inspecting and manually removing
    it before a retry).
    """


class _HashingStream:
    """Wrap a byte-chunk iterable, hashing and counting it as it is consumed.

    ``size`` and ``sha256`` are only valid once iteration has completed --
    accessing them earlier is a programming error, since bounded-memory
    streaming never buffers enough to answer sooner.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = chunks
        self._hasher = hashlib.sha256()
        self._size = 0
        self._done = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self._hasher.update(chunk)
            self._size += len(chunk)
            yield chunk
        self._done = True

    @property
    def size(self) -> int:
        if not self._done:
            raise RescueTransferError("stream has not been fully consumed")
        return self._size

    @property
    def sha256(self) -> str:
        if not self._done:
            raise RescueTransferError("stream has not been fully consumed")
        return self._hasher.hexdigest()


def _open_source(client: httpx.Client, source_url: str) -> httpx.Response:
    """Open one source response without following redirects."""

    try:
        request = client.build_request("GET", source_url, timeout=_SOURCE_TIMEOUT)
        response = client.send(request, stream=True, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise RescueTransferError(f"source request failed: {exc}") from exc
    try:
        if response.status_code != 200:
            raise RescueTransferError(
                f"source returned HTTP {response.status_code}: {source_url}"
            )
    except Exception:
        response.close()
        raise
    return response


def _verify_source(
    destination_name: str,
    expected_size: int,
    expected_sha256: str,
    streamed_size: int,
    streamed_sha256: str,
) -> None:
    """Compare fetched source bytes with the trusted plan metadata."""

    if streamed_size != expected_size:
        raise RescueSourceIntegrityError(
            f"{destination_name}: source returned {streamed_size} bytes, "
            f"expected {expected_size}"
        )
    if streamed_sha256 != expected_sha256:
        raise RescueSourceIntegrityError(
            f"{destination_name}: source content digest does not match the "
            "expected sha256"
        )


def verify_uploaded_asset(
    destination_name: str,
    github_asset: GitHubAsset,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Validate that GitHub's response confirms the exact uploaded asset."""

    if github_asset.name != destination_name:
        raise RescueUploadIntegrityError(
            f"{destination_name}: GitHub reported asset name {github_asset.name!r}, "
            f"expected {destination_name!r}"
        )
    if github_asset.state != "uploaded":
        raise RescueUploadIntegrityError(
            f"{destination_name}: GitHub reported upload state {github_asset.state!r}, "
            f"expected 'uploaded'"
        )
    if github_asset.size != expected_size:
        raise RescueUploadIntegrityError(
            f"{destination_name}: GitHub reported size {github_asset.size}, "
            f"expected {expected_size}"
        )
    if github_asset.sha256 != expected_sha256:
        raise RescueUploadIntegrityError(
            f"{destination_name}: GitHub reported digest {github_asset.sha256!r}, "
            f"expected {expected_sha256!r}"
        )


def transfer_original_asset(
    client: httpx.Client,
    repository: str,
    release: GitHubRelease,
    asset: RescueAsset,
) -> GitHubAsset:
    """Stream one original asset directly from its source into a draft release."""

    response = _open_source(client, asset.source_url)
    try:
        hashing = _HashingStream(response.iter_bytes(chunk_size=_CHUNK_SIZE))
        github_asset = upload_release_asset(
            client,
            repository,
            release,
            asset.destination_name,
            asset.content_type,
            asset.expected_size,
            hashing,
        )
    finally:
        response.close()
    _verify_source(
        asset.destination_name,
        asset.expected_size,
        asset.expected_sha256,
        hashing.size,
        hashing.sha256,
    )
    verify_uploaded_asset(
        asset.destination_name, github_asset, hashing.size, hashing.sha256
    )
    return github_asset


def transfer_asset(
    client: httpx.Client,
    repository: str,
    release: GitHubRelease,
    asset: RescueAsset,
) -> GitHubAsset:
    """Transfer one rescued asset directly from its legacy source."""

    return transfer_original_asset(client, repository, release, asset)


__all__ = [
    "RescueSourceIntegrityError",
    "RescueTransferError",
    "RescueUploadIntegrityError",
    "transfer_asset",
    "transfer_original_asset",
    "verify_uploaded_asset",
]
