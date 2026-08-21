"""Validation of a committed, immutable promotion candidate."""

from __future__ import annotations

import re
from pathlib import Path

from ..adapters import validate_record_policy
from ..candidate import CandidateError, load_releases
from ..contracts import BlockedManifest, BlockedRelease, ReleaseRecord
from ..digest import canonical_json
from ..policy import PolicyError, SourcePolicy, load_source_policy
from .drift import check_candidate_reproduction
from .local import validate_local
from .remote import validate_blocked_remotes, validate_release_remotes
from .report import Collector, ValidationReport
from .support import display_path, read_file

# Source policy is deliberately excluded: policy changes need a separate,
# explicit transaction allowance because promotion does not emit them.
_ALLOWED_EXACT_PATHS = frozenset(
    {
        "promotion/blocked.json",
        "pointers/latest.json",
        "public/registry.json",
        "public/v1/snapshots.json",
    }
)
_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_BLOB_FILENAME = re.compile(r"^[0-9a-f]{32}\.json$")
_IGNORED_PATH_PARTS = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
)


def validate_candidate(repository: Path, base: Path) -> ValidationReport:
    """Validate and reproduce one committed candidate without discovery."""

    repo = Path(repository)
    merge_base = Path(base)
    collector = Collector()
    local = validate_local(repo, merge_base)
    _merge_report(collector, local)

    if repo.is_symlink() or not repo.is_dir():
        return collector.report()

    policy = _load_policy(repo, collector)
    records = _load_records(repo, collector)
    blocked = _load_blocked(repo, collector)
    if policy is not None:
        _check_record_policy(repo, records, policy, collector)
        _check_blocked_policy(repo, blocked, policy, collector)

        added_records = _added_record_paths(repo, merge_base, collector)
        remote = validate_release_remotes(repo, added_records, policy=policy)
        _merge_report(collector, remote)
        base_blocked = _load_base_blocked(merge_base, collector)
        changed_blocked = (
            _changed_blocked_entries(blocked, base_blocked)
            if base_blocked is not None
            else ()
        )
        if changed_blocked:
            blocked_remote = validate_blocked_remotes(
                repo, changed_blocked, policy=policy
            )
            _merge_report(collector, blocked_remote)

    _check_allowed_paths(repo, merge_base, records, collector)
    check_candidate_reproduction(repo, collector, base=merge_base)
    return collector.report()


def _load_policy(repo: Path, collector: Collector) -> SourcePolicy | None:
    check = "candidate.policy"
    collector.begin(check)
    try:
        return load_source_policy(repo)
    except (OSError, PolicyError, ValueError) as exc:
        collector.add(
            check, display_path(repo, repo / "sources/products.json"), str(exc)
        )
        return None


def _load_records(repo: Path, collector: Collector) -> tuple[ReleaseRecord, ...]:
    check = "candidate.records"
    collector.begin(check)
    try:
        return load_releases(repo)
    except (CandidateError, OSError, ValueError) as exc:
        collector.add(check, display_path(repo, repo / "releases"), str(exc))
        return ()


def _load_blocked(repo: Path, collector: Collector) -> tuple[BlockedRelease, ...]:
    check = "candidate.blocked"
    collector.begin(check)
    path = repo / "promotion" / "blocked.json"
    if not path.exists():
        return ()
    try:
        raw = read_file(path)
        manifest = BlockedManifest.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, path), str(exc))
        return ()
    if raw != canonical_json(manifest):
        collector.add(check, display_path(repo, path), "is not canonical JSON")
    if not manifest.entries:
        collector.add(
            check,
            display_path(repo, path),
            "empty blocked manifest must be omitted",
        )
    return manifest.entries


def _load_base_blocked(
    base: Path, collector: Collector
) -> tuple[BlockedRelease, ...] | None:
    """Load base blocked evidence, returning ``None`` on unsafe evidence."""

    check = "candidate.blocked.base"
    collector.begin(check)
    base = Path(base)
    promotion_root = base / "promotion"
    path = base / "promotion" / "blocked.json"
    if base.is_symlink() or not base.is_dir():
        collector.add(
            check, display_path(base, path), "merge-base tree is not a directory"
        )
        return None
    if promotion_root.is_symlink() or (
        promotion_root.exists() and not promotion_root.is_dir()
    ):
        collector.add(
            check, display_path(base, promotion_root), "is not a safe directory"
        )
        return None
    if not path.exists():
        if path.is_symlink():
            collector.add(check, display_path(base, path), "is not a regular file")
            return None
        return ()
    try:
        raw = read_file(path)
        manifest = BlockedManifest.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(base, path), str(exc))
        return None
    if raw != canonical_json(manifest):
        collector.add(check, display_path(base, path), "is not canonical JSON")
        return None
    if not manifest.entries:
        collector.add(
            check,
            display_path(base, path),
            "empty blocked manifest must be omitted",
        )
        return None
    return manifest.entries


