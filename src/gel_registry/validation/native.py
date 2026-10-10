"""Offline consistency checks over signed native metadata and committed records."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from ..native.inputs import input_path, load_yanked
from ..native.metadata import NAMESPACES, StoredPackage, check_stored
from ..native.models import CHANNELS, SUPPORTED, LockEntry, format_evr, pool_path
from ..native.sign import cleartext_matches_release, verify_signature
from ..render.snapshots import load_releases
from .report import Collector


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


def _packages(raw: bytes) -> set[tuple[str, str, int, str, str, str]]:
    entries = set()
    for stanza in raw.decode().strip().split("\n\n"):
        if not stanza.strip():
            continue
        fields = {}
        for line in stanza.splitlines():
            if line.startswith(
                (
                    "Filename:",
                    "SHA256:",
                    "Size:",
                    "Package:",
                    "Version:",
                    "Architecture:",
                )
            ):
                name, value = line.split(":", 1)
                if name in fields:
                    raise ValueError(f"duplicate package field: {name}")
                fields[name] = value.strip()
        entries.add(
            (
                fields["SHA256"],
                fields["Filename"],
                int(fields["Size"]),
                fields["Package"],
                fields["Version"],
                fields["Architecture"],
            )
        )
    return entries


def validate_lock(repo: Path) -> list[LockEntry]:
    """Parse the shared contract and bind every live artifact to a record."""
    public = repo / "public"
    raw_lock = json.loads(input_path(repo, "packages.lock.json").read_bytes())
    if not isinstance(raw_lock, list):
        raise ValueError("native lock must be an array")
    entries = [LockEntry.model_validate(entry) for entry in raw_lock]
    for entry in entries:
        _safe(public / ("apt" if entry.format == "deb" else "rpm"), entry.path)
    yanked = load_yanked(repo)
    records = set()
    live_records = set()
    for record in load_releases(repo):
        if record.native:
            for package in record.native.packages:
                asset = urlsplit(package.url).path.rsplit("/", 1)[1]
                item = (
                    asset.rsplit(".", 1)[1],
                    package.sha256,
                    package.size,
                    pool_path(record, package),
                )
                records.add(item)
                if package.sha256 not in yanked:
                    live_records.add(item)
    locked = {(e.format, e.sha256, e.size, e.path) for e in entries}
    if len(entries) != len(locked):
        raise ValueError("duplicate native lock artifact")
    if not live_records <= locked <= records:
        raise ValueError("native lock does not match committed records")
    return entries


def validate_apt(public: Path, channel: str, entries: list[LockEntry]) -> set[Path]:
    """Validate one APT suite, independent of signatures."""
    apt = public / "apt/dists" / channel
    release = _safe(apt, "Release")
    expected = {release}
    for name in ("InRelease", "Release.gpg"):
        signature = _safe(apt, name)
        if signature.exists():
            expected.add(signature)
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
        for arch in SUPPORTED["deb"]
        for suffix in ("", ".gz")
    }
    # apt-ftparchive can include Release itself, but Packages are mandatory.
    if not required <= hashes.keys() or hashes.keys() - required - {"Release"}:
        raise ValueError("unexpected Release SHA256 paths")
    for relative in required:
        path = _safe(apt, relative)
        expected.add(path)
        _check_hash(path, *hashes[relative])
    for arch in SUPPORTED["deb"]:
        path = apt / f"main/binary-{arch}/Packages"
        if (
            gzip.decompress(path.with_name("Packages.gz").read_bytes())
            != path.read_bytes()
        ):
            raise ValueError("compressed Packages differs")
        wanted = {
            (e.sha256, e.path, e.size, e.name, e.version, e.arch)
            for e in entries
            if e.format == "deb"
            and e.arch == arch
            and (channel == "testing" or e.channel == channel)
        }
        if _packages(path.read_bytes()) != wanted:
            raise ValueError("Packages does not match native lock")
    return expected


def _xml(raw: bytes) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("invalid native XML metadata") from exc


def _rpm_packages(raw: bytes, kind: str) -> ET.Element:
    tree = _xml(raw)
    tag = (
        "metadata" if kind == "primary" else ("otherdata" if kind == "other" else kind)
    )
    if tree.tag != f"{{{NAMESPACES[kind]}}}{tag}":
        raise ValueError("invalid RPM metadata root")
    if int(tree.get("packages", "-1")) != len(tree.findall("{*}package")):
        raise ValueError("RPM package count differs")
    return tree


def validate_rpm(
    public: Path, channel: str, arch: str, entries: list[LockEntry]
) -> set[Path]:
    """Validate RPM checksums and package identities for one architecture."""
    root = public / "rpm" / channel / arch
    repomd = _safe(root, "repodata/repomd.xml")
    signature = _safe(root, "repodata/repomd.xml.asc")
    expected = {repomd}
    if signature.exists():
        expected.add(signature)
    trees: dict[str, ET.Element] = {}
    kinds = set()
    for data in _xml(repomd.read_bytes()).findall("{*}data"):
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
        raw = gzip.decompress(path.read_bytes())
        open_checksum = data.find("{*}open-checksum")
        open_size = data.find("{*}open-size")
        if (
            open_checksum is None
            or open_checksum.get("type") != "sha256"
            or open_checksum.text != hashlib.sha256(raw).hexdigest()
            or open_size is None
            or int(open_size.text or "") != len(raw)
        ):
            raise ValueError("invalid RPM open checksum or size")
        assert kind is not None
        trees[kind] = _rpm_packages(raw, kind)
    if kinds != {"primary", "filelists", "other"}:
        raise ValueError("primary metadata missing")
    packages = set()
    for package_node in trees["primary"].findall("{*}package"):
        location = package_node.find("{*}location")
        checksum = package_node.find("{*}checksum")
        size_node = package_node.find("{*}size")
        name_node = package_node.find("{*}name")
        arch_node = package_node.find("{*}arch")
        version_node = package_node.find("{*}version")
        if (
            location is None
            or checksum is None
            or size_node is None
            or checksum.get("type") != "sha256"
        ):
            raise ValueError("invalid primary package")
        if (
            name_node is None
            or not name_node.text
            or arch_node is None
            or not arch_node.text
            or version_node is None
            or not {"epoch", "ver", "rel"} <= version_node.attrib.keys()
        ):
            raise ValueError("invalid primary package identity")
        epoch = version_node.attrib["epoch"]
        version = format_evr(
            epoch, version_node.attrib["ver"], version_node.attrib["rel"]
        )
        packages.add(
            (
                checksum.text or "",
                location.attrib["href"],
                int(size_node.attrib["package"]),
                name_node.text,
                version,
                arch_node.text,
            )
        )
    wanted = {
        (e.sha256, e.path, e.size, e.name, e.version, e.arch)
        for e in entries
        if e.format == "rpm"
        and e.arch == arch
        and (channel == "testing" or e.channel == channel)
    }
    if packages != wanted:
        raise ValueError("primary metadata does not match native lock")
    auxiliary_wanted = {
        (e.sha256, e.name, e.version, e.arch)
        for e in entries
        if e.format == "rpm"
        and e.arch == arch
        and (channel == "testing" or e.channel == channel)
    }
    for kind in ("filelists", "other"):
        auxiliary = set()
        for node in trees[kind].findall("{*}package"):
            version_node = node.find("{*}version")
            if (
                version_node is None
                or not {"epoch", "ver", "rel"} <= version_node.attrib.keys()
            ):
                raise ValueError("invalid auxiliary RPM version")
            auxiliary.add(
                (
                    node.get("pkgid"),
                    node.get("name"),
                    format_evr(
                        version_node.attrib["epoch"],
                        version_node.attrib["ver"],
                        version_node.attrib["rel"],
                    ),
                    node.get("arch"),
                )
            )
        if auxiliary != auxiliary_wanted:
            raise ValueError(f"{kind} metadata does not match native lock")
    return expected


def validate_inventory(public: Path, expected: set[Path]) -> None:
    """Reject missing, extra and linked files throughout the native trees."""
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


def signing_subkeys(key: Path) -> dict[str, bool]:
    """Return full signing-subkey fingerprints and current revocation status."""
    with tempfile.TemporaryDirectory(prefix="native-certificate-") as directory:
        result = subprocess.run(
            [
                "gpg",
                "--batch",
                "--no-options",
                "--homedir",
                directory,
                "--with-colons",
                "--import-options",
                "show-only",
                "--import",
                str(key.resolve()),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise ValueError("cannot read native signing certificate")
    subkeys = {}
    pending: bool | None = None
    primary_revoked = False
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            primary_revoked = fields[1] == "r"
            pending = None
        elif fields[0] == "sub":
            pending = (
                fields[1] not in {"r", "e", "d"} and not primary_revoked
                if "s" in fields[11].lower()
                else None
            )
        elif fields[0] == "fpr" and pending is not None:
            subkeys[fields[9]] = pending
            pending = None
    return subkeys


def validate_retained(
    repo: Path, entries: list[LockEntry], *, require_generation: bool = False
) -> None:
    """Keep reusable metadata and RPM trust attestations sound at publication."""
    path = input_path(repo, "package-metadata.json")
    if not path.exists():
        if entries or require_generation:
            raise ValueError("retained metadata missing; render before signing")
        return
    raw = path.read_bytes()
    packages = json.loads(raw)
    if not isinstance(packages, dict) or any(
        not re.fullmatch(r"[0-9a-f]{64}", key) for key in packages
    ):
        raise ValueError("invalid retained native package map")
    stored = {
        digest: StoredPackage.model_validate(value)
        for digest, value in packages.items()
    }
    for digest, package in stored.items():
        check_stored(digest, package, live=False)
    subkeys = signing_subkeys(_safe(repo / "public", "keys/gelstable.asc"))
    yanked = load_yanked(repo)
    bad_signers = []
    for entry in entries:
        saved = stored.get(entry.sha256)
        if saved is None or (
            saved.identity.format,
            saved.identity.name,
            saved.identity.version,
            saved.identity.arch,
            saved.size,
        ) != (entry.format, entry.name, entry.version, entry.arch, entry.size):
            raise ValueError("retained package identity does not match native lock")
        check_stored(entry.sha256, saved, live=True)
        if entry.format == "rpm" and entry.sha256 not in yanked:
            matches = [
                valid
                for fingerprint, valid in subkeys.items()
                if saved.signer and fingerprint.endswith(saved.signer)
            ]
            if matches != [True]:
                bad_signers.append(entry.sha256)
    if bad_signers:
        raise ValueError(
            "RPM signer is missing, ambiguous or revoked; yank digests: "
            + ", ".join(sorted(bad_signers))
        )


def validate_native_structure(repo: Path, *, require_generation: bool = True) -> None:
    """Validate unsigned or signed metadata without requiring signatures."""
    entries = validate_lock(repo)
    public = repo / "public"
    expected: set[Path] = set()
    for channel in CHANNELS:
        expected.update(validate_apt(public, channel, entries))
        for arch in SUPPORTED["rpm"]:
            expected.update(validate_rpm(public, channel, arch, entries))
    validate_inventory(public, expected)
    validate_retained(repo, entries, require_generation=require_generation)


def validate_native_signatures(repo: Path, *, key: Path | None = None) -> None:
    """Require every signature and verify using the public certificate only."""
    public = repo / "public"
    key = key or _safe(public, "keys/gelstable.asc")
    for channel in CHANNELS:
        apt = public / "apt/dists" / channel
        release = _safe(apt, "Release")
        if not cleartext_matches_release(
            verify_signature(key, _safe(apt, "InRelease")), release.read_bytes()
        ):
            raise ValueError("InRelease cleartext does not match Release")
        verify_signature(key, _safe(apt, "Release.gpg"), release)
        for arch in SUPPORTED["rpm"]:
            root = public / "rpm" / channel / arch
            verify_signature(
                key,
                _safe(root, "repodata/repomd.xml.asc"),
                _safe(root, "repodata/repomd.xml"),
            )


def validate_native(repo: Path, *, base: Path | None = None) -> None:
    """Authenticate publication, or unchanged base metadata in a key-change PR."""
    validate_native_structure(repo, require_generation=False)
    try:
        validate_native_signatures(repo)
    except (ValueError, OSError):
        # A certificate/yank PR has no signing secret. Authenticate its exact
        # unchanged metadata with the already trusted merge-base certificate;
        # current-certificate RPM signer rules above still apply to live entries.
        if (
            base is None
            or (repo / "public/keys/gelstable.asc").read_bytes()
            == (base / "public/keys/gelstable.asc").read_bytes()
        ):
            raise
        for relative in ("native/packages.lock.json", "native/package-metadata.json"):
            current, previous = repo / relative, base / relative
            if current.exists() != previous.exists() or (
                current.exists() and current.read_bytes() != previous.read_bytes()
            ):
                raise ValueError(
                    "key-change PR must retain base lock and package records"
                ) from None
        for fmt in ("apt", "rpm"):

            def tree(root: Path) -> dict[str, bytes]:
                result = {}
                for path in root.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("symlink in key-change metadata")
                    if path.is_file():
                        result[str(path.relative_to(root))] = path.read_bytes()
                return result

            if tree(repo / "public" / fmt) != tree(base / "public" / fmt):
                raise ValueError(
                    "key-change PR must retain exact base metadata"
                ) from None
        validate_native_signatures(
            repo, key=_safe(base / "public", "keys/gelstable.asc")
        )


def check_native(repo: Path, collector: Collector, base: Path | None = None) -> None:
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
        "native.integrity",
        "public/apt public/rpm",
        lambda: validate_native(repo, base=base),
    )
