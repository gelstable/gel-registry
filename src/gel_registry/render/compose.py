"""Pure composition of bootstrap indexes and release records.

Nothing in this module touches the filesystem: it maps validated inputs to
the canonical index bytes a snapshot is built from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import cmp_to_key
from typing import Any

from packaging.version import InvalidVersion, Version

from ..contracts import (
    InstallRef,
    PackageEntry,
    PackageIndex,
    ReleaseRecord,
    RootIndex,
    RootManifest,
    Verification,
)
from ..digest import canonical_json
from .errors import ContestedIdentityError, RenderError


def compose_indexes(
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
        platforms = sorted({artifact.platform for artifact in record.artifacts})
        for platform in platforms:
            key = (record.channel, platform)
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


def root_for_indexes(blobs: Mapping[tuple[str, str], str], prefix: str) -> RootManifest:
    """Build a manifest of pointers into the content-addressed blob store.

    ``prefix`` is what makes one manifest resolve correctly from its own
    location: a pinned root at ``s/<snapshot>/registry.json`` carries
    ``../../i/``, the moving root at ``registry.json`` carries ``i/``.  Both are
    document-relative on purpose -- ``gel-cli`` resolves a scheme-less reference
    against the manifest's own source, and for a file mirror that is
    ``Path.join``, which discards the base when given a root-absolute path.
    """

    indexes = tuple(
        RootIndex(
            channel=channel,
            platform=platform,
            url=f"{prefix}{identity}.json",
        )
        for (channel, platform), identity in blobs.items()
    )
    return RootManifest(indexes=indexes)


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
