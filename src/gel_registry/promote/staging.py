"""Construction of the temporary tree a transaction is rendered into.

Nothing here touches the caller's repository: staging copies the verified
source and publication state, installs the new release record and pointer,
and checks that the resulting snapshot is nonempty.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from ..contracts import PackageIndex, Pointer, ReleaseRecord
from ..digest import canonical_json
from ..storage import read_tree
from .result import PromotionError

_SOURCE_ROOTS = ("bootstrap", "releases", "pointers", "public")
type _TreeEntry = bytes | None


def _tree_entries(root: Path, label: str) -> dict[str, _TreeEntry]:
    """Read a regular-file tree while rejecting symlinks and special files."""

    if not root.exists():
        if root.is_symlink():
            raise PromotionError(f"{label} is a symlink: {root}")
        return {}
    try:
        return read_tree(root)
    except ValueError as exc:
        raise PromotionError(f"{label} {exc}") from exc
    except OSError as exc:
        raise PromotionError(f"could not inspect {label} {root}: {exc}") from exc


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
