"""Checks tying the pointer, pinned snapshots and moving documents together."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from ..contracts import Pointer, RootManifest, SnapshotListing
from ..digest import blob_id
from ..render import RenderError, load_pinned_snapshot
from .report import Collector
from .support import canonical_error, display_path, read_file

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_BLOB_FILENAME = re.compile(r"^([0-9a-f]{32})\.json$")
_BLOB_URL = re.compile(r"^\.\./\.\./i/([0-9a-f]{32})\.json$")


def _check_blob_store(
    repo: Path,
    snapshots: list[str],
    collector: Collector,
) -> None:
    """Check the shared content-addressed index store against every manifest.

    ``load_pinned_snapshot`` verifies the blobs one snapshot references, but no
    single snapshot has authority over a store every snapshot shares.  This is
    where the store as a whole is held to its invariants: nothing referenced is
    missing, nothing present is unreferenced, every name is a content address,
    and every file's bytes hash to the name it is filed under.

    The orphan rule exists to prove the append-only rules hold, not to enable
    collection: a blob is never deleted, because a pinned snapshot that is no
    longer selected still references it.

    Manifests are read here directly rather than taken from the set that
    ``load_pinned_snapshot`` accepted: a snapshot whose blob is missing does not
    load, and reusing that set would make a missing blob erase the very
    reference that proves it is missing.
    """

    check = "publication.blob_store"
    collector.begin(check)
    store = repo / "public" / "i"

    referenced: dict[str, str] = {}
    for snapshot in snapshots:
        path = repo / "public" / "s" / snapshot / "registry.json"
        try:
            manifest = RootManifest.model_validate_json(read_file(path))
        except (OSError, PydanticValidationError, ValueError):
            # An unreadable manifest is already reported by snapshot.internals.
            continue
        for item in manifest.indexes:
            matched = _BLOB_URL.fullmatch(item.url)
            if matched is not None:
                referenced.setdefault(matched.group(1), snapshot)

    if not store.exists():
        if referenced:
            collector.add(check, display_path(repo, store), "blob store is missing")
        return
    if store.is_symlink() or not store.is_dir():
        collector.add(check, display_path(repo, store), "blob store is not a directory")
        return

    try:
        children = sorted(store.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        collector.add(check, display_path(repo, store), str(exc))
        return

    present: set[str] = set()
    for child in children:
        if child.is_symlink():
            collector.add(check, display_path(repo, child), "blob path is a symlink")
            continue
        if not child.is_file():
            collector.add(
                check, display_path(repo, child), "unexpected path beneath public/i"
            )
            continue
        matched = _BLOB_FILENAME.fullmatch(child.name)
        if matched is None:
            collector.add(check, display_path(repo, child), "invalid blob filename")
            continue
        identity = matched.group(1)
        present.add(identity)
        try:
            data = read_file(child)
        except (OSError, ValueError) as exc:
            collector.add(check, display_path(repo, child), str(exc))
            continue
        if blob_id(data) != identity:
            collector.add(
                check,
                display_path(repo, child),
                f"blob content address is {blob_id(data)}, not its filename",
            )
        if identity not in referenced:
            collector.add(
                check,
                display_path(repo, child),
                "blob is not referenced by any snapshot manifest",
            )

    for identity, snapshot in sorted(referenced.items()):
        if identity not in present:
            collector.add(
                check,
                display_path(repo, store / f"{identity}.json"),
                f"blob referenced by snapshot {snapshot} is missing",
            )


def check_pointer_and_snapshots(repo: Path, collector: Collector) -> None:
    pointer_check = "pointer.integrity"
    listing_check = "snapshot.listing"
    internals_check = "snapshot.internals"
    moving_check = "moving.root"
    collector.begin(pointer_check)
    collector.begin(listing_check)
    collector.begin(internals_check)
    collector.begin(moving_check)
    pointer_path = repo / "pointers" / "latest.json"
    pointer: Pointer | None = None
    try:
        pointer_raw = read_file(pointer_path)
    except (OSError, ValueError) as exc:
        collector.add(pointer_check, display_path(repo, pointer_path), str(exc))
    else:
        try:
            pointer = Pointer.model_validate_json(pointer_raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(pointer_check, display_path(repo, pointer_path), str(exc))
        else:
            canonical_issue = canonical_error(pointer_raw, pointer)
            if canonical_issue is not None:
                collector.add(
                    pointer_check, display_path(repo, pointer_path), canonical_issue
                )

    public = repo / "public"
    snapshots_root = public / "s"
    snapshots: list[str] = []
    if not snapshots_root.exists():
        collector.add(
            internals_check,
            display_path(repo, snapshots_root),
            "snapshot root is missing",
        )
    elif snapshots_root.is_symlink() or not snapshots_root.is_dir():
        collector.add(
            internals_check,
            display_path(repo, snapshots_root),
            "snapshot root is not a directory",
        )
    else:
        try:
            children = sorted(snapshots_root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            collector.add(internals_check, display_path(repo, snapshots_root), str(exc))
            children = []
        for child in children:
            if child.is_symlink():
                collector.add(
                    internals_check,
                    display_path(repo, child),
                    "snapshot path is a symlink",
                )
                continue
            if not child.is_dir():
                collector.add(
                    internals_check,
                    display_path(repo, child),
                    "unexpected path beneath public/s",
                )
                continue
            if _SNAPSHOT_ID.fullmatch(child.name) is None:
                collector.add(
                    internals_check,
                    display_path(repo, child),
                    "invalid snapshot directory name",
                )
                continue
            snapshots.append(child.name)

    manifests: dict[str, RootManifest] = {}
    indexes: dict[str, dict[str, bytes]] = {}
    for snapshot in snapshots:
        path = snapshots_root / snapshot
        try:
            manifest, snapshot_indexes = load_pinned_snapshot(repo, snapshot)
        except (RenderError, OSError, ValueError) as exc:
            collector.add(internals_check, display_path(repo, path), str(exc))
        else:
            manifests[snapshot] = manifest
            indexes[snapshot] = snapshot_indexes

    _check_blob_store(repo, snapshots, collector)

    listing_path = public / "v1" / "snapshots.json"
    listing: SnapshotListing | None = None
    try:
        listing_raw = read_file(listing_path)
    except (OSError, ValueError) as exc:
        collector.add(listing_check, display_path(repo, listing_path), str(exc))
    else:
        try:
            listing = SnapshotListing.model_validate_json(listing_raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(listing_check, display_path(repo, listing_path), str(exc))
        else:
            canonical_issue = canonical_error(listing_raw, listing)
            if canonical_issue is not None:
                collector.add(
                    listing_check, display_path(repo, listing_path), canonical_issue
                )
            if set(listing.snapshots) != set(snapshots):
                collector.add(
                    listing_check,
                    display_path(repo, listing_path),
                    f"snapshot listing does not account for committed snapshots "
                    f"(expected={snapshots} observed={list(listing.snapshots)})",
                )
            if pointer is not None and listing.latest != pointer.snapshot:
                collector.add(
                    listing_check,
                    display_path(repo, listing_path),
                    "latest "
                    f"{listing.latest} does not match pointer {pointer.snapshot}",
                )

    moving_path = public / "registry.json"
    moving: RootManifest | None = None
    try:
        moving_raw = read_file(moving_path)
    except (OSError, ValueError) as exc:
        collector.add(moving_check, display_path(repo, moving_path), str(exc))
    else:
        try:
            moving = RootManifest.model_validate_json(moving_raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(moving_check, display_path(repo, moving_path), str(exc))
        else:
            canonical_issue = canonical_error(moving_raw, moving)
            if canonical_issue is not None:
                collector.add(
                    moving_check, display_path(repo, moving_path), canonical_issue
                )

    if pointer is not None:
        selected = pointer.snapshot
        if selected not in snapshots:
            collector.add(
                pointer_check,
                display_path(repo, pointer_path),
                f"selected snapshot is not committed: {selected}",
            )
        selected_manifest = manifests.get(selected)
        if selected_manifest is not None and moving is not None:
            # Recomputed from the selected snapshot's index bytes rather than
            # derived by stripping the pinned URLs' ``../../``, for symmetry
            # with how the renderer builds the moving root.
            selected_indexes = indexes[selected]
            expected = tuple(
                (
                    item.channel,
                    item.platform,
                    "i/"
                    + blob_id(selected_indexes[f"{item.channel}-{item.platform}.json"])
                    + ".json",
                )
                for item in selected_manifest.indexes
            )
            observed = tuple(
                (item.channel, item.platform, item.url) for item in moving.indexes
            )
            if observed != expected:
                collector.add(
                    moving_check,
                    display_path(repo, moving_path),
                    "moving root references the wrong snapshot "
                    f"(expected={expected} observed={observed})",
                )
