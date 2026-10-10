"""Incremental native composition with complete-input and output tracking."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import storage
from ..gather import load_repositories
from ..render.snapshots import load_releases
from .fetch import fetch_package
from .inputs import input_path, load_yanked, metadata_inventory
from .inspect import inspect_package
from .metadata import (
    RELEASE_OPTIONS,
    StoredPackage,
    check_stored,
    compose_metadata,
    extract_metadata,
)
from .models import LockEntry, pool_path, version_channel

# Bump when extraction or aggregate formatting changes.
RENDER_VERSION = 2


def _command(args: list[str], cwd: Path) -> bytes:
    result = subprocess.run(args, cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"native tool failed: {args[0]}: {result.stderr.decode()}")
    return result.stdout


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_packages(repo: Path) -> dict[str, StoredPackage]:
    path = input_path(repo, "package-metadata.json")
    raw = json.loads(path.read_bytes()) if path.exists() else {}
    if not isinstance(raw, dict) or any(
        not re.fullmatch(r"[0-9a-f]{64}", k) for k in raw
    ):
        raise ValueError("invalid retained native package map")
    return {
        digest: StoredPackage.model_validate(value) for digest, value in raw.items()
    }


def _identity_key(package: StoredPackage) -> tuple[str, str, str, str]:
    identity = package.identity
    # Debian 0: versions compare equal even though metadata preserves spelling.
    version = (
        identity.version.removeprefix("0:")
        if identity.format == "deb"
        else identity.version
    )
    return identity.format, identity.name, version, identity.arch


def render_native(
    repo: Path, cache: Path, out: Path, base_url: str = "https://registry.gelstable.com"
) -> bool:
    """Reuse digest-bound package records; rebuild aggregates only when needed."""
    from ..validation.native import validate_native

    repositories = load_repositories(repo)
    config = json.loads((repo / "sources/github.json").read_bytes())
    names = config.get("native_package_names", {})
    lock_path = input_path(repo, "packages.lock.json")
    metadata_path = input_path(repo, "package-metadata.json")
    state_path = input_path(repo, "render-state.json")
    yanked = load_yanked(repo)
    stored = _load_packages(repo)
    state = json.loads(state_path.read_bytes()) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("invalid native render state")
    if state_path.exists() and not metadata_path.exists():
        raise ValueError("retained native metadata missing; restore it from Git")
    if metadata_path.exists() and state.get("packages_sha256") not in (
        None,
        _hash(metadata_path.read_bytes()),
    ):
        raise ValueError(
            "retained native metadata changed; restore package-metadata.json "
            "from the trusted Git generation"
        )
    key = repo / "public/keys/gelstable.asc"
    if any(path.is_symlink() for path in (key, *key.parents)):
        raise ValueError("symlink in native public key")
    key_digest = _hash(key.read_bytes())
    # Check output links even if other changes would otherwise rebuild the tree.
    inventory = metadata_inventory(out)
    entries: list[LockEntry] = []
    identities: dict[tuple[str, str, str, str], str] = {}
    for digest, retained_package in stored.items():
        check_stored(digest, retained_package, live=False)
        previous = identities.setdefault(_identity_key(retained_package), digest)
        if previous != digest:
            raise ValueError("conflicting retained native package identity")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".native-render-", dir=cache) as directory:
        scratch = Path(directory)
        for record in load_releases(repo):
            if not record.native or not record.native.packages:
                continue
            repository = record.source.repository
            if repository not in repositories or repository not in names:
                raise ValueError(f"native repository is not allowed: {repository}")
            for package in record.native.packages:
                digest = package.sha256
                fmt = package.url.rsplit(".", 1)[1]
                saved = stored.get(digest)
                live = digest not in yanked
                needs_bytes = saved is None or (
                    live
                    and (
                        (fmt == "rpm" and saved.verified_key != key_digest)
                        or (fmt == "deb" and saved.deb is None)
                        or (fmt == "rpm" and not saved.rpm)
                    )
                )
                if needs_bytes:
                    blob = fetch_package(package.url, digest, package.size, cache)
                    inspected = inspect_package(
                        blob, fmt, names[repository], key if live else None
                    )
                    if saved is not None and inspected != saved.identity:
                        raise ValueError(
                            "native identity differs from retained metadata"
                        )
                    saved = StoredPackage(identity=inspected, size=package.size)
                    if live:
                        extract_metadata(blob, saved, scratch / digest, _command)
                        if fmt == "rpm":
                            saved.verified_key = key_digest
                    stored[digest] = saved
                assert saved is not None
                if saved.size != package.size or saved.identity.format != fmt:
                    raise ValueError("native record differs from retained metadata")
                if not any(
                    re.fullmatch(pattern, saved.identity.name)
                    for pattern in names[repository]
                ):
                    raise ValueError(
                        f"native package name is not allowed: {saved.identity.name}"
                    )
                previous = identities.setdefault(_identity_key(saved), digest)
                if previous != digest:
                    raise ValueError(
                        f"conflicting native package identity: {_identity_key(saved)}"
                    )
                check_stored(digest, saved, live=live)
                if not live:
                    continue
                entry = LockEntry.model_validate(
                    {
                        **saved.identity.model_dump(),
                        "channel": version_channel(saved.identity.version),
                        "sha256": digest,
                        "size": package.size,
                        "path": pool_path(record, package),
                    }
                )
                if entry not in entries:
                    entries.append(entry)
        entries.sort(
            key=lambda e: (e.channel, e.format, e.name, e.version, e.arch, e.path)
        )
        lock = _json([entry.model_dump() for entry in entries])
        retained = _json(
            {digest: value.model_dump() for digest, value in stored.items()}
        )
        inputs = _hash(
            _json(
                {
                    "version": RENDER_VERSION,
                    "base_url": base_url.rstrip("/"),
                    "key": key_digest,
                    "names": names,
                    "options": RELEASE_OPTIONS,
                    "lock": _hash(lock),
                    "packages": _hash(retained),
                }
            )
        )
        unchanged = (
            lock_path.exists()
            and lock_path.read_bytes() == lock
            and state.get("inputs") == inputs
            and state.get("outputs") == inventory
        )
        if unchanged and out == repo / "public":
            try:
                validate_native(repo)
            except (ValueError, OSError):
                unchanged = False
        if unchanged:
            return False
        metadata = scratch / "metadata"
        compose_metadata(entries, stored, metadata, base_url, _command)
        next_state = _json(
            {
                "inputs": inputs,
                "outputs": metadata_inventory(metadata),
                "packages_sha256": _hash(retained),
                "base_url": base_url.rstrip("/"),
            }
        )
        _install_metadata(
            metadata,
            out,
            lock_path,
            lock,
            {
                metadata_path: retained,
                state_path: next_state,
            },
        )
    return True


def _install_metadata(
    metadata: Path,
    out: Path,
    lock_path: Path,
    lock: bytes,
    native_files: dict[Path, bytes] | None = None,
) -> None:
    """Stage on each destination filesystem, then install with rollback.

    Renames are atomic per path. This restores the old generation on ordinary
    filesystem errors; it is not a crash-atomic swap of the complete generation.
    """
    native_files = {lock_path: lock, **(native_files or {})}
    for path in (out, out / "apt", out / "rpm", *native_files):
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError(f"symlink in native output: {path}")
    for path in (out / "apt", out / "rpm"):
        if path.exists() and not path.is_dir():
            raise ValueError(f"native output is not a directory: {path}")
    created_dirs: list[Path] = []
    stages: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    cleanup = True
    try:
        created_dirs.extend(storage.ensure_directory_chain(out))
        created_dirs.extend(storage.ensure_directory_chain(lock_path.parent))
        trees = Path(tempfile.mkdtemp(prefix=".native-install-", dir=out))
        stages.append(trees)
        lock_stage = Path(
            tempfile.mkdtemp(prefix=".native-install-", dir=lock_path.parent)
        )
        stages.append(lock_stage)
        for fmt in ("apt", "rpm"):
            shutil.copytree(metadata / fmt, trees / fmt)
        for path, data in native_files.items():
            (lock_stage / path.name).write_bytes(data)
        replacements = [
            (trees / "apt", out / "apt"),
            (trees / "rpm", out / "rpm"),
            *((lock_stage / path.name, path) for path in native_files),
        ]
        try:
            for staged, destination in replacements:
                backup = staged.with_name(staged.name + ".backup")
                if destination.exists():
                    os.replace(destination, backup)
                    moved.append((destination, backup))
                os.replace(staged, destination)
                installed.append(destination)
        except OSError:
            try:
                for destination in reversed(installed):
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                for destination, backup in reversed(moved):
                    os.replace(backup, destination)
            except OSError as exc:
                # Keep backups for operator recovery if the filesystem also
                # refuses rollback. Never delete the only remaining old bytes.
                cleanup = False
                raise OSError(
                    f"native installation rollback failed; backups retained in {stages}"
                ) from exc
            raise
    finally:
        if cleanup:
            for stage in stages:
                shutil.rmtree(stage)
            for directory in reversed(created_dirs):
                if directory.exists() and not any(directory.iterdir()):
                    directory.rmdir()
