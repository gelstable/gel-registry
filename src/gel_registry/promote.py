"""Failure-safe transactions for bootstrap publication and release promotion.

The renderer intentionally exposes two independent phases: building an
immutable snapshot and selecting that snapshot through the internal pointer.
Promotion composes those phases in a temporary repository tree.  Nothing in
the caller's tree is changed until both phases, plus all immutable-byte checks,
have succeeded.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .digest import canonical_json
from .normalize import _rename_noreplace
from .render import RenderError, build_snapshot, select_snapshot
from .schema import PackageIndex, Pointer, ReleaseRecord


class PromotionError(RuntimeError):
    """Raised when a publication transaction cannot be committed safely."""


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """The deterministic result of a staged publication transaction."""

    snapshot: str
    branch: str
    changed_paths: tuple[str, ...]

    @property
    def snapshot_id(self) -> str:
        """Compatibility alias for callers that name snapshots explicitly."""

        return self.snapshot

    @property
    def branch_name(self) -> str:
        """Compatibility alias for callers that name branches explicitly."""

        return self.branch

    @property
    def paths(self) -> tuple[str, ...]:
        """Compatibility alias for the git-style changed-path report."""

        return self.changed_paths

    @property
    def noop(self) -> bool:
        """Whether the transaction already matched every resulting byte."""

        return not self.changed_paths


_MUTABLE_PATHS = frozenset(
    {
        "pointers/latest.json",
        "public/registry.json",
        "public/v1/snapshots.json",
    }
)
_SOURCE_ROOTS = ("bootstrap", "releases", "pointers", "public")
type _TreeEntry = bytes | None


def _repo_path(repo: Path, relative: str) -> Path:
    """Return a repository-relative path without accepting path escapes."""

    path = repo / relative
    if path.parent != repo and repo not in path.parents:
        raise PromotionError(f"transaction path escapes repository: {relative}")
    return path


def _tree_entries(root: Path, label: str) -> dict[str, _TreeEntry]:
    """Read a regular-file tree while rejecting symlinks and special files."""

    if not root.exists():
        if root.is_symlink():
            raise PromotionError(f"{label} is a symlink: {root}")
        return {}
    if root.is_symlink() or not root.is_dir():
        raise PromotionError(f"{label} is not a directory: {root}")
    entries: dict[str, _TreeEntry] = {}
    try:
        paths = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise PromotionError(f"{label} contains symlink: {relative}")
            if path.is_dir():
                entries[relative] = None
            elif path.is_file():
                entries[relative] = path.read_bytes()
            else:
                raise PromotionError(f"{label} contains unsupported path: {relative}")
    except PromotionError:
        raise
    except OSError as exc:
        raise PromotionError(f"could not inspect {label} {root}: {exc}") from exc
    return entries


def _copy_tree(source: Path, destination: Path, label: str) -> None:
    """Copy a verified regular-file tree into a temporary repository."""

    entries = _tree_entries(source, label)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for relative, value in sorted(
            entries.items(), key=lambda item: (item[0].count("/"), item[0])
        ):
            path = destination / relative
            if value is None:
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
    except OSError as exc:
        raise PromotionError(f"could not stage {label}: {exc}") from exc


def _validate_repo(repo: Path) -> None:
    if repo.is_symlink() or not repo.is_dir():
        raise PromotionError(f"repository is not a directory: {repo}")
    for name in _SOURCE_ROOTS:
        source = repo / name
        _tree_entries(source, name)


def _stage_repository(repo: Path) -> Path:
    """Copy all source and publication state needed by the renderer."""

    try:
        stage = Path(
            tempfile.mkdtemp(prefix=f".{repo.name}.promotion-", dir=str(repo.parent))
        )
    except OSError as exc:
        raise PromotionError(f"could not create promotion staging tree: {exc}") from exc
    try:
        for name in _SOURCE_ROOTS:
            source = repo / name
            if source.exists() or source.is_symlink():
                _copy_tree(source, stage / name, name)
        return stage
    except PromotionError:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _bootstrap_indexes(root: Path) -> tuple[Path, ...]:
    if not root.exists() or root.is_symlink() or not root.is_dir():
        raise PromotionError(f"bootstrap indexes are missing: {root}")
    try:
        paths = tuple(
            path
            for path in sorted(root.rglob("*.json"), key=lambda item: item.as_posix())
            if path.is_file()
        )
    except OSError as exc:
        raise PromotionError(f"could not inspect bootstrap indexes: {exc}") from exc
    if not paths:
        raise PromotionError(f"bootstrap indexes are missing: {root}")
    return paths


def _canonical_pointer(path: Path) -> bytes | None:
    """Validate an existing pointer before allowing a transaction to replace it."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PromotionError(f"latest pointer is not a regular file: {path}")
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        pointer = Pointer.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        raise PromotionError(f"invalid latest pointer {path}: {exc}") from exc
    expected = canonical_json(pointer)
    if raw != expected:
        raise PromotionError(f"latest pointer is not canonical: {path}")
    return raw


