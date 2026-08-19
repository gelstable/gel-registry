"""Immutable snapshot construction and verification.

A snapshot is content-addressed: :func:`build_snapshot` installs a tree only
when it does not exist yet, and otherwise verifies that the committed tree
matches the freshly composed one byte for byte.
"""

from __future__ import annotations

import errno
import re
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

from ..constants import CHANNELS, LEGACY_PLATFORMS
from ..contracts import PackageIndex, ReleaseRecord, RootManifest
from ..digest import canonical_json, snapshot_id
from ..storage import rename_noreplace
from . import files
from .compose import compose_indexes, root_for_indexes
from .errors import RenderError

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_INDEX_NAMES = {
    f"{channel}-{platform}.json": (channel, platform)
    for channel in CHANNELS
    for platform in LEGACY_PLATFORMS
}


def build_snapshot(repo: Path) -> str:
    """Compose inputs and install or verify their immutable snapshot tree."""

    repo = Path(repo)
    bootstrap = load_bootstrap(repo)
    releases = load_releases(repo)
    index_bytes = compose_indexes(bootstrap, releases)
    snapshot_indexes = {
        PurePosixPath("index") / f"{channel}-{platform}.json": data
        for (channel, platform), data in index_bytes.items()
    }
    snapshot = snapshot_id(snapshot_indexes)
    root = root_for_indexes(index_bytes, "index/")

    public = repo / "public"
    files.ensure_directory_chain(public, "public root")
    files.directory(public, "public root")
    stage_parent = public / ".snapshot-staging"
    files.ensure_directory_chain(stage_parent, "snapshot staging root")
    files.directory(stage_parent, "snapshot staging root")
    destination = public / "s" / snapshot
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise RenderError(
            f"snapshot destination is not a regular directory: {destination}"
        )
    files.ensure_directory_chain(destination.parent, f"snapshot {snapshot} parent")
    stage = Path(tempfile.mkdtemp(prefix="snapshot-", dir=str(stage_parent)))
    staged_snapshot = stage / snapshot
    try:
        index_dir = staged_snapshot / "index"
        index_dir.mkdir(parents=True)
        for relative, data in sorted(
            snapshot_indexes.items(), key=lambda item: str(item[0])
        ):
            (staged_snapshot / relative).write_bytes(data)
        (staged_snapshot / "registry.json").write_bytes(canonical_json(root))
        _install_directory(staged_snapshot, destination, f"snapshot {snapshot}")
    except (OSError, ValueError) as exc:
        raise RenderError(f"could not build snapshot {snapshot}: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        with suppress(OSError):
            stage_parent.rmdir()
    return snapshot


def load_pinned_snapshot(
    repo: Path, snapshot: str
) -> tuple[RootManifest, dict[str, bytes]]:
    if _SNAPSHOT_ID.fullmatch(snapshot) is None:
        raise RenderError(f"invalid snapshot ID: {snapshot!r}")
    root = repo / "public" / "s" / snapshot
    files.directory(root, f"snapshot {snapshot}")
    root_path = root / "registry.json"
    root_bytes = files.read_bytes(root_path, "pinned root")
    manifest = files.read_model(root_path, RootManifest, "pinned root")
    if root_bytes != canonical_json(manifest):
        raise RenderError(f"pinned root is not canonical: {root_path}")
    index_dir = root / "index"
    files.directory(index_dir, f"snapshot {snapshot} index directory")

    index_bytes: dict[str, bytes] = {}
    expected_names: set[str] = set()
    for reference in manifest.indexes:
        filename = f"{reference.channel}-{reference.platform}.json"
        expected_url = f"index/{filename}"
        if reference.url != expected_url:
            raise RenderError(
                f"pinned root has invalid index reference: {reference.url}"
            )
        if filename in expected_names:
            raise RenderError(f"pinned root contains duplicate index: {filename}")
        expected_names.add(filename)
        path = index_dir / filename
        data = files.read_bytes(path, "pinned index")
        index = files.read_model(path, PackageIndex, "pinned index")
        if data != canonical_json(index):
            raise RenderError(f"pinned index is not canonical: {path}")
        index_bytes[filename] = data

    actual_names: set[str] = set()
    try:
        paths = tuple(index_dir.iterdir())
    except OSError as exc:
        raise RenderError(f"could not inspect pinned index directory: {exc}") from exc
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise RenderError(
                f"pinned index directory contains unexpected path: {path}"
            )
        actual_names.add(path.name)
    if actual_names != expected_names:
        raise RenderError(
            f"pinned root/index mismatch: expected={sorted(expected_names)} "
            f"actual={sorted(actual_names)}"
        )
    calculated = snapshot_id(
        {
            PurePosixPath("index") / filename: data
            for filename, data in index_bytes.items()
        }
    )
    if calculated != snapshot:
        raise RenderError(
            f"pinned snapshot ID mismatch: directory={snapshot} calculated={calculated}"
        )
    expected_tree = {"registry.json": root_bytes, "index": None}
    expected_tree.update({f"index/{name}": data for name, data in index_bytes.items()})
    actual_tree = files.read_tree(root)
    if actual_tree != expected_tree:
        raise RenderError(f"pinned snapshot contains unexpected paths: {root}")
    return manifest, index_bytes


def load_bootstrap(repo: Path) -> dict[tuple[str, str], PackageIndex]:
    root = repo / "bootstrap"
    indexes: dict[tuple[str, str], PackageIndex] = {}
    for path in files.json_files(root, "bootstrap"):
        name = path.name
        key = _INDEX_NAMES.get(name)
        if key is None:
            raise RenderError(f"unknown bootstrap index filename: {path}")
        if key in indexes:
            raise RenderError(f"duplicate bootstrap index: {name}")
        indexes[key] = files.read_model(path, PackageIndex, "bootstrap index")
    return indexes


def load_releases(repo: Path) -> tuple[ReleaseRecord, ...]:
    releases: list[ReleaseRecord] = []
    for path in files.json_files(repo / "releases", "releases"):
        releases.append(files.read_model(path, ReleaseRecord, "release record"))
    return tuple(releases)


def _verify_tree(candidate: Path, existing: Path, label: str) -> None:
    candidate_entries = files.read_tree(candidate)
    existing_entries = files.read_tree(existing)
    if candidate_entries == existing_entries:
        return
    added = sorted(set(candidate_entries) - set(existing_entries))
    removed = sorted(set(existing_entries) - set(candidate_entries))
    changed = sorted(
        path
        for path in set(candidate_entries) & set(existing_entries)
        if candidate_entries[path] != existing_entries[path]
    )
    details: list[str] = []
    if added:
        details.append("added=" + ",".join(added))
    if removed:
        details.append("removed=" + ",".join(removed))
    if changed:
        details.append("changed=" + ",".join(changed))
    raise RenderError(f"immutable {label} mismatch: " + "; ".join(details))


def _install_directory(stage: Path, destination: Path, label: str) -> None:
    if destination.exists() or destination.is_symlink():
        files.directory(destination, label)
        _verify_tree(stage, destination, label)
        return
    try:
        files.ensure_directory_chain(destination.parent, f"{label} parent")
        rename_noreplace(stage, destination)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise RenderError(f"could not install immutable {label}: {exc}") from exc
        files.directory(destination, label)
        _verify_tree(stage, destination, label)
