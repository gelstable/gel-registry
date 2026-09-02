"""Gather valid publisher manifests from the canonical GitHub allowlist."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import ValidationError

from .contracts import ReleaseManifest, ReleaseRecord, ReleaseSource
from .digest import canonical_json
from .github import GitHubRelease, fetch_manifest_asset, list_releases
from .render import RenderError, files


@dataclass(frozen=True)
class RejectedRelease:
    repository: str
    release_id: int
    tag: str
    reason: str


@dataclass(frozen=True)
class GatherResult:
    records: tuple[ReleaseRecord, ...]
    rejected: tuple[RejectedRelease, ...]


def load_repositories(repo: Path) -> tuple[str, ...]:
    """Load the canonical repository allowlist."""
    raw = (repo / "sources" / "github.json").read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("GitHub source allowlist has an unsupported schema version")
    repositories = data.get("repositories")
    if (
        not isinstance(repositories, list)
        or not all(isinstance(repository, str) for repository in repositories)
        or any(repository.count("/") != 1 for repository in repositories)
        or len(repositories) != len(set(repositories))
    ):
        raise ValueError("GitHub source allowlist has invalid repositories")
    if raw != canonical_json({"repositories": repositories, "schema_version": 1}):
        raise ValueError("GitHub source allowlist is not canonical")
    return tuple(repositories)


def _record_path(repo: Path, record: ReleaseRecord) -> Path:
    owner, repository = record.source.repository.split("/", 1)
    return repo / "releases" / owner / repository / f"{record.source.release_id}.json"


def load_records(repo: Path) -> tuple[ReleaseRecord, ...]:
    """Load canonical committed records and verify their identity paths."""
    try:
        paths = files.json_files(repo / "releases", "releases")
    except RenderError as exc:
        raise ValueError(f"invalid committed release records: {exc}") from exc
    records: list[ReleaseRecord] = []
    for path in paths:
        try:
            raw = path.read_bytes()
            record = ReleaseRecord.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise ValueError(
                f"could not read committed release record: {path}"
            ) from exc
        if path != _record_path(repo, record):
            raise ValueError(
                f"committed release record has wrong identity path: {path}"
            )
        if raw != canonical_json(record):
            raise ValueError(f"committed release record is not canonical: {path}")
        records.append(record)
    return tuple(records)


def _asset_names_are_bound(record: ReleaseRecord, release: GitHubRelease) -> None:
    names = {asset.name for asset in release.assets}
    from .contracts.release import release_urls

    for url in release_urls(record):
        if unquote(urlsplit(url).path.rsplit("/", 1)[-1]) not in names:
            raise ValueError(
                "release record references an asset absent from the release"
            )


def _reason(error: Exception) -> str:
    message = str(error).strip()
    return (message or error.__class__.__name__).splitlines()[0]


def _bind_manifest(raw: bytes, release: GitHubRelease) -> ReleaseRecord:
    manifest = ReleaseManifest.model_validate_json(raw)
    record = ReleaseRecord(
        **manifest.model_dump(),
        source=ReleaseSource(
            repository=release.repository,
            release_id=release.release_id,
            tag=release.tag,
            published_at=release.published_at,
        ),
    )
    _asset_names_are_bound(record, release)
    return record


def gather_missing(repo: Path, client: httpx.Client) -> GatherResult:
    """Gather every eligible release identity absent from committed records."""
    existing = {
        (record.source.repository, record.source.release_id)
        for record in load_records(repo)
    }
    records: list[ReleaseRecord] = []
    rejected: list[RejectedRelease] = []
    for repository in load_repositories(repo):
        for release in list_releases(client, repository):
            identity = (repository, release.release_id)
            if release.draft or identity in existing:
                continue
            assets = [
                asset for asset in release.assets if asset.name == "gel-registry.json"
            ]
            if not assets:
                continue
            if len(assets) != 1:
                rejected.append(
                    RejectedRelease(
                        repository,
                        release.release_id,
                        release.tag,
                        "release has more than one gel-registry.json asset",
                    )
                )
                continue
            try:
                records.append(
                    _bind_manifest(fetch_manifest_asset(client, release), release)
                )
            except (ValidationError, ValueError) as exc:
                rejected.append(
                    RejectedRelease(
                        repository, release.release_id, release.tag, _reason(exc)
                    )
                )
    return GatherResult(records=tuple(records), rejected=tuple(rejected))
