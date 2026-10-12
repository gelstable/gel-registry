"""Extract once and compose complete native indexes from package records."""

from __future__ import annotations

import copy
import gzip
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from .models import CHANNELS, SUPPORTED, LockEntry, PackageIdentity

Command = Callable[[list[str], Path], bytes]
KINDS = ("primary", "filelists", "other")
NAMESPACES = {
    "primary": "http://linux.duke.edu/metadata/common",
    "filelists": "http://linux.duke.edu/metadata/filelists",
    "other": "http://linux.duke.edu/metadata/other",
}
REPOMD_NS = "http://linux.duke.edu/metadata/repo"
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
RELEASE_OPTIONS = {
    "Origin": "Gelstable",
    "Label": "Gelstable",
    "Architectures": "amd64 arm64",
    "Components": "main",
}


class StoredPackage(BaseModel):
    """Digest-bound full metadata; retained after withdrawal to reserve identity."""

    model_config = ConfigDict(extra="forbid", strict=True)
    identity: PackageIdentity
    size: int = Field(gt=0)
    deb: str | None = None
    rpm: dict[str, str] = Field(default_factory=dict)
    signer: str | None = Field(
        default=None,
        pattern=r"^(?:[0-9A-F]{8}|[0-9A-F]{16}|[0-9A-F]{40}|[0-9A-F]{64})$",
    )


def deb_fields(stanza: str) -> dict[str, str]:
    """Read top-level fields while preserving continuation text in the stanza."""
    fields: dict[str, str] = {}
    for line in stanza.splitlines():
        if line.startswith((" ", "\t")):
            continue
        name, separator, value = line.partition(":")
        if not separator or name in fields:
            raise ValueError("invalid or duplicate Debian package field")
        fields[name] = value.strip()
    return fields


def parse_xml(raw: str | bytes) -> ET.Element:
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("invalid retained native XML metadata") from exc


def check_stored(digest: str, package: StoredPackage, *, live: bool) -> None:
    """Reject retained records whose complete metadata disagrees with identity."""
    identity = package.identity
    if identity.format == "deb":
        if package.deb is None:
            if live:
                raise ValueError("retained Debian metadata missing")
            return
        fields = deb_fields(package.deb)
        if (
            fields.get("SHA256"),
            int(fields.get("Size", "-1")),
            fields.get("Package"),
            fields.get("Version"),
            fields.get("Architecture"),
        ) != (digest, package.size, identity.name, identity.version, identity.arch):
            raise ValueError("retained Debian metadata does not match identity")
    else:
        if not package.rpm and not live:
            return
        if set(package.rpm) != set(KINDS):
            raise ValueError("retained RPM metadata missing")
        from .models import format_evr

        primary = parse_xml(package.rpm["primary"])
        version = primary.find("{*}version")
        checksum = primary.find("{*}checksum")
        size = primary.find("{*}size")
        if (
            version is None
            or checksum is None
            or size is None
            or not {"epoch", "ver", "rel"} <= version.attrib.keys()
            or "package" not in size.attrib
        ):
            raise ValueError("invalid retained RPM metadata")
        if (
            checksum.text,
            int(size.attrib["package"]),
            primary.findtext("{*}name"),
            format_evr(
                version.attrib["epoch"], version.attrib["ver"], version.attrib["rel"]
            ),
            primary.findtext("{*}arch"),
        ) != (digest, package.size, identity.name, identity.version, identity.arch):
            raise ValueError("retained RPM metadata does not match identity")
        for kind in ("filelists", "other"):
            node = parse_xml(package.rpm[kind])
            if (node.get("pkgid"), node.get("name"), node.get("arch")) != (
                digest,
                identity.name,
                identity.arch,
            ):
                raise ValueError("retained RPM auxiliary identity differs")
            other_version = node.find("{*}version")
            if other_version is None or other_version.attrib != version.attrib:
                raise ValueError("retained RPM auxiliary version differs")


def extract_metadata(
    blob: Path, package: StoredPackage, scratch: Path, command: Command
) -> None:
    """Run distribution tools only on new or newly trusted package bytes."""
    pool = scratch / "pool"
    pool.mkdir(parents=True)
    os.link(blob, pool / f"package.{package.identity.format}")
    if package.identity.format == "deb":
        package.deb = (
            command(["apt-ftparchive", "packages", "pool"], scratch).decode().strip()
        )
    else:
        command(["createrepo_c", "--no-database", str(scratch)], scratch)
        repodata = scratch / "repodata"
        for kind in KINDS:
            path = next(repodata.glob(f"*-{kind}.xml.gz"))
            root = parse_xml(gzip.decompress(path.read_bytes()))
            nodes = root.findall("{*}package")
            if len(nodes) != 1:
                raise ValueError(
                    "package extraction produced an unexpected package set"
                )
            package.rpm[kind] = ET.tostring(nodes[0], encoding="unicode")


