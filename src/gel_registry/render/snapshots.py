"""Immutable snapshot construction and verification.

Index bytes are stored once, content-addressed, under ``public/i/<blob-id>.json``.
A snapshot is not a directory of copies but a single manifest of pointers into
that shared store: ``public/s/<snapshot-id>/registry.json``.  Unchanged indexes
are therefore shared by every snapshot that references them -- in the checkout
and in the deploy artifact, not merely in git's object store.

Both blobs and snapshot manifests are written with
:func:`gel_registry.storage.create_atomic`, which creates without replacing and
treats a destination already holding identical bytes as unchanged, so an
interrupted publication replays cleanly and a differing collision is an error.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from ..constants import CHANNELS, LEGACY_PLATFORMS
from ..contracts import PackageIndex, ReleaseRecord, RootManifest
from ..digest import blob_id, canonical_json, snapshot_id
from . import files
from .compose import compose_indexes, root_for_indexes
from .errors import RenderError

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_BLOB_ID = re.compile(r"^[0-9a-f]{32}$")
#: The only URL shape a pinned manifest may carry.  Document-relative, and
#: pointedly not root-absolute: see :func:`compose.root_for_indexes`.
_PINNED_URL = re.compile(r"^\.\./\.\./i/([0-9a-f]{32})\.json$")
#: Where a pinned manifest points, relative to ``public/s/<snapshot>/``.
PINNED_BLOB_PREFIX = "../../i/"
#: Where the moving root points, relative to ``public/``.
MOVING_BLOB_PREFIX = "i/"
_INDEX_NAMES = {
    f"{channel}-{platform}.json": (channel, platform)
    for channel in CHANNELS
    for platform in LEGACY_PLATFORMS
}


def blob_store(repo: Path) -> Path:
    """Return the shared content-addressed index store."""

    return Path(repo) / "public" / "i"


def build_snapshot(repo: Path) -> str:
    """Compose inputs and install or verify their immutable snapshot."""

    repo = Path(repo)
    bootstrap = load_bootstrap(repo)
    releases = load_releases(repo)
    index_bytes = compose_indexes(bootstrap, releases)
    # ``index/`` is a logical namespace for identity only; nothing is stored
    # there.  Feeding ``snapshot_id`` a storage path would change every
    # published snapshot ID.  See ``digest._validated_index_path``.
    snapshot = snapshot_id(
        {
            PurePosixPath("index") / f"{channel}-{platform}.json": data
            for (channel, platform), data in index_bytes.items()
        }
    )

    public = repo / "public"
    files.ensure_directory_chain(public, "public root")
    files.directory(public, "public root")
    # Preflight the snapshot destination before a single blob is written, so a
    # repository that cannot receive the manifest is not left holding blobs.
    destination = public / "s" / snapshot
    files.ensure_directory_chain(destination, f"snapshot {snapshot}")
    files.directory(destination, f"snapshot {snapshot}")
    store = blob_store(repo)
    files.ensure_directory_chain(store, "index blob store")
    files.directory(store, "index blob store")

    blobs: dict[tuple[str, str], str] = {}
    for key in sorted(index_bytes):
        data = index_bytes[key]
        identity = blob_id(data)
        blobs[key] = identity
        files.create_immutable(
            store / f"{identity}.json", data, f"index blob {identity}"
        )

    files.create_immutable(
        destination / "registry.json",
        canonical_json(root_for_indexes(blobs, PINNED_BLOB_PREFIX)),
        f"snapshot {snapshot} root",
    )
    return snapshot


def load_pinned_snapshot(
    repo: Path, snapshot: str
) -> tuple[RootManifest, dict[str, bytes]]:
    """Read and fully verify one pinned snapshot and the blobs it references.

    ``index_bytes`` is keyed by the logical ``<channel>-<platform>.json`` name
    rather than by blob ID, because that is the identity callers reason about.

    This deliberately does not check ``public/i/`` for stray files: the blob
    store is shared and no single snapshot has authority over it.  That check
    lives in ``validation.publication`` as ``publication.blob_store``.
    """

    if _SNAPSHOT_ID.fullmatch(snapshot) is None:
        raise RenderError(f"invalid snapshot ID: {snapshot!r}")
    root = repo / "public" / "s" / snapshot
    files.directory(root, f"snapshot {snapshot}")
    root_path = root / "registry.json"
    root_bytes = files.read_bytes(root_path, "pinned root")
    manifest = files.read_model(root_path, RootManifest, "pinned root")
    if root_bytes != canonical_json(manifest):
        raise RenderError(f"pinned root is not canonical: {root_path}")
    actual_tree = files.read_tree(root)
    if actual_tree != {"registry.json": root_bytes}:
        raise RenderError(f"pinned snapshot contains unexpected paths: {root}")

    store = blob_store(repo)
    index_bytes: dict[str, bytes] = {}
    for reference in manifest.indexes:
        matched = _PINNED_URL.fullmatch(reference.ref)
        if matched is None:
            raise RenderError(
                f"pinned root has invalid index reference: {reference.ref}"
            )
        identity = matched.group(1)
        filename = f"{reference.channel}-{reference.platform}.json"
        if filename in index_bytes:
            raise RenderError(f"pinned root contains duplicate index: {filename}")
        path = store / f"{identity}.json"
        data = files.read_bytes(path, "index blob")
        index = files.read_model(path, PackageIndex, "index blob")
        if data != canonical_json(index):
            raise RenderError(f"index blob is not canonical: {path}")
        if blob_id(data) != identity:
            raise RenderError(f"index blob does not match its content address: {path}")
        index_bytes[filename] = data

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
