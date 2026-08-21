"""Bootstrap-only publication transaction.

The transaction stages the repository inputs, renders the resulting immutable
snapshot and moving documents, then installs only the byte differences.  It
has no knowledge of product policy, release discovery, candidates, blocked
state, or promotion branches.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from . import storage
from .contracts import PackageIndex, Pointer
from .digest import canonical_json
from .render import (
    RenderError,
    build_snapshot,
    is_approved_release_record_predecessor,
    render_schemas,
    select_snapshot,
)

_SOURCE_ROOTS = ("bootstrap", "releases", "pointers", "public")
_MUTABLE_PATHS = frozenset(
    {
        "pointers/latest.json",
        "public/registry.json",
        "public/v1/snapshots.json",
        "vercel.json",
    }
)
_MOVING_PATHS = (
    "public/registry.json",
    "public/v1/snapshots.json",
    "vercel.json",
    "pointers/latest.json",
)
#: Generated files that sit at the repository root rather than inside one of
#: the source families, because the host reads them from there.
_ROOT_FILES = ("vercel.json",)
_MIGRATED_SUPPORT_PATH = "public/v1/schema/release-record.json"
_SUPPORT_PATHS = frozenset(
    {
        "public/healthz",
        "public/v1/schema/capture.json",
        "public/v1/schema/package-index.json",
        "public/v1/schema/pointer.json",
        "public/v1/schema/release-record.json",
        "public/v1/schema/root.json",
        "public/v1/schema/snapshot-listing.json",
    }
)
_BLOB_FILENAME = re.compile(r"^[0-9a-f]{32}\.json$")
type _TreeEntry = bytes | None


class PublicationError(RuntimeError):
    """Raised when a bootstrap publication cannot be committed safely."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """The deterministic result of a bootstrap publication transaction."""

    snapshot: str
    changed_paths: tuple[str, ...]


def publish_bootstrap(repo: Path) -> PublicationResult:
    """Build, select, and install a bootstrap-backed immutable snapshot."""

    repo = Path(repo)
    _validate_repository(repo)
    _validate_pointer(repo / "pointers" / "latest.json")
    _validate_bootstrap(repo / "bootstrap")
    stage = _stage_repository(repo)
    try:
        snapshot = build_snapshot(stage)
        _write_pointer(stage, snapshot)
        select_snapshot(stage)
        render_schemas(stage, allow_release_record_migration=True)
        changed_paths = _compare_transaction(repo, stage, snapshot=snapshot)
        _install_transaction(repo, stage, changed_paths)
        return PublicationResult(snapshot=snapshot, changed_paths=changed_paths)
    except (PublicationError, RenderError):
        raise
    except (OSError, TypeError, ValidationError, ValueError) as exc:
        raise PublicationError(f"bootstrap publication failed: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _validate_repository(repo: Path) -> None:
    if repo.is_symlink() or not repo.is_dir():
        raise PublicationError(f"repository is not a directory: {repo}")
    for name in _SOURCE_ROOTS:
        source = repo / name
        if source.exists() or source.is_symlink():
            _tree_entries(source, name)


def _validate_pointer(path: Path) -> bytes | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PublicationError(f"latest pointer is not a regular file: {path}")
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        pointer = Pointer.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        raise PublicationError(f"invalid latest pointer {path}: {exc}") from exc
    if raw != canonical_json(pointer):
        raise PublicationError(f"latest pointer is not canonical: {path}")
    return raw


def _validate_bootstrap(root: Path) -> tuple[Path, ...]:
    if root.is_symlink() or not root.is_dir():
        raise PublicationError(f"bootstrap indexes are missing: {root}")
    entries = _tree_entries(root, "bootstrap")
    paths = tuple(
        root / relative
        for relative, data in sorted(entries.items())
        if data is not None and Path(relative).suffix == ".json"
    )
    if not paths:
        raise PublicationError(f"bootstrap indexes are missing: {root}")
    package_count = 0
    for path in paths:
        try:
            package_count += len(
                PackageIndex.model_validate_json(path.read_bytes()).packages
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise PublicationError(f"invalid bootstrap index {path}: {exc}") from exc
    if package_count == 0:
        raise PublicationError("bootstrap contains no bootstrap packages")
    return paths


def _tree_entries(root: Path, label: str) -> dict[str, _TreeEntry]:
    if not root.exists():
        if root.is_symlink():
            raise PublicationError(f"{label} is a symlink: {root}")
        return {}
    try:
        return storage.read_tree(root)
    except ValueError as exc:
        raise PublicationError(f"{label} {exc}") from exc
    except OSError as exc:
        raise PublicationError(f"could not inspect {label}: {exc}") from exc


def _copy_tree(source: Path, destination: Path, label: str) -> None:
    entries = _tree_entries(source, label)
    try:
        for relative, value in sorted(
            entries.items(), key=lambda item: (item[0].count("/"), item[0])
        ):
            path = destination / relative
            if value is None:
                storage.ensure_directory_chain(path)
            else:
                storage.ensure_directory_chain(path.parent)
                path.write_bytes(value)
    except (OSError, ValueError) as exc:
        raise PublicationError(f"could not stage {label}: {exc}") from exc


def _stage_repository(repo: Path) -> Path:
    try:
        stage = Path(
            tempfile.mkdtemp(prefix=f".{repo.name}.publication-", dir=str(repo.parent))
        )
    except OSError as exc:
        raise PublicationError(
            f"could not create publication staging tree: {exc}"
        ) from exc
    try:
        for name in _SOURCE_ROOTS:
            source = repo / name
            if source.exists() or source.is_symlink():
                _copy_tree(source, stage / name, name)
        return stage
    except PublicationError:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _write_pointer(stage: Path, snapshot: str) -> None:
    path = stage / "pointers" / "latest.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PublicationError(f"latest pointer is not a regular file: {path}")
    try:
        storage.ensure_directory_chain(path.parent)
        path.write_bytes(canonical_json(Pointer(snapshot=snapshot)))
    except (OSError, ValueError) as exc:
        raise PublicationError(f"could not stage latest pointer {path}: {exc}") from exc


def _is_mutable(relative: str) -> bool:
    return relative in _MUTABLE_PATHS


def _is_migrated_support(relative: str) -> bool:
    return relative == _MIGRATED_SUPPORT_PATH


def _allowed_addition(relative: str, snapshot: str) -> bool:
    if relative in _MUTABLE_PATHS or relative in _SUPPORT_PATHS:
        return True
    parts = Path(relative).parts
    # A blob in the shared content-addressed store. Matched by exact shape
    # rather than by a bare ``public/i/`` prefix so a typo cannot smuggle an
    # arbitrary file into the immutable store.
    if (
        len(parts) == 3
        and parts[0] == "public"
        and parts[1] == "i"
        and _BLOB_FILENAME.fullmatch(parts[2]) is not None
    ):
        return True
    return (
        len(parts) >= 3
        and parts[0] == "public"
        and parts[1] == "s"
        and parts[2] == snapshot
    )


def _compare_family(
    repo: Path,
    stage: Path,
    name: str,
    *,
    snapshot: str,
    changed: set[str],
) -> None:
    existing = _tree_entries(repo / name, name)
    candidate = _tree_entries(stage / name, f"staged {name}")
    for relative in sorted(set(existing) | set(candidate)):
        full = f"{name}/{relative}"
        left = existing.get(relative)
        right = candidate.get(relative)
        if relative not in candidate:
            raise PublicationError(f"publication removed immutable path: {full}")
        if relative not in existing:
            if right is None:
                continue
            if not _allowed_addition(full, snapshot):
                raise PublicationError(f"publication added unexpected path: {full}")
            changed.add(full)
            continue
        if left == right:
            continue
        if _is_mutable(full):
            if right is None:
                raise PublicationError(f"mutable path became a directory: {full}")
            changed.add(full)
            continue
        if _is_migrated_support(full):
            if right is None:
                raise PublicationError(
                    f"migrated support path became a directory: {full}"
                )
            if left is None or not is_approved_release_record_predecessor(left):
                raise PublicationError(f"immutable path changed: {full}")
            changed.add(full)
            continue
        raise PublicationError(f"immutable path changed: {full}")


def _compare_root_file(
    repo: Path,
    stage: Path,
    relative: str,
    *,
    changed: set[str],
) -> None:
    """Compare one generated repository-root file outside the source families."""

    existing = _prior_file(repo / relative, relative)
    candidate = _prior_file(stage / relative, f"staged {relative}")
    if candidate is None:
        raise PublicationError(f"publication did not render {relative}")
    if existing != candidate:
        changed.add(relative)


def _compare_transaction(repo: Path, stage: Path, *, snapshot: str) -> tuple[str, ...]:
    changed: set[str] = set()
    for relative in _ROOT_FILES:
        _compare_root_file(repo, stage, relative, changed=changed)
    for name in _SOURCE_ROOTS:
        _compare_family(repo, stage, name, snapshot=snapshot, changed=changed)
    return tuple(sorted(changed))


def _preflight_parent(repo: Path, path: Path, label: str) -> None:
    try:
        path.parent.relative_to(repo)
    except ValueError as exc:
        raise PublicationError(f"{label} escapes repository: {path}") from exc
    current = path.parent
    while True:
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise PublicationError(f"{label} parent is not a directory: {current}")
        if current == repo:
            return
        parent = current.parent
        if parent == current:
            raise PublicationError(f"{label} parent escapes repository: {path}")
        current = parent


def _preflight_file(repo: Path, path: Path, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PublicationError(f"{label} is not a regular file: {path}")
    _preflight_parent(repo, path, label)


def _ensure_parent(repo: Path, path: Path, created_dirs: list[Path]) -> None:
    _preflight_parent(repo, path, "publication path")
    try:
        created_dirs.extend(storage.ensure_directory_chain(path.parent))
    except (OSError, ValueError) as exc:
        raise PublicationError(
            f"could not create publication parent {path.parent}: {exc}"
        ) from exc


def _create_file(
    repo: Path,
    path: Path,
    data: bytes,
    label: str,
    created_dirs: list[Path],
) -> bool:
    _preflight_file(repo, path, label)
    _ensure_parent(repo, path, created_dirs)
    try:
        return storage.create_atomic(path, data)
    except ValueError as exc:
        raise PublicationError(str(exc)) from exc
    except OSError as exc:
        raise PublicationError(
            f"could not install immutable {label} {path}: {exc}"
        ) from exc


def _replace_file(
    repo: Path,
    path: Path,
    data: bytes,
    label: str,
    created_dirs: list[Path],
) -> None:
    _preflight_file(repo, path, label)
    _ensure_parent(repo, path, created_dirs)
    try:
        storage.replace_atomic(path, data)
    except OSError as exc:
        raise PublicationError(
            f"could not atomically write {label} {path}: {exc}"
        ) from exc


def _prior_file(path: Path, label: str) -> bytes | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PublicationError(f"{label} is not a regular file: {path}")
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PublicationError(f"could not read prior {label} {path}: {exc}") from exc


def _restore_file(
    repo: Path,
    path: Path,
    previous: bytes | None,
    label: str,
    created_dirs: list[Path],
) -> None:
    if previous is None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise PublicationError(f"cannot remove {label}: {path}")
            try:
                path.unlink()
            except OSError as exc:
                raise PublicationError(
                    f"could not restore {label}: {path}: {exc}"
                ) from exc
        return
    if path.exists() and path.is_file() and path.read_bytes() == previous:
        return
    _replace_file(repo, path, previous, label, created_dirs)


def _rollback_created(
    created_files: list[tuple[Path, bytes]], created_dirs: list[Path]
) -> None:
    for path, expected in reversed(created_files):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.read_bytes() == expected:
                path.unlink()
        except OSError:
            continue
    for path in sorted(
        set(created_dirs), key=lambda item: len(item.parts), reverse=True
    ):
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
        except OSError:
            continue


def _install_transaction(repo: Path, stage: Path, changed_paths: Iterable[str]) -> None:
    changed = tuple(sorted(changed_paths))
    immutable = tuple(
        path
        for path in changed
        if not _is_mutable(path) and not _is_migrated_support(path)
    )
    migrated = tuple(path for path in changed if _is_migrated_support(path))
    mutable = tuple(path for path in _MOVING_PATHS if path in changed)
    transactional = (*migrated, *mutable)
    for relative in transactional:
        _preflight_file(repo, repo / relative, relative)
    prior = {
        relative: _prior_file(repo / relative, relative) for relative in transactional
    }

    created_files: list[tuple[Path, bytes]] = []
    created_dirs: list[Path] = []
    try:
        for relative in immutable:
            source = stage / relative
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise PublicationError(
                    f"could not read staged path {relative}: {exc}"
                ) from exc
            if _create_file(repo, repo / relative, data, relative, created_dirs):
                created_files.append((repo / relative, data))

        for relative in migrated:
            source = stage / relative
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise PublicationError(
                    f"could not read staged migrated path {relative}: {exc}"
                ) from exc
            if prior[relative] is None:
                if _create_file(repo, repo / relative, data, relative, created_dirs):
                    created_files.append((repo / relative, data))
            else:
                _replace_file(repo, repo / relative, data, relative, created_dirs)

        for relative in mutable:
            source = stage / relative
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise PublicationError(
                    f"could not read staged mutable path {relative}: {exc}"
                ) from exc
            _replace_file(repo, repo / relative, data, relative, created_dirs)
    except (OSError, PublicationError) as exc:
        try:
            for relative in reversed(mutable):
                _restore_file(
                    repo,
                    repo / relative,
                    prior[relative],
                    relative,
                    created_dirs,
                )
            for relative in reversed(migrated):
                _restore_file(
                    repo,
                    repo / relative,
                    prior[relative],
                    relative,
                    created_dirs,
                )
        except PublicationError as rollback_error:
            _rollback_created(created_files, created_dirs)
            raise PublicationError(
                f"publication failed and rollback failed: {rollback_error}"
            ) from exc
        _rollback_created(created_files, created_dirs)
        raise


__all__ = ["PublicationError", "PublicationResult", "publish_bootstrap"]
