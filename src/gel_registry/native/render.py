"""Compose native lock entries and render standard APT/RPM metadata."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

from .. import storage
from ..gather import load_repositories
from ..render.snapshots import load_releases
from .fetch import fetch_package
from .inputs import input_path, load_yanked
from .inspect import inspect_package


class LockEntry(TypedDict):
    channel: str
    format: str
    name: str
    version: str
    arch: str
    sha256: str
    size: int
    path: str


def _command(args: list[str], cwd: Path) -> bytes:
    result = subprocess.run(args, cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"native tool failed: {args[0]}: {result.stderr.decode()}")
    return result.stdout


def render_native(
    repo: Path, cache: Path, out: Path, base_url: str = "https://registry.gelstable.com"
) -> bool:
    """Render metadata and lock; return False without writes for a matching lock.

    Pool paths in the lock are relative to the format root, e.g.
    ``pool/gel/pkg-7/asset.deb``. Metadata alone is installed in out.
    """
    repositories = load_repositories(repo)
    config = json.loads((repo / "sources/github.json").read_bytes())
    names = config.get("native_package_names", {})
    lock_path = input_path(repo, "packages.lock.json")
    yanked = load_yanked(repo)
    entries: list[LockEntry] = []
    identities: dict[tuple[str, str, str, str], str] = {}
    blobs: dict[str, Path] = {}
    for record in load_releases(repo):
        if not record.native or not record.native.packages:
            continue
        repository = record.source.repository
        if repository not in repositories or repository not in names:
            raise ValueError(f"native repository is not allowed: {repository}")
        for package in record.native.packages:
            blob = fetch_package(package.url, package.sha256, package.size, cache)
            asset = urlsplit(package.url).path.rsplit("/", 1)[1]
            fmt = asset.rsplit(".", 1)[1]
            _, name, epoch, version, release, arch = inspect_package(
                blob,
                fmt,
                names[repository],
                None
                if package.sha256 in yanked
                else repo / "public/keys/gelstable.asc",
            )
            full_version = (
                (f"{epoch}:" if epoch != "0" else "")
                + version
                + (f"-{release}" if release else "")
            )
            identity = (fmt, name, full_version, arch)
            previous = identities.setdefault(identity, package.sha256)
            if previous != package.sha256:
                raise ValueError(f"conflicting native package identity: {identity}")
            # Withdrawn bytes still reserve their immutable identity, even after
            # their signing key leaves current trust. They never enter metadata.
            if package.sha256 in yanked:
                continue
            blobs[package.sha256] = blob
            entry: LockEntry = {
                "channel": record.native.channel,
                "format": fmt,
                "name": name,
                "version": full_version,
                "arch": arch,
                "sha256": package.sha256,
                "size": package.size,
                "path": f"pool/{repository.split('/')[1]}/{record.source.tag}/{asset}",
            }
            if entry not in entries:
                entries.append(entry)
    entries.sort(
        key=lambda entry: (
            entry["channel"],
            entry["format"],
            entry["name"],
            entry["version"],
            entry["arch"],
            entry["path"],
        )
    )
    lock = (json.dumps(entries, sort_keys=True, indent=2) + "\n").encode()
    if lock_path.exists() and lock_path.read_bytes() == lock:
        return False
    cache.mkdir(parents=True, exist_ok=True)
    # Build everything before installing any metadata or lock.
    with tempfile.TemporaryDirectory(prefix=".native-render-", dir=cache) as directory:
        scratch = Path(directory)
        metadata = scratch / "metadata"
        for channel in ("stable", "testing"):
            apt = metadata / "apt/dists" / channel
            for fmt, arches in (
                ("deb", ("amd64", "arm64")),
                ("rpm", ("x86_64", "aarch64")),
            ):
                for arch in arches:
                    pool = scratch / fmt / channel / arch
                    (pool / "pool").mkdir(parents=True)
                    for entry in entries:
                        if (entry["format"], entry["channel"], entry["arch"]) == (
                            fmt,
                            channel,
                            arch,
                        ):
                            target = pool / entry["path"]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            os.link(blobs[entry["sha256"]], target)
                    if fmt == "deb":
                        binary = apt / "main" / f"binary-{arch}"
                        binary.mkdir(parents=True)
                        packages = _command(
                            ["apt-ftparchive", "packages", "pool"], pool
                        )
                        (binary / "Packages").write_bytes(packages)
                        with (binary / "Packages.gz").open("wb") as compressed:
                            subprocess.run(
                                ["gzip", "-9n"],
                                input=packages,
                                stdout=compressed,
                                check=True,
                            )
                    else:
                        _command(
                            [
                                "createrepo_c",
                                "--no-database",
                                "--baseurl",
                                base_url.rstrip("/") + "/rpm/",
                                str(pool),
                            ],
                            pool,
                        )
                        destination = metadata / "rpm" / channel / arch / "repodata"
                        shutil.copytree(pool / "repodata", destination)
            options = {
                "Origin": "Gelstable",
                "Label": "Gelstable",
                "Suite": channel,
                "Codename": channel,
                "Architectures": "amd64 arm64",
                "Components": "main",
            }
            args = ["apt-ftparchive"]
            for option, value in options.items():
                args.extend(["-o", f"APT::FTPArchive::Release::{option}={value}"])
            (apt / "Release").write_bytes(_command([*args, "release", "."], apt))
        _install_metadata(metadata, out, lock_path, lock)
    return True


def _install_metadata(metadata: Path, out: Path, lock_path: Path, lock: bytes) -> None:
    """Stage on each destination filesystem, then install with rollback.

    Renames are atomic per path. This restores the old generation on ordinary
    filesystem errors; it is not a crash-atomic swap of all three paths.
    """
    for path in (out, out / "apt", out / "rpm", lock_path):
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
        (lock_stage / lock_path.name).write_bytes(lock)
        replacements = [
            (trees / "apt", out / "apt"),
            (trees / "rpm", out / "rpm"),
            (lock_stage / lock_path.name, lock_path),
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
