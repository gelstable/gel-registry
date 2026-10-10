"""Compose deterministic native repositories from retained package records."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..gather import load_repositories
from ..render.snapshots import load_releases
from .fetch import fetch_package
from .inputs import input_path, load_yanked
from .inspect import inspect_package, rpm_signer
from .metadata import (
    StoredPackage,
    check_stored,
    compose_metadata,
    extract_metadata,
)
from .models import LockEntry, pool_path, version_channel


def _command(args: list[str], cwd: Path) -> bytes:
    result = subprocess.run(args, cwd=cwd, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"native tool failed: {args[0]}: {result.stderr.decode()}")
    return result.stdout


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


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
    """Reuse package records and rewrite deterministic aggregate trees."""
    repositories = load_repositories(repo)
    config = json.loads((repo / "sources/github.json").read_bytes())
    names = config.get("native_package_names", {})
    lock_path = input_path(repo, "packages.lock.json")
    metadata_path = input_path(repo, "package-metadata.json")
    yanked = load_yanked(repo)
    stored = _load_packages(repo)
    key = repo / "public/keys/gelstable.asc"
    if any(path.is_symlink() for path in (key, *key.parents)):
        raise ValueError("symlink in native public key")
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
                if saved is None:
                    blob = fetch_package(package.url, digest, package.size, cache)
                    inspected = inspect_package(
                        blob, fmt, names[repository], key if live else None
                    )
                    saved = StoredPackage(identity=inspected, size=package.size)
                    extract_metadata(blob, saved, scratch / digest, _command)
                    if fmt == "rpm":
                        try:
                            saved.signer = rpm_signer(blob)
                        except ValueError:
                            if live:
                                raise
                        if live:
                            from ..validation.native import signing_subkeys

                            matches = [
                                valid
                                for fingerprint, valid in signing_subkeys(key).items()
                                if saved.signer and fingerprint.endswith(saved.signer)
                            ]
                            if matches != [True]:
                                raise ValueError(
                                    "RPM signer must be a current signing subkey"
                                )
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
        metadata = scratch / "metadata"
        compose_metadata(entries, stored, metadata, base_url, _command)
        # Keep old Release dates and signatures when the signed bytes match.
        for channel in ("stable", "testing"):
            new = metadata / "apt/dists" / channel / "Release"
            old = out / "apt/dists" / channel / "Release"
            if old.is_file() and not old.is_symlink():

                def without_date(raw: bytes) -> bytes:
                    return b"\n".join(
                        line
                        for line in raw.splitlines()
                        if not line.startswith(b"Date:")
                    )

                if without_date(new.read_bytes()) == without_date(old.read_bytes()):
                    new.write_bytes(old.read_bytes())
        for pattern in (
            "apt/dists/*/InRelease",
            "apt/dists/*/Release.gpg",
            "rpm/*/*/repodata/repomd.xml.asc",
        ):
            for old in out.glob(pattern):
                if old.is_symlink():
                    raise ValueError("symlink in native output")
                new = metadata / old.relative_to(out)
                new.write_bytes(old.read_bytes())
        before = {
            str(p.relative_to(out)): p.read_bytes()
            for fmt in ("apt", "rpm")
            for p in (out / fmt).rglob("*")
            if p.is_file() and not p.is_symlink()
        }
        after = {
            str(p.relative_to(metadata)): p.read_bytes()
            for p in metadata.rglob("*")
            if p.is_file()
        }
        changed = (
            before != after
            or not lock_path.exists()
            or lock_path.read_bytes() != lock
            or not metadata_path.exists()
            or metadata_path.read_bytes() != retained
        )
        _install_metadata(
            metadata,
            out,
            lock_path,
            lock,
            {
                metadata_path: retained,
            },
        )
    return changed


def _install_metadata(
    metadata: Path,
    out: Path,
    lock_path: Path,
    lock: bytes,
    native_files: dict[Path, bytes] | None = None,
) -> None:
    """Install composed trees; Git restores an interrupted checkout."""
    native_files = {lock_path: lock, **(native_files or {})}
    for path in (out, out / "apt", out / "rpm", *native_files):
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError(f"symlink in native output: {path}")
    out.mkdir(parents=True, exist_ok=True)
    for fmt in ("apt", "rpm"):
        destination = out / fmt
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(metadata / fmt, destination)
    for path, data in native_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