def _record_identity(record: ReleaseRecord) -> bytes:
    """Serialize a release while excluding the per-promotion bookkeeping time."""

    return canonical_json(
        record.model_copy(update={"promoted_at": datetime(1970, 1, 1, tzinfo=UTC)})
    )


def _validated_record(record: ReleaseRecord) -> ReleaseRecord:
    try:
        return ReleaseRecord.model_validate(record)
    except (AttributeError, ValidationError, TypeError, ValueError) as exc:
        raise PromotionError(f"invalid release record: {exc}") from exc


def _prepare_release(stage: Path, record: ReleaseRecord) -> None:
    """Install a new record in staging or verify an existing immutable record."""

    path = stage / "releases" / "gel-cli" / f"{record.version}.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PromotionError(f"release record is not a regular file: {path}")

    if path.exists():
        try:
            raw = path.read_bytes()
            existing = ReleaseRecord.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise PromotionError(
                f"invalid existing release record {path}: {exc}"
            ) from exc
        if raw != canonical_json(existing):
            raise PromotionError(f"immutable release record is not canonical: {path}")
        if _record_identity(existing) != _record_identity(record):
            raise PromotionError(f"immutable release record mismatch: {path}")
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Verification already records the one UTC promotion timestamp on the
        # immutable ReleaseRecord.  Preserve that exact record byte-for-byte;
        # rendering intentionally excludes this bookkeeping field.
        path.write_bytes(canonical_json(record))
    except OSError as exc:
        raise PromotionError(f"could not stage release record {path}: {exc}") from exc


def _write_pointer(stage: Path, snapshot: str) -> None:
    path = stage / "pointers" / "latest.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PromotionError(f"latest pointer is not a regular file: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json(Pointer(snapshot=snapshot)))
    except OSError as exc:
        raise PromotionError(f"could not stage latest pointer {path}: {exc}") from exc


def _ensure_nonempty_snapshot(
    stage: Path,
    snapshot: str,
    *,
    bootstrap_indexes: tuple[Path, ...],
    require_bootstrap_packages: bool,
) -> None:
    index_root = stage / "public" / "s" / snapshot / "index"
    if not index_root.exists() or index_root.is_symlink() or not index_root.is_dir():
        raise PromotionError(f"snapshot is empty: {snapshot}")
    try:
        indexes = tuple(
            path
            for path in sorted(index_root.glob("*.json"), key=lambda item: item.name)
            if path.is_file() and not path.is_symlink()
        )
    except OSError as exc:
        raise PromotionError(f"could not inspect snapshot {snapshot}: {exc}") from exc
    if not indexes:
        raise PromotionError(f"snapshot is empty: {snapshot}")
    if not require_bootstrap_packages:
        return
    package_count = 0
    try:
        for bootstrap_path in bootstrap_indexes:
            source = PackageIndex.model_validate_json(bootstrap_path.read_bytes())
            package_count += len(source.packages)
    except (OSError, ValidationError, ValueError) as exc:
        raise PromotionError(
            f"could not validate bootstrap-backed snapshot {snapshot}: {exc}"
        ) from exc
    if package_count == 0:
        raise PromotionError(f"snapshot contains no bootstrap packages: {snapshot}")


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
        relative_parent = path.parent.relative_to(repo)
    except ValueError as exc:
        raise PromotionError(f"transaction path escapes repository: {path}") from exc
    current = repo
    for part in relative_parent.parts:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise PromotionError(f"transaction parent is not a directory: {current}")
        try:
            current.mkdir(exist_ok=False)
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise PromotionError(
                    f"transaction parent is not a directory: {current}"
                ) from None
        else:
            if created_dirs is not None:
                created_dirs.append(current)


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
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise PromotionError(
            f"could not atomically write {label} {path}: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _create_file(
    repo: Path,
    path: Path,
    data: bytes,
    label: str,
    created_dirs: list[Path] | None = None,
) -> bool:
    """Create an immutable file without replacing a raced destination."""

    _ensure_parent(repo, path, created_dirs)
    if path.is_symlink() or path.exists():
        if path.is_file() and path.read_bytes() == data:
            return False
        raise PromotionError(f"immutable path collision: {path}")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _rename_noreplace(temporary, path)
        temporary = None
        return True
    except OSError as exc:
        if exc.errno == 17 and path.is_file() and path.read_bytes() == data:
            return False
        raise PromotionError(
            f"could not install immutable {label} {path}: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    except PromotionError:
        _rollback_created(created_files, created_dirs)
        raise

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


def publish_bootstrap(repo: Path) -> PromotionResult:
    """Build, select, and stage the first nonempty bootstrap-backed snapshot."""

    return _transaction(
        Path(repo),
        release=None,
        branch="publish/legacy-2026-08-bootstrap",
    )


def promote_release(repo: Path, record: ReleaseRecord) -> PromotionResult:
    """Publish one verified stable CLI release over the full bootstrap."""

    validated = _validated_record(record)
    return _transaction(
        Path(repo),
        release=validated,
        branch=f"promote/gel-cli-{validated.version}",
    )


__all__ = [
    "PromotionError",
    "PromotionResult",
    "promote_release",
    "publish_bootstrap",
]
