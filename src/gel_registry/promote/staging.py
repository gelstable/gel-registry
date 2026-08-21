"""Construction of the temporary tree used by candidate promotion."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .. import publication
from ..contracts import (
    BlockedManifest,
    BlockedRelease,
    PackageIndex,
    Pointer,
    ReleaseRecord,
)
from ..digest import canonical_json
from ..render import RenderError, load_pinned_snapshot
from .result import PromotionError

_SOURCE_ROOTS = (*publication._SOURCE_ROOTS, "promotion")
_PROMOTION_PATHS = frozenset({"blocked.json"})
type _TreeEntry = bytes | None


def _tree_entries(root: Path, label: str) -> dict[str, _TreeEntry]:
    try:
        return publication._tree_entries(root, label)
    except publication.PublicationError as exc:
        raise PromotionError(str(exc)) from exc


def _validate_repo(repo: Path) -> None:
    try:
        publication._validate_repository(repo, source_roots=_SOURCE_ROOTS)
    except publication.PublicationError as exc:
        raise PromotionError(str(exc)) from exc
    entries = _tree_entries(repo / "promotion", "promotion")
    unexpected = sorted(set(entries) - _PROMOTION_PATHS)
    if unexpected:
        raise PromotionError(
            "promotion contains unexpected paths: " + ", ".join(unexpected)
        )


def _stage_repository(repo: Path) -> Path:
    try:
        return publication._stage_repository(
            repo, source_roots=_SOURCE_ROOTS, prefix="promotion"
        )
    except publication.PublicationError as exc:
        raise PromotionError(str(exc)) from exc


def _bootstrap_indexes(root: Path) -> tuple[Path, ...]:
    try:
        return publication._validate_bootstrap(root)
    except publication.PublicationError as exc:
        raise PromotionError(str(exc)) from exc


def _canonical_pointer(path: Path) -> bytes | None:
    try:
        return publication._validate_pointer(path)
    except publication.PublicationError as exc:
        raise PromotionError(str(exc)) from exc


def _record_identity(record: ReleaseRecord) -> bytes:
    """Serialize a release without its per-promotion bookkeeping time."""

    return canonical_json(
        record.model_copy(update={"promoted_at": datetime(1970, 1, 1, tzinfo=UTC)})
    )


def _validated_record(record: ReleaseRecord) -> ReleaseRecord:
    try:
        return ReleaseRecord.model_validate(record)
    except (AttributeError, ValidationError, TypeError, ValueError) as exc:
        raise PromotionError(f"invalid release record: {exc}") from exc


def _safe_path_component(value: str, label: str) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or len(path.parts) != 1
        or path.parts[0] in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise PromotionError(f"safe {label} path component required: {value!r}")
    return value


def _prepare_release(stage: Path, record: ReleaseRecord) -> None:
    """Install a new record or verify an existing immutable record."""

    product = _safe_path_component(record.product, "release product")
    version = _safe_path_component(record.version, "release version")
    path = stage / "releases" / product / f"{version}.json"
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
        path.write_bytes(canonical_json(record))
    except OSError as exc:
        raise PromotionError(f"could not stage release record {path}: {exc}") from exc


def _prepare_blocked(stage: Path, blocked: tuple[BlockedRelease, ...]) -> None:
    """Install the current deterministic blocked manifest, or remove it."""

    path = stage / "promotion" / "blocked.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PromotionError(f"blocked manifest is not a regular file: {path}")
    if not blocked:
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                raise PromotionError(
                    f"could not remove blocked manifest {path}: {exc}"
                ) from exc
        return
    try:
        manifest = BlockedManifest(entries=blocked)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json(manifest))
    except (OSError, TypeError, ValidationError, ValueError) as exc:
        raise PromotionError(f"could not stage blocked manifest {path}: {exc}") from exc


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
    # The pinned snapshot is a root manifest referencing blobs in the shared
    # store, so emptiness is a property of the manifest, not of a directory.
    try:
        _, index_bytes = load_pinned_snapshot(stage, snapshot)
    except (RenderError, OSError, ValueError) as exc:
        raise PromotionError(f"could not inspect snapshot {snapshot}: {exc}") from exc
    if not index_bytes:
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


__all__ = [
    "_SOURCE_ROOTS",
    "_bootstrap_indexes",
    "_canonical_pointer",
    "_ensure_nonempty_snapshot",
    "_prepare_blocked",
    "_prepare_release",
    "_stage_repository",
    "_tree_entries",
    "_validated_record",
    "_validate_repo",
    "_write_pointer",
]
