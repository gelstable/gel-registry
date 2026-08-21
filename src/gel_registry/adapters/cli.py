"""The product adapter for Gel CLI GitHub releases."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import zstandard as zstd

from ..contracts import Artifact, BlockedCategory, ReleaseRecord, ReleaseSource
from ..digest import Digests, hash_file
from ..github import GITHUB_REPOSITORY, GitHubAsset, GitHubRelease, asset_name
from ..policy import PolicyError, ProductPolicy
from .base import (
    Attestor,
    DeterministicVerificationError,
    VerificationError,
    validate_record_policy,
)

_STREAM_CHUNK_SIZE = 1024 * 1024
_NEGATIVE_ATTESTATION_MARKERS = ("verification failed", "no attestations found")


def _media_type_for_platform(platform: str) -> str:
    if platform.endswith("-apple-darwin"):
        return "application/x-mach-binary"
    if platform.endswith("-windows-msvc"):
        return "application/x-dosexec"
    return "application/x-pie-executable"


def _release_matches_repository(policy: ProductPolicy, release: GitHubRelease) -> bool:
    """Ensure all repository evidence points at the policy source."""

    return release.repository == policy.repository and (
        not release.repositories
        or release.repositories == frozenset({policy.repository})
    )


def gh_attestor(path: Path, repository: str = GITHUB_REPOSITORY) -> None:
    """Verify one artifact with GitHub's production attestation command."""

    try:
        subprocess.run(
            ["gh", "attestation", "verify", str(path), "-R", repository],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if _is_negative_attestation_result(exc):
            raise DeterministicVerificationError(
                BlockedCategory.ATTESTATION_FAILED,
                f"attestation failed for asset {path.name!r}",
                assets=(path.name,),
            ) from exc
        raise


def _is_negative_attestation_result(
    error: subprocess.CalledProcessError,
) -> bool:
    """Recognize only gh's deterministic verified-rejection result."""

    if error.returncode != 1:
        return False
    output = "\n".join(
        value for value in (error.stdout, error.stderr) if isinstance(value, str)
    ).casefold()
    return any(marker in output for marker in _NEGATIVE_ATTESTATION_MARKERS)


def _validate_asset_url(
    policy: ProductPolicy, release: GitHubRelease, asset: GitHubAsset
) -> str:
    url = asset.download_url
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise DeterministicVerificationError(
            BlockedCategory.INVALID_METADATA,
            f"asset {asset.name!r} URL is malformed: {url!r}",
            assets=(asset.name,),
        ) from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise DeterministicVerificationError(
            BlockedCategory.INVALID_METADATA,
            f"asset {asset.name!r} URL has an invalid port: {url!r}",
            assets=(asset.name,),
        ) from exc
    expected_path = (
        f"/{policy.repository}/releases/download/{release.tag_name}/{asset.name}"
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
        raise DeterministicVerificationError(
            BlockedCategory.INVALID_METADATA,
            f"asset {asset.name!r} URL is not the allowlisted release URL: {url!r}",
            assets=(asset.name,),
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
                        raise DeterministicVerificationError(
                            BlockedCategory.ARTIFACT_MISMATCH,
                            f"zstd asset {name!r} does not match its identity asset",
                            assets=(name,),
                        )
                    size = min(len(expected_buffer), len(observed_buffer))
                    if expected_buffer[:size] != observed_buffer[:size]:
                        raise DeterministicVerificationError(
                            BlockedCategory.ARTIFACT_MISMATCH,
                            f"zstd asset {name!r} does not match its identity asset",
                            assets=(name,),
                        )
                    expected_buffer = expected_buffer[size:]
                    observed_buffer = observed_buffer[size:]
            finally:
                reader.close()  # type: ignore[no-untyped-call]
    except (ValueError, zstd.ZstdError) as exc:
        raise DeterministicVerificationError(
            BlockedCategory.ARTIFACT_MISMATCH,
            f"could not decompress zstd asset {name!r}: {exc}",
            assets=(name,),
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


class CliReleaseAdapter:
    """Verify the fixed GitHub artifact contract for ``gel-cli``."""

    name = "gel-cli"

    def canonical_asset_names(self, policy: ProductPolicy) -> tuple[str, ...]:
        """Return the policy's canonical identity and zstd asset names."""

        return tuple(
            asset_name(platform, encoding)
            for platform in policy.platforms
            for encoding in policy.encodings
        )

    def eligible(self, policy: ProductPolicy, release: GitHubRelease) -> bool:
        """Return whether a release matches the stable CLI policy."""

        return (
            policy.adapter == self.name
            and _release_matches_repository(policy, release)
            and not release.draft
            and not release.prerelease
            and re.fullmatch(policy.tag_pattern, release.tag_name) is not None
            and release.tag_name == f"v{release.version}"
        )

    def verify(
        self,
        policy: ProductPolicy,
        release: GitHubRelease,
        assets: Mapping[str, Path],
        attestor: Attestor | None = None,
    ) -> ReleaseRecord:
        """Verify CLI assets and return a complete immutable release record."""

        if policy.adapter != self.name:
            raise DeterministicVerificationError(
                BlockedCategory.INVALID_METADATA,
                f"policy adapter {policy.adapter!r} is not supported by {self.name}",
            )
        if not _release_matches_repository(policy, release):
            raise DeterministicVerificationError(
                BlockedCategory.INVALID_METADATA,
                "release repository is not allowlisted",
            )
        if release.draft or release.prerelease:
            raise DeterministicVerificationError(
                BlockedCategory.INVALID_METADATA,
                "release must be published and stable",
            )
        if re.fullmatch(policy.tag_pattern, release.tag_name) is None:
            raise DeterministicVerificationError(
                BlockedCategory.INVALID_METADATA,
                "release tag is not allowed by policy",
            )
        if release.tag_name != f"v{release.version}":
            raise DeterministicVerificationError(
                BlockedCategory.INVALID_METADATA,
                "release tag must exactly be v<version>",
            )

        names = [asset.name for asset in release.assets]
        if len(names) != len(set(names)):
            raise DeterministicVerificationError(
                BlockedCategory.INVALID_METADATA,
                "release contains duplicate asset names",
            )
        canonical_names = self.canonical_asset_names(policy)
        expected_names = set(canonical_names)
        release_names = set(names)
        missing = sorted(expected_names - release_names)
        if missing:
            raise DeterministicVerificationError(
                BlockedCategory.MISSING_ARTIFACTS,
                "release is missing canonical installrefs: " + ", ".join(missing),
                assets=missing,
            )
        canonical_assets = tuple(
            asset for asset in release.assets if asset.name in expected_names
        )
        missing_files = sorted(expected_names - set(assets))
        if missing_files:
            raise VerificationError(
                "asset path mapping is missing canonical assets: "
                + ", ".join(missing_files)
            )

        paths: dict[str, Path] = {}
        urls: dict[str, str] = {}
        for asset in canonical_assets:
            urls[asset.name] = _validate_asset_url(policy, release, asset)
            paths[asset.name] = _ensure_regular_file(assets[asset.name], asset.name)

        artifacts: list[Artifact] = []
        for platform in policy.platforms:
            identity_name = asset_name(platform, "identity")
            zstd_name = asset_name(platform, "zstd")
            identity_path = paths[identity_name]
            zstd_path = paths[zstd_name]
            identity_digests = hash_file(identity_path)
            zstd_digests = hash_file(zstd_path)
            _same_identity_bytes(identity_path, zstd_path, zstd_name)
            artifacts.extend(
                (
                    _artifact(
                        platform, "identity", urls[identity_name], identity_digests
                    ),
                    _artifact(platform, "zstd", urls[zstd_name], zstd_digests),
                )
            )

        selected_attestor = attestor or (
            lambda path: gh_attestor(path, policy.repository)
        )
        for name in canonical_names:
            try:
                selected_attestor(paths[name])
            except VerificationError:
                raise
            except subprocess.CalledProcessError:
                raise
            except Exception as exc:
                raise VerificationError(
                    f"attestation failed for asset {name!r}: {exc}"
                ) from exc

        record = ReleaseRecord(
            product=policy.product,
            channel=policy.channel,
            version=release.version,
            source=ReleaseSource(
                repository=policy.repository,
                release_tag=release.tag_name,
                release_id=release.id,
            ),
            promoted_at=datetime.now(UTC),
            artifacts=tuple(artifacts),
        )
        try:
            validate_record_policy(record, policy)
        except PolicyError as exc:
            raise DeterministicVerificationError(
                BlockedCategory.INVALID_METADATA,
                str(exc),
            ) from exc
        return record


__all__ = ["CliReleaseAdapter", "gh_attestor"]