type _BlockedIdentity = tuple[str, str, int, str]


def _blocked_identity(entry: BlockedRelease) -> _BlockedIdentity:
    return (entry.product, entry.repository, entry.release_id, entry.tag)


def _changed_blocked_entries(
    entries: tuple[BlockedRelease, ...],
    base_entries: tuple[BlockedRelease, ...],
) -> tuple[BlockedRelease, ...]:
    """Return current blocked entries that are new or changed from base."""

    previous = {_blocked_identity(entry): entry for entry in base_entries}
    return tuple(
        entry for entry in entries if previous.get(_blocked_identity(entry)) != entry
    )


def _check_record_policy(
    repo: Path,
    records: tuple[ReleaseRecord, ...],
    policy: SourcePolicy,
    collector: Collector,
) -> None:
    check = "candidate.records.policy"
    collector.begin(check)
    policies = policy.by_product()
    for record in records:
        record_path = repo / "releases" / record.product / f"{record.version}.json"
        product_policy = policies.get(record.product)
        if product_policy is None:
            collector.add(
                check,
                display_path(repo, record_path),
                f"release record product {record.product!r} has no product policy",
            )
            continue
        try:
            validate_record_policy(record, product_policy)
        except PolicyError as exc:
            collector.add(check, display_path(repo, record_path), str(exc))


def _check_blocked_policy(
    repo: Path,
    entries: tuple[BlockedRelease, ...],
    policy: SourcePolicy,
    collector: Collector,
) -> None:
    check = "candidate.blocked.policy"
    collector.begin(check)
    policies = policy.by_product()
    path = repo / "promotion" / "blocked.json"
    for entry in entries:
        product_policy = policies.get(entry.product)
        if product_policy is None:
            collector.add(
                check,
                display_path(repo, path),
                f"blocked product {entry.product!r} has no product policy",
            )
            continue
        if entry.repository != product_policy.repository:
            collector.add(
                check,
                display_path(repo, path),
                f"blocked repository {entry.repository!r} does not match policy",
            )


def _added_record_paths(
    repo: Path, base: Path, collector: Collector
) -> tuple[Path, ...]:
    check = "remote.release.scope"
    collector.begin(check)
    head_root = repo / "releases"
    base_root = base / "releases"
    try:
        head = _relative_files(head_root)
        previous = _relative_files(base_root)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, head_root), str(exc))
        return ()
    return tuple(
        repo / "releases" / relative for relative in sorted(set(head) - set(previous))
    )


def _check_allowed_paths(
    repo: Path,
    base: Path,
    records: tuple[ReleaseRecord, ...],
    collector: Collector,
) -> None:
    check = "candidate.scope"
    collector.begin(check)
    allowed_release_paths = frozenset(
        Path("releases") / record.product / f"{record.version}.json"
        for record in records
    )
    try:
        head = _repository_files(repo)
        previous = _repository_files(base)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, repo), str(exc))
        return
    for relative in sorted(set(head) | set(previous)):
        if _is_allowed_candidate_path(relative, allowed_release_paths):
            continue
        if head.get(relative) != previous.get(relative):
            collector.add(
                check, relative.as_posix(), "path changed outside promotion scope"
            )


def _is_allowed_candidate_path(
    relative: Path, allowed_release_paths: frozenset[Path]
) -> bool:
    if relative in allowed_release_paths:
        return True
    path = relative.as_posix()
    if path in _ALLOWED_EXACT_PATHS:
        return True

    parts = relative.parts
    if len(parts) == 4 and parts[:2] == ("public", "s"):
        return (
            _SNAPSHOT_ID.fullmatch(parts[2]) is not None and parts[3] == "registry.json"
        )
    # Index bodies live in the shared content-addressed blob store, so a
    # promotion may add blobs there as well as its pinned snapshot root.
    return (
        len(parts) == 3
        and parts[:2] == ("public", "i")
        and _BLOB_FILENAME.fullmatch(parts[2]) is not None
    )


def _relative_files(root: Path) -> dict[Path, bytes]:
    if not root.exists():
        if root.is_symlink():
            raise ValueError(f"is a symlink: {root}")
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"is not a directory: {root}")
    files: dict[Path, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root)):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError(f"contains symlink: {relative}")
        if path.is_file():
            files[relative] = path.read_bytes()
    return files


def _repository_files(root: Path) -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    if not root.exists():
        if root.is_symlink():
            raise ValueError(f"is a symlink: {root}")
        return files
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"is not a directory: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root)):
        relative = path.relative_to(root)
        if any(part in _IGNORED_PATH_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"contains symlink: {relative}")
        if path.is_file() and path.suffix != ".pyc":
            files[relative] = path.read_bytes()
    return files


def _merge_report(collector: Collector, report: ValidationReport) -> None:
    for check in report.checks:
        collector.begin(check)
    collector.errors.extend(report.errors)


__all__ = ["validate_candidate"]
