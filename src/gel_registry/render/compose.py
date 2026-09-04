"""Pure composition of bootstrap indexes and direct release records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from ..contracts import (
    InstallRef,
    PackageEntry,
    PackageIndex,
    ReleaseRecord,
    RootIndex,
    RootManifest,
)
from ..digest import canonical_json
from .errors import ContestedIdentityError, ContestedReplacementError, RenderError


@dataclass(frozen=True)
class MirrorLocation:
    index: tuple[str, str]
    identity: tuple[str, ...]
    reference: InstallRef


type MirrorDigestIndex = dict[str, MirrorLocation | None]
type PackageBuckets = dict[
    tuple[str, str], dict[tuple[str, ...], tuple[bytes, PackageEntry]]
]


def compose_indexes(
    bootstrap: Mapping[tuple[str, str], PackageIndex], releases: Iterable[ReleaseRecord]
) -> dict[tuple[str, str], bytes]:
    """Return canonical index bytes after explicit replacements and additions."""
    records = tuple(releases)
    mirror = index_mirror_references(bootstrap)
    packages = copy_bootstrap_packages(bootstrap)
    apply_replacements(packages, mirror, records)
    add_index_fragments(packages, records)
    return serialize_indexes(packages)


def index_mirror_references(
    bootstrap: Mapping[tuple[str, str], PackageIndex],
) -> MirrorDigestIndex:
    """Map every bootstrap digest to its one unambiguous install reference."""
    mirror: MirrorDigestIndex = {}
    for index_key, index in bootstrap.items():
        for package in index.packages:
            for reference in package.installrefs:
                digest = reference.verification.sha256
                if digest is None:
                    continue
                location = MirrorLocation(
                    index_key, package_identity(package), reference
                )
                mirror[digest] = None if digest in mirror else location
    return mirror


def copy_bootstrap_packages(
    bootstrap: Mapping[tuple[str, str], PackageIndex],
) -> PackageBuckets:
    packages: PackageBuckets = {}
    for key, index in bootstrap.items():
        bucket = packages.setdefault(key, {})
        for package in index.packages:
            add_package(bucket, package)
    return packages


def apply_replacements(
    packages: PackageBuckets,
    mirror: MirrorDigestIndex,
    records: Iterable[ReleaseRecord],
) -> None:
    claims: dict[str, list[tuple[ReleaseRecord, str]]] = {}
    for record in records:
        for replacement in record.replacements:
            claims.setdefault(replacement.sha256, []).append((record, replacement.url))

    for digest in sorted(claims):
        claimants = claims[digest]
        if len(claimants) > 1:
            sources = tuple(
                sorted(
                    (
                        f"{r.source.repository}@{r.source.tag} "
                        f"(release {r.source.release_id})"
                    )
                    for r, _url in claimants
                )
            )
            raise ContestedReplacementError(digest, sources)

    for digest in sorted(claims):
        _record, url = claims[digest][0]
        location = mirror.get(digest)
        if location is None:
            reason = "ambiguous" if digest in mirror else "absent"
            raise RenderError(f"replacement digest is {reason}: {digest}")
        _serialized, package = packages[location.index][location.identity]
        new_refs = tuple(
            reference
            if reference.verification.sha256 != digest
            else reference.model_copy(update={"ref": url})
            for reference in package.installrefs
        )
        new_installref = (
            url if selected_reference_sha256(package) == digest else package.installref
        )
        updated = package.model_copy(
            update={"installref": new_installref, "installrefs": new_refs}
        )
        packages[location.index][location.identity] = (
            canonical_json(updated),
            updated,
        )


def add_index_fragments(
    packages: PackageBuckets, records: Iterable[ReleaseRecord]
) -> None:
    for record in records:
        for fragment in record.indexes:
            bucket = packages.setdefault((fragment.channel, fragment.platform), {})
            for package in fragment.packages:
                add_package(bucket, package)


def serialize_indexes(packages: PackageBuckets) -> dict[tuple[str, str], bytes]:
    return {
        key: canonical_json(
            PackageIndex(
                packages=tuple(
                    sorted(
                        (entry[1] for entry in bucket.values()), key=package_sort_key
                    )
                )
            )
        )
        for key, bucket in sorted(packages.items())
    }


def root_for_indexes(blobs: Mapping[tuple[str, str], str], prefix: str) -> RootManifest:
    """Build a manifest of pointers into the content-addressed blob store."""
    return RootManifest(
        indexes=tuple(
            RootIndex(
                channel=channel, platform=platform, ref=f"{prefix}{identity}.json"
            )
            for (channel, platform), identity in blobs.items()
        )
    )


def _installref_sort_key(reference: InstallRef) -> tuple[int, str, bytes]:
    encoding = reference.encoding or ""
    return (
        {"identity": 0, "zstd": 1}.get(encoding, 2),
        encoding,
        canonical_json(reference),
    )


def canonical_package(package: PackageEntry) -> PackageEntry:
    references = tuple(sorted(package.installrefs, key=_installref_sort_key))
    return (
        package
        if references == package.installrefs
        else package.model_copy(update={"installrefs": references})
    )


def package_identity(package: PackageEntry) -> tuple[str, ...]:
    if "extension" in package.tags or "server_slot" in package.tags:
        return (
            "extension",
            str(package.tags.get("extension", "")),
            package.version,
            str(package.tags.get("server_slot", "")),
        )
    if package.slot:
        return "server", package.basename, package.version, package.slot
    return "package", package.basename, package.version


def selected_reference_sha256(package: PackageEntry) -> str | None:
    return next(
        (
            reference.verification.sha256
            for reference in package.installrefs
            if reference.ref == package.installref
        ),
        None,
    )


def package_sort_key(
    package: PackageEntry,
) -> tuple[str, int, tuple[Version, ...], str, bytes]:
    try:
        parsed: tuple[Version, ...] = (Version(package.version),)
    except InvalidVersion:
        return package.basename, 1, (), package.version, canonical_json(package)
    return package.basename, 0, parsed, package.version, canonical_json(package)


def add_package(
    bucket: dict[tuple[str, ...], tuple[bytes, PackageEntry]], package: PackageEntry
) -> None:
    package = canonical_package(package)
    identity = package_identity(package)
    serialized = canonical_json(package)
    existing = bucket.get(identity)
    if existing is None:
        bucket[identity] = serialized, package
    elif existing[0] != serialized:
        raise ContestedIdentityError(identity)
