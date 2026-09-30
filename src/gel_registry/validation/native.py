"""Offline consistency checks over signed native metadata and committed records."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from ..native.sign import verify_signature
from ..render.snapshots import load_releases
from .report import Collector


class LockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channel: str
    format: str
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    arch: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0)
    path: str


def _safe(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe metadata path: {relative}")
    result = root / path
    if any(p.is_symlink() for p in (result, *result.parents)):
        raise ValueError(f"symlink in native metadata: {result}")
    return result


def _check_hash(path: Path, digest: str, size: int | None = None) -> None:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest or (
        size is not None and len(raw) != size
    ):
        raise ValueError(f"checksum or size mismatch: {path}")


def _packages(raw: bytes) -> set[tuple[str, str, int]]:
    entries = set()
    for stanza in raw.decode().strip().split("\n\n"):
        if not stanza.strip():
            continue
        fields = {}
        for line in stanza.splitlines():
            if line.startswith(("Filename:", "SHA256:", "Size:")):
                name, value = line.split(":", 1)
                if name in fields:
                    raise ValueError(f"duplicate package field: {name}")
                fields[name] = value.strip()
        entries.add((fields["SHA256"], fields["Filename"], int(fields["Size"])))
    return entries


def validate_native(repo: Path) -> None:
    """Validate initialized native trees without network, package files or secrets."""
    public = repo / "public"
    lock_path = repo / "native/packages.lock.json"
    raw_lock = json.loads(lock_path.read_bytes())
    if not isinstance(raw_lock, list):
        raise ValueError("native lock must be an array")
    entries = [LockEntry.model_validate(entry) for entry in raw_lock]
    supported = {"deb": {"amd64", "arm64"}, "rpm": {"x86_64", "aarch64"}}
    for entry in entries:
        if (
            entry.channel not in {"stable", "testing"}
            or entry.arch not in supported.get(entry.format, set())
            or not entry.path.startswith("pool/")
        ):
            raise ValueError(
                "invalid native lock channel, format, architecture or path"
            )
        _safe(public / ("apt" if entry.format == "deb" else "rpm"), entry.path)
    yanks_path = repo / "native/yanked.json"
    yanks = json.loads(yanks_path.read_bytes()) if yanks_path.exists() else []
    if not isinstance(yanks, list) or any(
        not isinstance(e, dict)
        or set(e) != {"sha256", "reason"}
        or not isinstance(e["sha256"], str)
        or not isinstance(e["reason"], str)
        for e in yanks
    ):
        raise ValueError("invalid native yank list")
    yanked = {e["sha256"] for e in yanks}
    records = set()
    for record in load_releases(repo):
        if record.native:
            for package in record.native.packages:
                if package.sha256 in yanked:
                    continue
                asset = urlsplit(package.url).path.rsplit("/", 1)[1]
                records.add(
                    (
                        record.native.channel,
                        asset.rsplit(".", 1)[1],
                        package.sha256,
                        package.size,
                        f"pool/{record.source.repository.split('/')[1]}/"
                        f"{record.source.tag}/{asset}",
                    )
                )
    locked = {(e.channel, e.format, e.sha256, e.size, e.path) for e in entries}
    if len(entries) != len(locked):
        raise ValueError("duplicate native lock artifact")
    if locked != records:
        raise ValueError("native lock does not match committed records")
    expected: set[Path] = set()
    key = public / "keys/gelstable.asc"
    for channel in ("stable", "testing"):
        apt = public / "apt/dists" / channel
        release = _safe(apt, "Release")
        inrelease = _safe(apt, "InRelease")
        detached = _safe(apt, "Release.gpg")
        expected.update((release, inrelease, detached))
        if verify_signature(key, inrelease) != release.read_bytes():
            raise ValueError("InRelease cleartext does not match Release")
        verify_signature(key, detached, release)
        hashes = {}
        section = False
        for line in release.read_text().splitlines():
            if line == "SHA256:":
                section = True
            elif line and not line[0].isspace():
                section = False
            elif section:
                digest, size, relative = line.split()
                if relative in hashes or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("invalid Release SHA256 entry")
                hashes[relative] = (digest, int(size))
        required = {
            f"main/binary-{arch}/Packages{suffix}"
            for arch in supported["deb"]
            for suffix in ("", ".gz")
        }
        # apt-ftparchive can include Release itself, but Packages are mandatory.
        if not required <= hashes.keys() or hashes.keys() - required - {"Release"}:
            raise ValueError("unexpected Release SHA256 paths")
        for relative in required:
            path = _safe(apt, relative)
            expected.add(path)
            _check_hash(path, *hashes[relative])
        for arch in supported["deb"]:
            path = apt / f"main/binary-{arch}/Packages"
            if (
                gzip.decompress(path.with_name("Packages.gz").read_bytes())
                != path.read_bytes()
            ):
                raise ValueError("compressed Packages differs")
            wanted = {
                (e.sha256, e.path, e.size)
                for e in entries
                if (e.channel, e.format, e.arch) == (channel, "deb", arch)
            }
            if _packages(path.read_bytes()) != wanted:
                raise ValueError("Packages does not match native lock")
        for arch in supported["rpm"]:
            root = public / "rpm" / channel / arch
            repomd = _safe(root, "repodata/repomd.xml")
            signature = _safe(root, "repodata/repomd.xml.asc")
            expected.update((repomd, signature))
            verify_signature(key, signature, repomd)
            primary = None
            kinds = set()
            for data in ET.fromstring(repomd.read_bytes()).findall("{*}data"):
                kind = data.get("type")
                if kind not in {"primary", "filelists", "other"} or kind in kinds:
                    raise ValueError("unexpected or duplicate repodata type")
                kinds.add(kind)
                location = data.find("{*}location")
                checksum = data.find("{*}checksum")
                size_node = data.find("{*}size")
                if (
                    location is None
                    or checksum is None
                    or size_node is None
                    or checksum.get("type") != "sha256"
                ):
                    raise ValueError("invalid repomd entry")
                path = _safe(root, location.attrib["href"])
                if path.parent != root / "repodata" or path in expected:
                    raise ValueError("invalid or duplicate repodata location")
                expected.add(path)
                _check_hash(path, checksum.text or "", int(size_node.text or ""))
                if data.get("type") == "primary":
                    if primary is not None:
                        raise ValueError("duplicate primary metadata")
                    primary = path
            if primary is None or kinds != {"primary", "filelists", "other"}:
                raise ValueError("primary metadata missing")
            packages = set()
            for package_node in ET.fromstring(
                gzip.decompress(primary.read_bytes())
            ).findall("{*}package"):
                location = package_node.find("{*}location")
                checksum = package_node.find("{*}checksum")
                size_node = package_node.find("{*}size")
                if (
                    location is None
                    or checksum is None
                    or size_node is None
                    or checksum.get("type") != "sha256"
                ):
                    raise ValueError("invalid primary package")
                packages.add(
                    (
                        checksum.text or "",
                        location.attrib["href"],
                        int(size_node.attrib["package"]),
                    )
                )
            wanted = {
                (e.sha256, e.path, e.size)
                for e in entries
                if (e.channel, e.format, e.arch) == (channel, "rpm", arch)
            }
            if packages != wanted:
                raise ValueError("primary metadata does not match native lock")
    actual = set()
    for fmt in ("apt", "rpm"):
        root = public / fmt
        if root.is_symlink():
            raise ValueError("symlink native root")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"symlink native metadata: {path}")
            if path.is_file():
                actual.add(path)
    if actual != expected:
        raise ValueError("unexpected or missing native repository files")


def check_native(repo: Path, collector: Collector) -> None:
    """Preserve existing validation before native metadata has been bootstrapped."""
    initialized = any(
        ((repo / path).exists() or (repo / path).is_symlink())
        for path in ("native/packages.lock.json", "public/apt", "public/rpm")
    )
    if not initialized:
        try:
            initialized = any(
                record.native and record.native.packages
                for record in load_releases(repo)
            )
        except (ValueError, OSError):
            # The release validation layer reports malformed records separately.
            return
    if not initialized:
        return
    collector.run(
        "native.integrity", "public/apt public/rpm", lambda: validate_native(repo)
    )
