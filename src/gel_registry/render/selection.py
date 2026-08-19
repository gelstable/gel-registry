"""Pointer-driven publication of the two moving public documents."""

from __future__ import annotations

import re
from pathlib import Path

from ..contracts import Pointer, RootIndex, RootManifest, SnapshotListing
from ..digest import canonical_json
from . import files
from .errors import RenderError
from .snapshots import load_pinned_snapshot

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")


def select_snapshot(repo: Path) -> None:
    """Materialize moving roots from the snapshot selected by the pointer."""

    repo = Path(repo)
    pointer_path = repo / "pointers" / "latest.json"
    pointer = files.read_model(pointer_path, Pointer, "latest pointer")
    selected = pointer.snapshot
    selected_manifest, _ = load_pinned_snapshot(repo, selected)

    public = repo / "public"
    files.directory(public, "public root")
    snapshots = _snapshot_directories(public)
    for snapshot in snapshots:
        load_pinned_snapshot(repo, snapshot)
    if selected not in snapshots:
        raise RenderError(f"selected snapshot is not listed: {selected}")

    moving = RootManifest(
        indexes=tuple(
            RootIndex(
                channel=reference.channel,
                platform=reference.platform,
                url=f"s/{selected}/{reference.url}",
            )
            for reference in selected_manifest.indexes
        )
    )
    listing = SnapshotListing(latest=selected, snapshots=snapshots)
    _publish_moving_pair(
        public / "registry.json",
        canonical_json(moving),
        public / "v1" / "snapshots.json",
        canonical_json(listing),
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


def _publish_moving_pair(
    root_path: Path,
    root_bytes: bytes,
    listing_path: Path,
    listing_bytes: bytes,
) -> None:
    """Publish the two moving documents as a failure-safe pair.

    There is no single portable filesystem primitive that atomically switches
    two independent files.  Both targets are therefore preflighted before any
    mutation, and if the second replacement fails the first is restored from
    its captured prior bytes.
    """

    _preflight_target(root_path, "moving root")
    _preflight_target(listing_path, "snapshot listing")
    prior_root = _prior_bytes(root_path)
    prior_listing = _prior_bytes(listing_path)
    try:
        _atomic_replace(root_path, root_bytes, "moving root")
        _atomic_replace(listing_path, listing_bytes, "snapshot listing")
    except RenderError as exc:
        try:
            _restore_target(root_path, prior_root, "moving root rollback")
            _restore_target(listing_path, prior_listing, "snapshot listing rollback")
        except RenderError as rollback_error:
            raise RenderError(
                "moving-document publication failed and rollback failed: "
                f"{rollback_error}"
            ) from exc
        raise
