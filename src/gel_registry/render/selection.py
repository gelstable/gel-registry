"""Pointer-driven publication of the moving documents."""

from __future__ import annotations

import re
from pathlib import Path

from ..contracts import Pointer, SnapshotListing
from ..digest import blob_id, canonical_json
from . import files
from .compose import root_for_indexes
from .errors import RenderError
from .hosting import hosting_config
from .snapshots import MOVING_BLOB_PREFIX, load_pinned_snapshot

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")


def select_snapshot(repo: Path) -> None:
    """Materialize moving roots from the snapshot selected by the pointer."""

    repo = Path(repo)
    pointer_path = repo / "pointers" / "latest.json"
    pointer = files.read_model(pointer_path, Pointer, "latest pointer")
    selected = pointer.snapshot
    selected_manifest, selected_indexes = load_pinned_snapshot(repo, selected)

    public = repo / "public"
    files.directory(public, "public root")
    snapshots = _snapshot_directories(public)
    for snapshot in snapshots:
        load_pinned_snapshot(repo, snapshot)
    if selected not in snapshots:
        raise RenderError(f"selected snapshot is not listed: {selected}")

    # The moving root points at the same blobs as the pinned root, but it lives
    # one directory level up, so its URLs are recomputed from the index bytes
    # rather than derived by string surgery on the pinned URLs.  Both documents
    # are document-relative; see ``compose.root_for_indexes``.
    moving = root_for_indexes(
        {
            (reference.channel, reference.platform): blob_id(
                selected_indexes[f"{reference.channel}-{reference.platform}.json"]
            )
            for reference in selected_manifest.indexes
        },
        MOVING_BLOB_PREFIX,
    )
    listing = SnapshotListing(latest=selected, snapshots=snapshots)
    _publish_moving_documents(
        (public / "registry.json", canonical_json(moving), "moving root"),
        (public / "v1" / "snapshots.json", canonical_json(listing), "snapshot listing"),
        (repo / "vercel.json", hosting_config(moving), "hosting configuration"),
    )


def _snapshot_directories(public: Path) -> tuple[str, ...]:
    root = public / "s"
    files.directory(root, "snapshot root")
    snapshots: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise RenderError(f"could not inspect snapshot root: {exc}") from exc
    for child in children:
        if child.is_symlink():
            raise RenderError(f"snapshot root contains symlink: {child.name}")
        if not child.is_dir():
            continue
        if _SNAPSHOT_ID.fullmatch(child.name) is None:
            continue
        snapshots.append(child.name)
    return tuple(snapshots)


def _atomic_replace(path: Path, data: bytes, label: str) -> None:
    """Replace one regular file atomically.

    This small wrapper is kept separate from the implementation so selection
    can restore a prior moving pair even when a test or caller injects a
    failure into one publication call.
    """

    files.atomic_replace(path, data, label)


def _preflight_target(path: Path, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RenderError(f"{label} is not a regular file: {path}")
    parent = path.parent
    files.ensure_directory_chain(parent, f"{label} parent")


def _prior_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RenderError(
            f"could not read prior moving document {path}: {exc}"
        ) from exc


def _restore_target(path: Path, previous: bytes | None, label: str) -> None:
    if previous is None:
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise RenderError(f"cannot remove non-file moving document: {path}")
            try:
                path.unlink()
            except OSError as exc:
                raise RenderError(
                    f"could not restore moving document {path}: {exc}"
                ) from exc
        return
    if path.exists() and path.is_file() and path.read_bytes() == previous:
        return
    files.atomic_replace(path, previous, label)


def _publish_moving_documents(*targets: tuple[Path, bytes, str]) -> None:
    """Publish the moving documents as one failure-safe group.

    There is no single portable filesystem primitive that atomically switches
    several independent files.  Every target is therefore preflighted before
    any mutation, and if a later replacement fails the earlier ones are
    restored from their captured prior bytes.
    """

    for path, _, label in targets:
        _preflight_target(path, label)
    prior = [_prior_bytes(path) for path, _, _ in targets]
    try:
        for path, data, label in targets:
            _atomic_replace(path, data, label)
    except RenderError as exc:
        try:
            for (path, _, label), previous in zip(targets, prior, strict=True):
                _restore_target(path, previous, f"{label} rollback")
        except RenderError as rollback_error:
            raise RenderError(
                "moving-document publication failed and rollback failed: "
                f"{rollback_error}"
            ) from exc
        raise