def _deb_stanza(package: StoredPackage, entry: LockEntry) -> str:
    assert package.deb is not None
    return "\n".join(
        "Filename: " + entry.path if line.startswith("Filename:") else line
        for line in package.deb.splitlines()
    )


def _rpm_tree(
    kind: str, entries: list[LockEntry], stored: dict[str, StoredPackage], base_url: str
) -> bytes:
    tag = (
        "metadata" if kind == "primary" else ("otherdata" if kind == "other" else kind)
    )
    root = ET.Element(f"{{{NAMESPACES[kind]}}}{tag}", {"packages": str(len(entries))})
    for entry in entries:
        node = copy.deepcopy(parse_xml(stored[entry.sha256].rpm[kind]))
        if kind == "primary":
            location = node.find("{*}location")
            if location is None:
                raise ValueError("retained RPM location missing")
            location.set("href", entry.path)
            location.set(XML_BASE, base_url.rstrip("/") + "/rpm/")
        root.append(node)
    ET.register_namespace("", NAMESPACES[kind])
    ET.register_namespace("rpm", "http://linux.duke.edu/metadata/rpm")
    raw: bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return raw


def compose_metadata(
    entries: list[LockEntry],
    stored: dict[str, StoredPackage],
    out: Path,
    base_url: str,
    command: Command,
) -> None:
    """Rewrite small aggregate indexes, without reading previously seen blobs."""
    for channel in CHANNELS:
        apt = out / "apt/dists" / channel
        for arch in SUPPORTED["deb"]:
            binary = apt / "main" / f"binary-{arch}"
            binary.mkdir(parents=True)
            selected = [
                e
                for e in entries
                if e.format == "deb"
                and e.arch == arch
                and (channel == "testing" or e.channel == channel)
            ]
            raw = "".join(
                _deb_stanza(stored[e.sha256], e) + "\n\n" for e in selected
            ).encode()
            (binary / "Packages").write_bytes(raw)
            (binary / "Packages.gz").write_bytes(
                gzip.compress(raw, compresslevel=9, mtime=0)
            )
        options = {**RELEASE_OPTIONS, "Suite": channel, "Codename": channel}
        args = ["apt-ftparchive"]
        for name, value in options.items():
            args.extend(["-o", f"APT::FTPArchive::Release::{name}={value}"])
        (apt / "Release").write_bytes(command([*args, "release", "."], apt))
        for arch in SUPPORTED["rpm"]:
            repodata = out / "rpm" / channel / arch / "repodata"
            repodata.mkdir(parents=True)
            selected = [
                e
                for e in entries
                if e.format == "rpm"
                and e.arch == arch
                and (channel == "testing" or e.channel == channel)
            ]
            repomd = ET.Element(f"{{{REPOMD_NS}}}repomd")
            ET.SubElement(repomd, f"{{{REPOMD_NS}}}revision").text = hashlib.sha256(
                b"".join(_rpm_tree(kind, selected, stored, base_url) for kind in KINDS)
            ).hexdigest()
            for kind in KINDS:
                raw = _rpm_tree(kind, selected, stored, base_url)
                compressed = gzip.compress(raw, compresslevel=9, mtime=0)
                digest = hashlib.sha256(compressed).hexdigest()
                filename = f"{digest}-{kind}.xml.gz"
                (repodata / filename).write_bytes(compressed)
                data = ET.SubElement(repomd, f"{{{REPOMD_NS}}}data", {"type": kind})
                for tag, attrs, value in (
                    ("checksum", {"type": "sha256"}, digest),
                    (
                        "open-checksum",
                        {"type": "sha256"},
                        hashlib.sha256(raw).hexdigest(),
                    ),
                    ("location", {"href": "repodata/" + filename}, ""),
                    ("timestamp", {}, "0"),
                    ("size", {}, str(len(compressed))),
                    ("open-size", {}, str(len(raw))),
                ):
                    ET.SubElement(data, f"{{{REPOMD_NS}}}{tag}", attrs).text = value
            ET.register_namespace("", REPOMD_NS)
            (repodata / "repomd.xml").write_bytes(
                ET.tostring(repomd, encoding="utf-8", xml_declaration=True)
            )
