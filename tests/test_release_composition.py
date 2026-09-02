"""Release records describe the exact index changes they publish."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gel_registry.contracts import (
    IndexFragment,
    PackageEntry,
    PackageIndex,
    ReleaseRecord,
    Replacement,
)
from gel_registry.render import ContestedIdentityError
from gel_registry.render.compose import compose_indexes

REPOSITORY = "gelstable/gel-cli"
TAG = "v1.0.0"
PREFIX = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/"


def record(
    *,
    replacements: tuple[Replacement, ...] = (),
    indexes: tuple[IndexFragment, ...] = (),
) -> ReleaseRecord:
    return ReleaseRecord(
        source={
            "repository": REPOSITORY,
            "release_id": 1,
            "tag": TAG,
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=replacements,
        indexes=indexes,
    )


def changed_fields(left: PackageEntry, right: PackageEntry) -> set[str]:
    return {
        field
        for field in PackageEntry.model_fields
        if getattr(left, field) != getattr(right, field)
    }


def with_github_reference(
    package: PackageEntry, suffix: str = "publisher"
) -> PackageEntry:
    reference = package.installrefs[0].model_copy(update={"ref": PREFIX + suffix})
    return package.model_copy(
        update={"installref": reference.ref, "installrefs": (reference,)}
    )


def test_rescue_changes_only_matching_urls_in_a_real_mirror_entry() -> None:
    """A rescue preserves every historical field except selected URLs."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    original = index.packages[0]
    digest = original.installrefs[0].verification.sha256
    assert digest is not None

    output = compose_indexes(
        {key: index},
        (record(replacements=(Replacement(sha256=digest, url=PREFIX + "rescued"),)),),
    )
    rescued = next(
        package
        for package in PackageIndex.model_validate_json(output[key]).packages
        if package.basename == original.basename
        and package.version == original.version
        and package.slot == original.slot
    )

    assert (
        rescued.model_copy(
            update={
                "installref": original.installref,
                "installrefs": original.installrefs,
            }
        )
        == original
    )
    assert changed_fields(rescued, original) <= {"installref", "installrefs"}
    for actual, expected in zip(rescued.installrefs, original.installrefs, strict=True):
        assert actual.model_copy(update={"ref": expected.ref}) == expected


def test_partial_rescue_leaves_other_artifacts_on_packages_geldata_com() -> None:
    """Replacement is per install reference, not per package."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    original = index.packages[0]
    digest = original.installrefs[0].verification.sha256
    assert digest is not None

    output = compose_indexes(
        {key: index},
        (record(replacements=(Replacement(sha256=digest, url=PREFIX + "identity"),)),),
    )
    rescued = next(
        package
        for package in PackageIndex.model_validate_json(output[key]).packages
        if package.basename == original.basename
        and package.version == original.version
        and package.slot == original.slot
    )

    assert rescued.installrefs[0].ref == PREFIX + "identity"
    assert rescued.installrefs[1].ref.startswith("https://packages.geldata.com/")


def test_net_new_entries_are_added_to_the_declared_channel_and_platform() -> None:
    """The publisher entry reaches the output without central reconstruction."""
    key = ("testing", "x86_64-unknown-linux-musl")
    entry = with_github_reference(
        PackageEntry.model_validate(
            {
                "basename": "gel-cli",
                "name": "gel-cli",
                "version": "1.0.0",
                "version_details": {
                    "major": 1,
                    "minor": 0,
                    "patch": 0,
                    "prerelease": [],
                    "metadata": {},
                },
                "version_key": "1.0.0",
                "revision": "1",
                "build_date": "2026-08-15T00:00:00+00:00",
                "architecture": "x86_64",
                "slot": "",
                "installref": "https://packages.geldata.com/archive/gel-cli-1.0.0",
                "installrefs": [
                    {
                        "ref": "https://packages.geldata.com/archive/gel-cli-1.0.0",
                        "type": "application/octet-stream",
                        "verification": {
                            "size": 3,
                            "sha256": "a" * 64,
                            "blake2b": "b" * 128,
                        },
                    }
                ],
            }
        )
    )

    output = compose_indexes(
        {},
        (
            record(
                indexes=(
                    IndexFragment(channel=key[0], platform=key[1], packages=(entry,)),
                )
            ),
        ),
    )

    assert PackageIndex.model_validate_json(output[key]).packages == (entry,)


def test_conflicting_consumer_identity_stops_composition() -> None:
    """Two meanings for one installable identity are never resolved by order."""
    first = with_github_reference(
        PackageEntry.model_validate(
            {
                "basename": "gel-cli",
                "name": "gel-cli",
                "version": "1.0.0",
                "version_details": {
                    "major": 1,
                    "minor": 0,
                    "patch": 0,
                    "prerelease": [],
                    "metadata": {},
                },
                "version_key": "1.0.0",
                "revision": "1",
                "build_date": "2026-08-15T00:00:00+00:00",
                "architecture": "x86_64",
                "slot": "",
                "installref": "https://packages.geldata.com/archive/gel-cli-1.0.0",
                "installrefs": [
                    {
                        "ref": "https://packages.geldata.com/archive/gel-cli-1.0.0",
                        "type": "application/octet-stream",
                        "verification": {
                            "size": 3,
                            "sha256": "a" * 64,
                            "blake2b": "b" * 128,
                        },
                    }
                ],
            }
        )
    )
    conflicting = first.model_copy(update={"revision": "2"})
    fragments = (
        IndexFragment(
            channel="stable", platform="x86_64-unknown-linux-musl", packages=(first,)
        ),
        IndexFragment(
            channel="stable",
            platform="x86_64-unknown-linux-musl",
            packages=(conflicting,),
        ),
    )

    with pytest.raises(ContestedIdentityError):
        compose_indexes(
            {}, tuple(record(indexes=(fragment,)) for fragment in fragments)
        )
