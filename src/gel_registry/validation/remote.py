"""Explicitly-scoped remote verification of newly added release records.

This is the only validation layer that reaches the network, so workflows can
opt into it for release pull requests alone.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path

import httpx
from pydantic import ValidationError as PydanticValidationError

from ..contracts import ReleaseRecord
from ..github import (
    GITHUB_REPOSITORY,
    GitHubAsset,
    GitHubRelease,
    asset_name,
    download_assets,
)
from ..verify import VerificationError, verify_cli_release
from .report import Collector, ValidationReport
from .support import display_path, read_file


def validate_release_remotes(
    repo: Path,
    changed_records: Iterable[object] | Mapping[object, object],
) -> ValidationReport:
    """Verify only the caller-supplied newly-added release records remotely."""

    repository = Path(repo)
    collector = Collector()
    collector.begin("remote.release.scope")
    try:
        pairs = _changed_release_pairs(repository, changed_records)
    except (OSError, PydanticValidationError, ValueError) as exc:
        collector.add("remote.release.scope", "releases", str(exc))
        return collector.report()
    if not pairs:
        return collector.report()

    collector.begin("remote.release.bytes")
    collector.begin("remote.release.attestations")
    try:
        client = httpx.Client()
    except Exception as exc:
        collector.add(
            "remote.release.scope", "releases", f"could not create HTTP client: {exc}"
        )
        return collector.report()
    try:
        for release, record, path in pairs:
            label = display_path(repository, path)
            with tempfile.TemporaryDirectory(
                prefix=".gel-registry-release-"
            ) as directory:
                destination = Path(directory) / "assets"
                try:
                    assets = download_assets(client, release, destination)
                    observed = verify_cli_release(release, assets)
                    if record is not None:
                        _compare_remote_record(record, observed)
                except Exception as exc:
                    collector.add("remote.release.bytes", label, str(exc))
    finally:
        with suppress(Exception):
            client.close()
    return collector.report()


def _synthetic_release(record: ReleaseRecord) -> GitHubRelease:
    assets: list[GitHubAsset] = []
    for index, artifact in enumerate(record.artifacts, 1):
        name = asset_name(artifact.platform, artifact.encoding)
        assets.append(
            GitHubAsset(
                name=name,
                url=f"https://api.github.com/repos/{GITHUB_REPOSITORY}/assets/{index}",
                browser_download_url=artifact.url,
                id=index,
                size=artifact.size,
            )
        )
    return GitHubRelease(
        id=record.source.release_id,
        tag_name=record.source.release_tag,
        assets=tuple(assets),
        repository=record.source.repository,
    )


def _load_release_path(repository: Path, value: object) -> tuple[ReleaseRecord, Path]:
    path = Path(value) if isinstance(value, (str, Path)) else None
    if path is None:
        raise ValueError(f"unsupported changed release value: {value!r}")
    if not path.is_absolute():
        path = repository / path
    raw = read_file(path)
    record = ReleaseRecord.model_validate_json(raw)
    return record, path


def _changed_release_pairs(
    repository: Path,
    changed_records: Iterable[object] | Mapping[object, object],
) -> list[tuple[GitHubRelease, ReleaseRecord | None, Path]]:
    values = (
        list(changed_records.values())
        if isinstance(changed_records, Mapping)
        else list(changed_records)
    )
    result: list[tuple[GitHubRelease, ReleaseRecord | None, Path]] = []
    for value in values:
        release: GitHubRelease | None = None
        record: ReleaseRecord | None = None
        path: Path | None = None
        if isinstance(value, tuple) and len(value) == 2:
            left, right = value
            if isinstance(left, GitHubRelease):
                release = left
            elif isinstance(right, GitHubRelease):
                release = right
            if isinstance(left, ReleaseRecord):
                record = left
            elif isinstance(right, ReleaseRecord):
                record = right
            if isinstance(left, (str, Path)):
                path = Path(left)
            elif isinstance(right, (str, Path)):
                path = Path(right)
        elif isinstance(value, GitHubRelease):
            release = value
        elif isinstance(value, ReleaseRecord):
            record = value
        elif isinstance(value, (str, Path)):
            path = Path(value)
        else:
            raise ValueError(f"unsupported changed release value: {value!r}")

        if record is None and path is not None:
            record, path = _load_release_path(repository, path)
        if record is None and release is not None:
            candidate = repository / "releases" / "gel-cli" / f"{release.version}.json"
            if candidate.is_file():
                record, path = _load_release_path(repository, candidate)
        if release is None and record is not None:
            release = _synthetic_release(record)
        if release is None:
            raise ValueError("changed release has no release metadata")
        if path is None:
            path = repository / "releases" / "gel-cli" / f"{release.version}.json"
        result.append((release, record, path))
    return result


def _compare_remote_record(expected: ReleaseRecord, observed: ReleaseRecord) -> None:
    expected_artifacts = {
        (artifact.platform, artifact.encoding): artifact
        for artifact in expected.artifacts
    }
    observed_artifacts = {
        (artifact.platform, artifact.encoding): artifact
        for artifact in observed.artifacts
    }
    if set(expected_artifacts) != set(observed_artifacts):
        raise VerificationError(
            "remote release artifact target set differs from the record"
        )
    for key in sorted(expected_artifacts):
        left = expected_artifacts[key]
        right = observed_artifacts[key]
        if (
            left.url != right.url
            or left.size != right.size
            or left.sha256 != right.sha256
            or left.blake2b != right.blake2b
        ):
            raise VerificationError(
                f"remote artifact differs from release record for {key[0]}/{key[1]}"
            )
