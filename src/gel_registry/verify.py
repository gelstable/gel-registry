"""Independent byte, URL, pair, and attestation verification for releases."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import zstandard as zstd

from .constants import CLI_PLATFORMS
from .digest import Digests, hash_file
from .github import GITHUB_REPOSITORY, GitHubAsset, GitHubRelease, canonical_asset_names
from .schema import Artifact, ReleaseRecord, ReleaseSource, _media_type_for_platform

_STREAM_CHUNK_SIZE = 1024 * 1024


class VerificationError(RuntimeError):
    """Raised when a release cannot be independently verified."""


Attestor = Callable[[Path], None]


def gh_attestor(path: Path) -> None:
    """Verify one artifact with GitHub's production attestation command."""

    subprocess.run(
        ["gh", "attestation", "verify", str(path), "-R", GITHUB_REPOSITORY],
        check=True,
    )


def _asset_url(asset: GitHubAsset) -> str:
    return asset.download_url


def _validate_asset_url(release: GitHubRelease, asset: GitHubAsset) -> str:
    url = _asset_url(asset)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise VerificationError(
            f"asset {asset.name!r} URL is malformed: {url!r}"
        ) from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise VerificationError(
            f"asset {asset.name!r} URL has an invalid port: {url!r}"
        ) from exc
    expected_path = (
        f"/{GITHUB_REPOSITORY}/releases/download/{release.tag_name}/{asset.name}"
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
    ):
        raise VerificationError(
            f"asset {asset.name!r} URL is not the allowlisted release URL: {url!r}"
        )
    return url


def _ensure_regular_file(path: Path, name: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"asset {name!r} is not a regular file: {path}")
    return path


def _same_identity_bytes(identity: Path, compressed: Path, name: str) -> None:
    try:
        with (
            identity.open("rb") as identity_stream,
            compressed.open("rb") as compressed_stream,
        ):
            reader = zstd.ZstdDecompressor().stream_reader(compressed_stream)
            try:
                expected_buffer = b""
                observed_buffer = b""
                while True:
                    if not expected_buffer:
                        expected_buffer = identity_stream.read(_STREAM_CHUNK_SIZE)
                    if not observed_buffer:
                        observed_buffer = reader.read(_STREAM_CHUNK_SIZE)
                    if not expected_buffer and not observed_buffer:
                        return
                    if not expected_buffer or not observed_buffer:
                        raise VerificationError(
                            f"zstd asset {name!r} does not match its identity asset"
                        )
                    size = min(len(expected_buffer), len(observed_buffer))
                    if expected_buffer[:size] != observed_buffer[:size]:
                        raise VerificationError(
                            f"zstd asset {name!r} does not match its identity asset"
                        )
                    expected_buffer = expected_buffer[size:]
                    observed_buffer = observed_buffer[size:]
            finally:
                reader.close()  # type: ignore[no-untyped-call]
    except VerificationError:
        raise
    except (OSError, ValueError, zstd.ZstdError) as exc:
        raise VerificationError(
            f"could not decompress zstd asset {name!r}: {exc}"
        ) from exc


def _artifact(
    platform: str,
    encoding: Literal["identity", "zstd"],
    url: str,
    digests: Digests,
) -> Artifact:
    return Artifact(
        platform=platform,
        encoding=encoding,
        media_type=_media_type_for_platform(platform),
        url=url,
        size=digests.size,
        sha256=digests.sha256,
        blake2b=digests.blake2b,
    )


def _installref_parts(platform: str) -> tuple[str, str]:
    suffix = ".exe" if platform.endswith("-windows-msvc") else ""
    identity = f"gel-cli-{platform}{suffix}"
    return identity, f"{identity}.zst"


def _installref_like(name: str) -> bool:
    if not name.startswith("gel-cli-"):
        return False
    return any(name.startswith(f"gel-cli-{platform}") for platform in CLI_PLATFORMS)


def verify_cli_release(
    release: GitHubRelease,
    assets: Mapping[str, Path],
    attestor: Attestor | None = None,
) -> ReleaseRecord:
    """Verify all release assets and return a complete immutable release record."""

    if release.repository != GITHUB_REPOSITORY:
        raise VerificationError("release repository is not allowlisted")
    if release.draft or release.prerelease:
        raise VerificationError("release must be published and stable")
    if not release.tag_name.startswith("v"):
        raise VerificationError("release tag must be exactly v<semver>")
    names = [asset.name for asset in release.assets]
    if len(names) != len(set(names)):
        raise VerificationError("release contains duplicate asset names")
    expected_names = set(canonical_asset_names())
    release_names = set(names)
    missing = sorted(expected_names - release_names)
    if missing:
        raise VerificationError(
            "release is missing canonical installrefs: " + ", ".join(missing)
        )
    extra_installrefs = sorted(
        name for name in release_names - expected_names if _installref_like(name)
    )
    if extra_installrefs:
        raise VerificationError(
            "release contains extra installrefs: " + ", ".join(extra_installrefs)
        )
    if set(assets) != release_names:
        missing_files = sorted(release_names - set(assets))
        extra_files = sorted(set(assets) - release_names)
        details: list[str] = []
        if missing_files:
            details.append("missing=" + ",".join(missing_files))
        if extra_files:
            details.append("extra=" + ",".join(extra_files))
        raise VerificationError(
            "asset path mapping does not match release: " + "; ".join(details)
        )

    paths: dict[str, Path] = {}
    urls: dict[str, str] = {}
    by_name: dict[str, GitHubAsset] = {}
    for asset in release.assets:
        by_name[asset.name] = asset
        urls[asset.name] = _validate_asset_url(release, asset)
        paths[asset.name] = _ensure_regular_file(assets[asset.name], asset.name)

    pairs: dict[str, tuple[str, str]] = {
        platform: _installref_parts(platform) for platform in CLI_PLATFORMS
    }
    artifacts: list[Artifact] = []
    for platform in CLI_PLATFORMS:
        identity_name, zstd_name = pairs[platform]
        identity_path = paths[identity_name]
        zstd_path = paths[zstd_name]
        identity_digests = hash_file(identity_path)
        zstd_digests = hash_file(zstd_path)
        _same_identity_bytes(identity_path, zstd_path, zstd_name)
        artifacts.extend(
            (
                _artifact(platform, "identity", urls[identity_name], identity_digests),
                _artifact(platform, "zstd", urls[zstd_name], zstd_digests),
            )
        )

    selected_attestor = attestor or gh_attestor
    for asset in release.assets:
        try:
            selected_attestor(paths[asset.name])
        except VerificationError:
            raise
        except Exception as exc:
            raise VerificationError(
                f"attestation failed for asset {asset.name!r}: {exc}"
            ) from exc

    return ReleaseRecord(
        product="gel-cli",
        channel="stable",
        version=release.version,
        source=ReleaseSource(
            repository=GITHUB_REPOSITORY,
            release_tag=release.tag_name,
            release_id=release.id,
        ),
        promoted_at=datetime.now(UTC),
        artifacts=tuple(artifacts),
    )


__all__ = [
    "Attestor",
    "VerificationError",
    "gh_attestor",
    "verify_cli_release",
]
