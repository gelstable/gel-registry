"""Comparison, installation and rollback against the caller's repository.

Immutable paths are created without replacing a raced destination, and the
three mutable documents are published as a pair-then-pointer sequence whose
prior bytes are restored if any single write fails.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from .. import storage
from ..contracts import ReleaseRecord
from ..render import RenderError, build_snapshot, select_snapshot
from .result import PromotionError, PromotionResult
from .staging import (
    _SOURCE_ROOTS,
    _bootstrap_indexes,
    _canonical_pointer,
    _ensure_nonempty_snapshot,
    _prepare_release,
    _stage_repository,
    _tree_entries,
    _validate_repo,
    _write_pointer,
)

_MUTABLE_PATHS = frozenset(
    {
        "pointers/latest.json",
        "public/registry.json",
        "public/v1/snapshots.json",
    }
)


def _transaction(
    repo: Path,
    *,
    release: ReleaseRecord | None,
    branch: str,
) -> PromotionResult:
    repo = Path(repo)
    _validate_repo(repo)
    _canonical_pointer(repo / "pointers" / "latest.json")
    _bootstrap_indexes(repo / "bootstrap")

    stage = _stage_repository(repo)
    release_path: str | None = None
    try:
        staged_bootstrap_indexes = _bootstrap_indexes(stage / "bootstrap")
        if release is not None:
            release_path = f"releases/gel-cli/{release.version}.json"
            _prepare_release(stage, release)
        snapshot = build_snapshot(stage)
        _ensure_nonempty_snapshot(
            stage,
            snapshot,
            bootstrap_indexes=staged_bootstrap_indexes,
            require_bootstrap_packages=release is None,
        )
        _write_pointer(stage, snapshot)
        select_snapshot(stage)
        changed_paths = _compare_transaction(
            repo,
            stage,
            snapshot=snapshot,
            release_path=release_path,
        )
        _install_transaction(repo, stage, changed_paths)
        return PromotionResult(
            snapshot=snapshot,
            branch=branch,
            changed_paths=changed_paths,
        )
    except PromotionError:
        raise
    except RenderError:
        raise
    except (OSError, ValidationError, ValueError) as exc:
        raise PromotionError(f"promotion transaction failed: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _is_mutable(relative: str) -> bool:
    return relative in _MUTABLE_PATHS


def _allowed_addition(
    relative: str, *, snapshot: str, release_path: str | None
) -> bool:
    if _is_mutable(relative):
        return True
    if release_path is not None and relative == release_path:
        return True
    parts = Path(relative).parts
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
    release_path: str | None,
    changed: set[str],
) -> None:
    existing = _tree_entries(repo / name, name)
    candidate = _tree_entries(stage / name, f"staged {name}")
    for relative in sorted(set(existing) | set(candidate)):
        full = f"{name}/{relative}"
        left = existing.get(relative)
        right = candidate.get(relative)
        if relative not in candidate:
            raise PromotionError(f"transaction removed immutable path: {full}")
        if relative not in existing:
            if right is None:
                continue
            if not _allowed_addition(
                full, snapshot=snapshot, release_path=release_path
            ):
                raise PromotionError(f"transaction added unexpected path: {full}")
            changed.add(full)
            continue
        if left == right:
            continue
        if _is_mutable(full):
            if right is None:
                raise PromotionError(f"mutable path became a directory: {full}")
            changed.add(full)
            continue
        raise PromotionError(f"immutable path changed: {full}")


def _compare_transaction(
    repo: Path,
    stage: Path,
    *,
    snapshot: str,
    release_path: str | None,
) -> tuple[str, ...]:
    changed: set[str] = set()
    for name in _SOURCE_ROOTS:
        _compare_family(
            repo,
            stage,
            name,
            snapshot=snapshot,
            release_path=release_path,
            changed=changed,
        )
    return tuple(sorted(changed))


def _ensure_parent(
    repo: Path, path: Path, created_dirs: list[Path] | None = None
) -> None:
    try:
        path.parent.relative_to(repo)
    except ValueError as exc:
        raise PromotionError(f"transaction path escapes repository: {path}") from exc
    try:
        created = storage.ensure_directory_chain(path.parent)
    except ValueError as exc:
        raise PromotionError(f"transaction parent {exc}") from exc
    except OSError as exc:
        raise PromotionError(
            f"could not create transaction parent {path.parent}: {exc}"
        ) from exc
    if created_dirs is not None:
        created_dirs.extend(created)


def _replace_file(
    repo: Path,
    path: Path,
    data: bytes,
    label: str,
    created_dirs: list[Path] | None = None,
) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PromotionError(f"{label} is not a regular file: {path}")
    _ensure_parent(repo, path, created_dirs)
    try:
        storage.replace_atomic(path, data)
    except OSError as exc:
        raise PromotionError(
            f"could not atomically write {label} {path}: {exc}"
        ) from exc


def _create_file(
    repo: Path,
    path: Path,
    data: bytes,
    label: str,
    created_dirs: list[Path] | None = None,
) -> bool:
    """Create an immutable file without replacing a raced destination."""

    _ensure_parent(repo, path, created_dirs)
    try:
        return storage.create_atomic(path, data)
    except ValueError as exc:
        raise PromotionError(str(exc)) from exc
    except OSError as exc:
        raise PromotionError(
            f"could not install immutable {label} {path}: {exc}"
        ) from exc


def _prior_file(path: Path) -> bytes | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PromotionError(f"moving document is not a regular file: {path}")
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PromotionError(f"could not read moving document {path}: {exc}") from exc


def _restore_file(repo: Path, path: Path, previous: bytes | None, label: str) -> None:
    if previous is None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise PromotionError(f"cannot remove moving document: {path}")
            try:
                path.unlink()
            except OSError as exc:
                raise PromotionError(
                    f"could not restore {label} {path}: {exc}"
                ) from exc
        return
    _replace_file(repo, path, previous, label)


def _rollback_created(
    created_files: list[tuple[Path, bytes]], created_dirs: list[Path]
) -> None:
    """Remove only files and directories created by this invocation."""

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
            # A raced/preexisting child keeps the directory in place.
            continue


def _install_transaction(
    repo: Path,
    stage: Path,
    changed_paths: Iterable[str],
) -> None:
    changed = tuple(sorted(changed_paths))
    immutable = tuple(path for path in changed if not _is_mutable(path))
    mutable = tuple(path for path in changed if _is_mutable(path))
    created_files: list[tuple[Path, bytes]] = []
    created_dirs: list[Path] = []

    try:
        for relative in immutable:
            source = stage / relative
            target = repo / relative
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise PromotionError(
                    f"could not read staged path {relative}: {exc}"
                ) from exc
            if _create_file(repo, target, data, relative, created_dirs):
                created_files.append((target, data))
    except (OSError, PromotionError) as exc:
        _rollback_created(created_files, created_dirs)
        if isinstance(exc, PromotionError):
            raise
        raise PromotionError(
            f"could not install immutable path {relative}: {exc}"
        ) from exc

    if not mutable:
        return

    moving = tuple(
        relative
        for relative in ("public/registry.json", "public/v1/snapshots.json")
        if relative in mutable
    )
    pointer = "pointers/latest.json"
    prior: dict[str, bytes | None] = {}
    prior_complete = False
    try:
        prior = {relative: _prior_file(repo / relative) for relative in _MUTABLE_PATHS}
        prior_complete = True
        # Publish the moving pair before the internal pointer.  If the pointer
        # write fails, the pair is restored, leaving the old selection intact.
        for relative in moving:
            _replace_file(
                repo,
                repo / relative,
                (stage / relative).read_bytes(),
                relative,
                created_dirs,
            )
        if pointer in mutable:
            _replace_file(
                repo,
                repo / pointer,
                (stage / pointer).read_bytes(),
                pointer,
                created_dirs,
            )
    except (OSError, PromotionError) as exc:
        try:
            if prior_complete:
                for relative, previous in prior.items():
                    _restore_file(repo, repo / relative, previous, relative)
        except PromotionError as rollback_error:
            _rollback_created(created_files, created_dirs)
            raise PromotionError(
                f"publication failed and rollback failed: {rollback_error}"
            ) from exc
        _rollback_created(created_files, created_dirs)
        raise
