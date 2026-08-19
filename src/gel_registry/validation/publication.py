"""Checks tying the pointer, pinned snapshots and moving documents together."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from ..contracts import Pointer, RootManifest, SnapshotListing
from ..render import RenderError, load_pinned_snapshot
from .report import Collector
from .support import canonical_error, display_path, read_file

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")


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
    for snapshot in snapshots:
        path = snapshots_root / snapshot
        try:
            manifest, _indexes = load_pinned_snapshot(repo, snapshot)
        except (RenderError, OSError, ValueError) as exc:
            collector.add(internals_check, display_path(repo, path), str(exc))
        else:
            manifests[snapshot] = manifest

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
            expected = tuple(
                (item.channel, item.platform, f"s/{selected}/{item.url}")
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
