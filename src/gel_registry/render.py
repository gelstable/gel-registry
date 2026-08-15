"""Pure composition of registry inputs into immutable snapshots.

Rendering deliberately has two phases.  :func:`build_snapshot` only consumes
the append-only bootstrap and release inputs and installs (or verifies) a
content-addressed tree.  :func:`select_snapshot` only consumes the internal
pointer and materializes the two moving public documents from an existing
pinned tree.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from functools import cmp_to_key
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ValidationError

from .constants import CHANNELS, CLI_PLATFORMS, LEGACY_PLATFORMS
from .digest import canonical_json, snapshot_id
from .normalize import _rename_noreplace
from .schema import (
    CaptureManifest,
    InstallRef,
    PackageEntry,
    PackageIndex,
    Pointer,
    ReleaseRecord,
    RootIndex,
    RootManifest,
    SnapshotListing,
    Verification,
)

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_INDEX_NAMES = {
    f"{channel}-{platform}.json": (channel, platform)
    for channel in CHANNELS
    for platform in LEGACY_PLATFORMS
}


class RenderError(RuntimeError):
    """Raised when local registry inputs or rendered trees are invalid."""


class ContestedIdentityError(RenderError):
    """Raised when one package identity has different serialized entries."""

    def __init__(self, identity: tuple[str, str, str]) -> None:
        self.identity = identity
        basename, version, slot = identity
        super().__init__(
            "contested package identity "
            f"(basename={basename!r}, version={version!r}, slot={slot!r})"
        )


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RenderError(f"{label} is not a regular file: {path}")


def _directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RenderError(f"{label} is not a directory: {path}")


def _ensure_directory_chain(path: Path, label: str) -> None:
    """Create a directory and reject symlinked/non-directory ancestors.

    ``Path.mkdir(parents=True)`` follows an existing symlink in the chain.
    Renderer inputs can be supplied by a working tree or a generated staging
    tree, so every ancestor is checked before a filesystem write is attempted.
    """

    missing: list[Path] = []
    current = path
    while True:
        if current.is_symlink():
            raise RenderError(f"{label} contains symlinked ancestor: {current}")
        if current.exists():
            if not current.is_dir():
                raise RenderError(f"{label} ancestor is not a directory: {current}")
            break
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise RenderError(
                    f"{label} ancestor is not a regular directory: {directory}"
                ) from None
        except OSError as exc:
            raise RenderError(
                f"could not create {label} directory {directory}: {exc}"
            ) from exc


def _read_model[ModelT: BaseModel](
    path: Path, model: type[ModelT], label: str
) -> ModelT:
    _regular_file(path, label)
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise RenderError(f"invalid {label} {path}: {exc}") from exc


def _read_bytes(path: Path, label: str) -> bytes:
    _regular_file(path, label)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RenderError(f"could not read {label} {path}: {exc}") from exc


def _json_files(root: Path, label: str) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    _directory(root, label)
    files: list[Path] = []
    try:
        paths = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
        for path in paths:
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                raise RenderError(f"{label} contains symlink: {relative}")
            if path.is_file() and path.suffix == ".json":
                files.append(path)
    except OSError as exc:
        raise RenderError(f"could not inspect {label} {root}: {exc}") from exc
    return tuple(files)


def _load_bootstrap(repo: Path) -> dict[tuple[str, str], PackageIndex]:
    root = repo / "bootstrap"
    indexes: dict[tuple[str, str], PackageIndex] = {}
    for path in _json_files(root, "bootstrap"):
        name = path.name
        key = _INDEX_NAMES.get(name)
        if key is None:
            raise RenderError(f"unknown bootstrap index filename: {path}")
        if key in indexes:
            raise RenderError(f"duplicate bootstrap index: {name}")
        indexes[key] = _read_model(path, PackageIndex, "bootstrap index")
    return indexes


def _load_releases(repo: Path) -> tuple[ReleaseRecord, ...]:
    releases: list[ReleaseRecord] = []
    for path in _json_files(repo / "releases", "releases"):
        releases.append(_read_model(path, ReleaseRecord, "release record"))
    return tuple(releases)


def _installref_sort_key(reference: InstallRef) -> tuple[int, str, bytes]:
    encoding = reference.encoding or ""
    order = {"identity": 0, "zstd": 1}.get(encoding, 2)
    return order, encoding, canonical_json(reference)


def _canonical_package(package: PackageEntry) -> PackageEntry:
    references = tuple(sorted(package.installrefs, key=_installref_sort_key))
    if references == package.installrefs:
        return package
    return package.model_copy(update={"installrefs": references})


def _package_identity(package: PackageEntry) -> tuple[str, str, str]:
    return package.basename, package.version, package.slot


def _version_details(version: str) -> dict[str, Any]:
    core_and_metadata = version.split("+", 1)
    core_and_prerelease = core_and_metadata[0].split("-", 1)
    core = core_and_prerelease[0]
    major, minor, patch = (int(part) for part in core.split("."))
    prerelease: list[int | str] = []
    if len(core_and_prerelease) == 2:
        for identifier in core_and_prerelease[1].split("."):
            prerelease.append(int(identifier) if identifier.isdigit() else identifier)
    return {
        "major": major,
        "minor": minor,
        "patch": patch,
        "prerelease": prerelease,
        "metadata": {},
    }


def _release_package(record: ReleaseRecord, platform: str) -> PackageEntry:
    artifacts = tuple(
        artifact for artifact in record.artifacts if artifact.platform == platform
    )
    if {artifact.encoding for artifact in artifacts} != {"identity", "zstd"}:
        raise RenderError(
            f"release {record.version} does not have identity and zstd "
            f"artifacts for {platform}"
        )
    references = tuple(
        InstallRef(
            ref=artifact.url,
            type=artifact.media_type,
            encoding=artifact.encoding,
            verification=Verification(
                size=artifact.size,
                sha256=artifact.sha256,
                blake2b=artifact.blake2b,
            ),
        )
        for artifact in sorted(
            artifacts,
            key=lambda item: (0 if item.encoding == "identity" else 1, item.url),
        )
    )
    identity = next(
        reference for reference in references if reference.encoding == "identity"
    )
    return PackageEntry(
        basename=record.product,
        name=record.product,
        version=record.version,
        version_details=_version_details(record.version),
        version_key=record.version,
        revision=str(record.source.release_id),
        # ``promoted_at`` is review bookkeeping, not package provenance.  The
        # release tag is stable source metadata and keeps rendered bytes
        # independent of when the record was promoted.
        build_date=record.source.release_tag,
        architecture=platform.split("-", 1)[0],
        slot="",
        tags={},
        installref=identity.ref,
        installrefs=references,
    )


def _compare_packages(left: PackageEntry, right: PackageEntry) -> int:
    if left.basename != right.basename:
        return -1 if left.basename < right.basename else 1
    try:
        left_version = Version(left.version)
        right_version = Version(right.version)
    except InvalidVersion:
        if left.version != right.version:
            return -1 if left.version < right.version else 1
    else:
        if left_version != right_version:
            return -1 if left_version < right_version else 1
    if left.version != right.version:
        return -1 if left.version < right.version else 1
    left_bytes = canonical_json(left)
    right_bytes = canonical_json(right)
    if left_bytes == right_bytes:
        return 0
    return -1 if left_bytes < right_bytes else 1


def _add_package(
    packages: dict[tuple[str, str, str], tuple[bytes, PackageEntry]],
    package: PackageEntry,
) -> None:
    package = _canonical_package(package)
    identity = _package_identity(package)
    serialized = canonical_json(package)
    existing = packages.get(identity)
    if existing is None:
        packages[identity] = serialized, package
        return
    if existing[0] != serialized:
        raise ContestedIdentityError(identity)


def _compose_indexes(
    bootstrap: Mapping[tuple[str, str], PackageIndex],
    releases: Iterable[ReleaseRecord],
) -> dict[tuple[str, str], bytes]:
    packages: dict[
        tuple[str, str], dict[tuple[str, str, str], tuple[bytes, PackageEntry]]
    ] = {}
    for key, index in bootstrap.items():
        bucket = packages.setdefault(key, {})
        for package in index.packages:
            _add_package(bucket, package)

    for record in releases:
        for platform in CLI_PLATFORMS:
            key = ("stable", platform)
            bucket = packages.setdefault(key, {})
            _add_package(bucket, _release_package(record, platform))

    rendered: dict[tuple[str, str], bytes] = {}
    for key in sorted(packages):
        values = sorted(
            (entry[1] for entry in packages[key].values()),
            key=cmp_to_key(_compare_packages),
        )
        rendered[key] = canonical_json(PackageIndex(packages=tuple(values)))
    return rendered


def _root_for_indexes(keys: Iterable[tuple[str, str]], prefix: str) -> RootManifest:
    indexes = tuple(
        RootIndex(
            channel=channel,
            platform=platform,
            url=f"{prefix}{channel}-{platform}.json",
        )
        for channel, platform in keys
    )
    return RootManifest(indexes=indexes)


def _tree_entries(root: Path) -> dict[str, bytes | None]:
    _directory(root, "tree")
    entries: dict[str, bytes | None] = {}
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise RenderError(f"tree contains symlink: {relative}")
            if path.is_dir():
                entries[relative] = None
            elif path.is_file():
                entries[relative] = path.read_bytes()
            else:
                raise RenderError(f"tree contains unsupported path: {relative}")
    except OSError as exc:
        raise RenderError(f"could not inspect tree {root}: {exc}") from exc
    return entries


def _verify_tree(candidate: Path, existing: Path, label: str) -> None:
    candidate_entries = _tree_entries(candidate)
    existing_entries = _tree_entries(existing)
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
        _directory(destination, label)
        _verify_tree(stage, destination, label)
        return
    try:
        _ensure_directory_chain(destination.parent, f"{label} parent")
        _rename_noreplace(stage, destination)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise RenderError(f"could not install immutable {label}: {exc}") from exc
        _directory(destination, label)
        _verify_tree(stage, destination, label)


def _atomic_replace_impl(path: Path, data: bytes, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RenderError(f"{label} is not a regular file: {path}")
    parent = path.parent
    _ensure_directory_chain(parent, f"{label} parent")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise RenderError(f"could not atomically write {label} {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_replace(path: Path, data: bytes, label: str) -> None:
    """Replace one regular file atomically.

    This small wrapper is kept separate from the implementation so selection
    can restore a prior moving pair even when a test or caller injects a
    failure into one publication call.
    """

    _atomic_replace_impl(path, data, label)


def _preflight_target(path: Path, label: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RenderError(f"{label} is not a regular file: {path}")
    parent = path.parent
    _ensure_directory_chain(parent, f"{label} parent")


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
    _atomic_replace_impl(path, previous, label)


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


def build_snapshot(repo: Path) -> str:
    """Compose inputs and install or verify their immutable snapshot tree."""

    repo = Path(repo)
    bootstrap = _load_bootstrap(repo)
    releases = _load_releases(repo)
    index_bytes = _compose_indexes(bootstrap, releases)
    snapshot_indexes = {
        PurePosixPath("index") / f"{channel}-{platform}.json": data
        for (channel, platform), data in index_bytes.items()
    }
    snapshot = snapshot_id(snapshot_indexes)
    root = _root_for_indexes(index_bytes, "index/")

    public = repo / "public"
    _ensure_directory_chain(public, "public root")
    _directory(public, "public root")
    stage_parent = public / ".snapshot-staging"
    _ensure_directory_chain(stage_parent, "snapshot staging root")
    _directory(stage_parent, "snapshot staging root")
    destination = public / "s" / snapshot
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise RenderError(
            f"snapshot destination is not a regular directory: {destination}"
        )
    _ensure_directory_chain(destination.parent, f"snapshot {snapshot} parent")
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


def _load_pinned_snapshot(
    repo: Path, snapshot: str
) -> tuple[RootManifest, dict[str, bytes]]:
    if _SNAPSHOT_ID.fullmatch(snapshot) is None:
        raise RenderError(f"invalid snapshot ID: {snapshot!r}")
    root = repo / "public" / "s" / snapshot
    _directory(root, f"snapshot {snapshot}")
    root_path = root / "registry.json"
    root_bytes = _read_bytes(root_path, "pinned root")
    manifest = _read_model(root_path, RootManifest, "pinned root")
    if root_bytes != canonical_json(manifest):
        raise RenderError(f"pinned root is not canonical: {root_path}")
    index_dir = root / "index"
    _directory(index_dir, f"snapshot {snapshot} index directory")

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
        data = _read_bytes(path, "pinned index")
        index = _read_model(path, PackageIndex, "pinned index")
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
    actual_tree = _tree_entries(root)
    if actual_tree != expected_tree:
        raise RenderError(f"pinned snapshot contains unexpected paths: {root}")
    return manifest, index_bytes


def _snapshot_directories(public: Path) -> tuple[str, ...]:
    root = public / "s"
    _directory(root, "snapshot root")
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


def select_snapshot(repo: Path) -> None:
    """Materialize moving roots from the snapshot selected by the pointer."""

    repo = Path(repo)
    pointer_path = repo / "pointers" / "latest.json"
    pointer = _read_model(pointer_path, Pointer, "latest pointer")
    selected = pointer.snapshot
    selected_manifest, _ = _load_pinned_snapshot(repo, selected)

    public = repo / "public"
    _directory(public, "public root")
    snapshots = _snapshot_directories(public)
    for snapshot in snapshots:
        _load_pinned_snapshot(repo, snapshot)
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


def _support_documents() -> tuple[tuple[str, bytes], ...]:
    models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("capture.json", CaptureManifest),
        ("package-index.json", PackageIndex),
        ("release-record.json", ReleaseRecord),
        ("pointer.json", Pointer),
        ("root.json", RootManifest),
        ("snapshot-listing.json", SnapshotListing),
    )
    return tuple(
        (name, canonical_json(model.model_json_schema())) for name, model in models
    )


def render_schemas(repo: Path) -> None:
    """Install canonical public JSON Schemas and the static health response."""

    repo = Path(repo)
    public = repo / "public"
    _ensure_directory_chain(public, "public root")
    _directory(public, "public root")
    schema_dir = public / "v1" / "schema"
    _ensure_directory_chain(schema_dir, "schema root")
    _directory(schema_dir, "schema root")

    documents = list(_support_documents())
    documents.append(("../healthz", b"ok\n"))
    targets = tuple(
        (schema_dir / name if name != "../healthz" else public / "healthz", data)
        for name, data in documents
    )
    for path, data in targets:
        if path.is_symlink():
            raise RenderError(f"support document is a symlink: {path}")
        if path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise RenderError(f"immutable support document mismatch: {path}")
    for path, data in targets:
        if not path.exists():
            _atomic_replace(path, data, "support document")


__all__ = [
    "ContestedIdentityError",
    "RenderError",
    "build_snapshot",
    "render_schemas",
    "select_snapshot",
]
