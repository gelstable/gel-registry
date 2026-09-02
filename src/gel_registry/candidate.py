"""Build a complete local candidate from gathered release records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from .contracts import ReleaseRecord
from .digest import canonical_json
from .gather import GatherResult, RejectedRelease, gather_missing
from .publication import publish_registry


@dataclass(frozen=True)
class CandidateResult:
    snapshot: str
    records: tuple[ReleaseRecord, ...]
    rejected: tuple[RejectedRelease, ...]


def release_record_path(repo: Path, record: ReleaseRecord) -> Path:
    """Return the canonical source-identity path for one release record."""
    owner, repository = record.source.repository.split("/", 1)
    return repo / "releases" / owner / repository / f"{record.source.release_id}.json"


def _validate_record_path(repo: Path, path: Path, data: bytes) -> None:
    current = path.parent
    while current != repo:
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError(f"release record parent is not a directory: {current}")
        current = current.parent
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"release record path is not a regular file: {path}")
    if path.exists() and path.read_bytes() != data:
        raise ValueError(f"release record path collides with different bytes: {path}")


def _write_records(repo: Path, records: tuple[ReleaseRecord, ...]) -> None:
    if repo.is_symlink() or not repo.is_dir():
        raise ValueError(f"repository is not a directory: {repo}")
    planned: dict[Path, bytes] = {}
    for record in records:
        path = release_record_path(repo, record)
        data = canonical_json(record)
        if path in planned and planned[path] != data:
            raise ValueError(
                f"release record path collides with different bytes: {path}"
            )
        planned[path] = data
    for path, data in planned.items():
        _validate_record_path(repo, path, data)
    for path, data in planned.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)


def build_candidate(repo: Path, client: httpx.Client) -> CandidateResult:
    """Gather missing records and publish one complete local candidate."""
    gathered: GatherResult = gather_missing(repo, client)
    _write_records(repo, gathered.records)
    publication = publish_registry(repo)
    return CandidateResult(
        snapshot=publication.snapshot,
        records=gathered.records,
        rejected=gathered.rejected,
    )
