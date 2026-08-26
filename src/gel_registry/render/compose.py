"""Pure composition of bootstrap indexes and release records.

Nothing in this module touches the filesystem: it maps validated inputs to
the canonical index bytes a snapshot is built from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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
            key=_package_sort_key,
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
            ref=f"{prefix}{identity}.json",
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
    # A set comparison only proves each encoding appears at least once.  Two
    # identity artifacts would satisfy it, emit duplicate installrefs, and let
    # ``next`` below pick one of them arbitrarily, so the count is checked too.
    encodings = [artifact.encoding for artifact in artifacts]
    if sorted(encodings) != ["identity", "zstd"]:
        raise RenderError(
            f"release {record.version} does not have exactly one identity and "
            f"one zstd artifact for {platform}"
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


def _package_sort_key(
    package: PackageEntry,
) -> tuple[str, int, tuple[Version, ...], str, bytes]:
    """Return a total ordering key for one package entry.

    The legacy index carries versions PEP 440 cannot parse (``1.0-dev.6154``
    parses, a malformed entry may not), so parsed and unparsed versions are
    partitioned into separate ranks and never compared against each other.  A
    comparator that fell back to a lexical branch whenever *either* side failed
    to parse was not transitive, which made the sorted order depend on the
    input order and put the reproducibility of the rendered bytes at risk.

    The trailing version string and canonical bytes break ties so entries that
    compare equal under PEP 440 -- ``1.0.0-alpha.1`` and ``1.0.0a1`` -- still
    have one deterministic order.
    """

    try:
        parsed: tuple[Version, ...] = (Version(package.version),)
    except InvalidVersion:
        return (package.basename, 1, (), package.version, canonical_json(package))
    return (package.basename, 0, parsed, package.version, canonical_json(package))


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
