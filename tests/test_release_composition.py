"""Release records describe the exact index changes they publish."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from gel_registry.contracts import (
    IndexFragment,
    PackageEntry,
    PackageIndex,
    ReleaseManifest,
    ReleaseRecord,
    ReleaseSource,
    Replacement,
    validate_release_url,
    validate_repository_name,
)
from gel_registry.render import (
    ContestedIdentityError,
    ContestedReplacementError,
    RenderError,
)
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


def test_cross_record_duplicate_replacement_claims_are_rejected() -> None:
    """Two releases claiming same replacement digest are rejected deterministically."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    original = index.packages[0]
    digest = original.installrefs[0].verification.sha256
    assert digest is not None

    record_a = ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 10,
            "tag": "v1.0.0",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest,
                url="https://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact-a",
            ),
        ),
    )
    record_b = ReleaseRecord(
        source={
            "repository": "gelstable/gel",
            "release_id": 20,
            "tag": "v2.0.0",
            "published_at": datetime(2026, 8, 16, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest,
                url="https://github.com/gelstable/gel/releases/download/v2.0.0/artifact-b",
            ),
        ),
    )

    with pytest.raises(ContestedReplacementError) as exc_info:
        compose_indexes({key: index}, (record_a, record_b))

    assert exc_info.value.sha256 == digest
    assert "gelstable/gel-cli@v1.0.0 (release 10)" in exc_info.value.sources
    assert "gelstable/gel@v2.0.0 (release 20)" in exc_info.value.sources
    assert str(exc_info.value) == (
        f"contested replacement digest {digest}: claimed by "
        "gelstable/gel-cli@v1.0.0 (release 10), gelstable/gel@v2.0.0 (release 20)"
    )


def test_cross_record_duplicate_claims_are_order_independent() -> None:
    """The contested replacement error message is identical across input orders."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    original = index.packages[0]
    digest = original.installrefs[0].verification.sha256
    assert digest is not None

    record_a = ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 10,
            "tag": "v1.0.0",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest,
                url="https://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact-a",
            ),
        ),
    )
    record_b = ReleaseRecord(
        source={
            "repository": "gelstable/gel",
            "release_id": 20,
            "tag": "v2.0.0",
            "published_at": datetime(2026, 8, 16, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest,
                url="https://github.com/gelstable/gel/releases/download/v2.0.0/artifact-b",
            ),
        ),
    )

    with pytest.raises(ContestedReplacementError) as exc_ab:
        compose_indexes({key: index}, (record_a, record_b))

    with pytest.raises(ContestedReplacementError) as exc_ba:
        compose_indexes({key: index}, (record_b, record_a))

    assert str(exc_ab.value) == str(exc_ba.value)


def test_cross_record_identical_urls_still_rejected() -> None:
    """Duplicate replacement claims are rejected even if URLs happen to match."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    original = index.packages[0]
    digest = original.installrefs[0].verification.sha256
    assert digest is not None

    shared_url = (
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact-same"
    )
    record_1 = ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 101,
            "tag": "v1.0.0",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(Replacement(sha256=digest, url=shared_url),),
    )
    record_2 = ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 102,
            "tag": "v1.0.0",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(Replacement(sha256=digest, url=shared_url),),
    )

    with pytest.raises(ContestedReplacementError) as exc:
        compose_indexes({key: index}, (record_1, record_2))
    assert exc.value.sha256 == digest


def test_cross_record_conflict_does_not_partially_mutate_packages() -> None:
    """No mutations are applied if any replacement digest conflict exists."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    digest_valid = index.packages[0].installrefs[0].verification.sha256
    digest_conflict = index.packages[0].installrefs[1].verification.sha256
    assert digest_valid is not None and digest_conflict is not None

    record_1 = ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 1,
            "tag": "v1",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest_valid,
                url="https://github.com/gelstable/gel-cli/releases/download/v1/valid",
            ),
            Replacement(
                sha256=digest_conflict,
                url="https://github.com/gelstable/gel-cli/releases/download/v1/conflict-1",
            ),
        ),
    )
    record_2 = ReleaseRecord(
        source={
            "repository": "gelstable/gel",
            "release_id": 2,
            "tag": "v2",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest_conflict,
                url="https://github.com/gelstable/gel/releases/download/v2/conflict-2",
            ),
        ),
    )

    from gel_registry.render.compose import (
        apply_replacements,
        copy_bootstrap_packages,
        index_mirror_references,
    )

    mirror = index_mirror_references({key: index})
    packages = copy_bootstrap_packages({key: index})
    original_serialized = packages[key][
        ("package", "edgedb-cli", index.packages[0].version)
    ][0]

    with pytest.raises(ContestedReplacementError):
        apply_replacements(packages, mirror, (record_1, record_2))

    assert (
        packages[key][("package", "edgedb-cli", index.packages[0].version)][0]
        == original_serialized
    )


def test_valid_replacements_in_different_records_succeed() -> None:
    """Distinct release records can rescue different digests in the same run."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    original = index.packages[0]
    digest_1 = original.installrefs[0].verification.sha256
    digest_2 = original.installrefs[1].verification.sha256
    assert digest_1 is not None and digest_2 is not None

    record_1 = ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 1,
            "tag": "v1",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest_1,
                url="https://github.com/gelstable/gel-cli/releases/download/v1/artifact-1",
            ),
        ),
    )
    record_2 = ReleaseRecord(
        source={
            "repository": "gelstable/gel-cli",
            "release_id": 2,
            "tag": "v2",
            "published_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
        replacements=(
            Replacement(
                sha256=digest_2,
                url="https://github.com/gelstable/gel-cli/releases/download/v2/artifact-2",
            ),
        ),
    )

    output = compose_indexes({key: index}, (record_1, record_2))
    rescued = next(
        package
        for package in PackageIndex.model_validate_json(output[key]).packages
        if package.basename == original.basename
        and package.version == original.version
        and package.slot == original.slot
    )
    assert (
        rescued.installrefs[0].ref
        == "https://github.com/gelstable/gel-cli/releases/download/v1/artifact-1"
    )
    assert (
        rescued.installrefs[1].ref
        == "https://github.com/gelstable/gel-cli/releases/download/v2/artifact-2"
    )


def test_absent_mirror_digest_fails() -> None:
    """A replacement claiming an unknown digest fails with absent message."""
    repo_root = Path(__file__).parents[1]
    key = ("stable", "x86_64-unknown-linux-musl")
    index = PackageIndex.model_validate_json(
        (repo_root / "bootstrap" / "stable-x86_64-unknown-linux-musl.json").read_bytes()
    )
    unknown_digest = "f" * 64
    r = record(
        replacements=(
            Replacement(
                sha256=unknown_digest,
                url=PREFIX + "unknown",
            ),
        )
    )
    with pytest.raises(
        RenderError, match=f"replacement digest is absent: {unknown_digest}"
    ):
        compose_indexes({key: index}, (r,))


def test_ambiguous_mirror_digest_fails() -> None:
    """A replacement matching multiple bootstrap references fails as ambiguous."""
    key = ("stable", "x86_64-unknown-linux-musl")
    shared_digest = "d" * 64
    p1 = with_github_reference(
        PackageEntry.model_validate(
            {
                "basename": "pkg1",
                "name": "pkg1",
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
                "build_date": "2026-08-15T00:00:00Z",
                "architecture": "x86_64",
                "slot": "",
                "installref": "https://packages.geldata.com/1",
                "installrefs": [
                    {
                        "ref": "https://packages.geldata.com/1",
                        "type": "application/octet-stream",
                        "verification": {
                            "size": 1,
                            "sha256": shared_digest,
                            "blake2b": "b" * 128,
                        },
                    }
                ],
            }
        )
    )
    p2 = with_github_reference(
        PackageEntry.model_validate(
            {
                "basename": "pkg2",
                "name": "pkg2",
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
                "build_date": "2026-08-15T00:00:00Z",
                "architecture": "x86_64",
                "slot": "",
                "installref": "https://packages.geldata.com/2",
                "installrefs": [
                    {
                        "ref": "https://packages.geldata.com/2",
                        "type": "application/octet-stream",
                        "verification": {
                            "size": 2,
                            "sha256": shared_digest,
                            "blake2b": "c" * 128,
                        },
                    }
                ],
            }
        )
    )
    bootstrap = {key: PackageIndex(packages=(p1, p2))}
    r = record(
        replacements=(Replacement(sha256=shared_digest, url=PREFIX + "ambiguous"),)
    )
    with pytest.raises(
        RenderError, match=f"replacement digest is ambiguous: {shared_digest}"
    ):
        compose_indexes(bootstrap, (r,))


def test_index_fragment_requires_at_least_one_package() -> None:
    """IndexFragment rejects empty packages."""
    with pytest.raises(ValidationError):
        IndexFragment(
            channel="stable", platform="x86_64-unknown-linux-musl", packages=()
        )


def test_release_manifest_effective_nonempty_invariant() -> None:
    """ReleaseManifest rejects completely empty changes or duplicate index fragments."""
    with pytest.raises(
        ValidationError,
        match="release manifest must contain a replacement or index",
    ):
        ReleaseManifest(replacements=(), indexes=())

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
                "build_date": "2026-08-15T00:00:00Z",
                "architecture": "x86_64",
                "slot": "",
                "installref": PREFIX + "cli",
                "installrefs": [
                    {
                        "ref": PREFIX + "cli",
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
    frag1 = IndexFragment(
        channel="stable", platform="x86_64-unknown-linux-musl", packages=(entry,)
    )
    frag2 = IndexFragment(
        channel="stable", platform="x86_64-unknown-linux-musl", packages=(entry,)
    )
    with pytest.raises(
        ValidationError, match="release manifest contains duplicate index fragments"
    ):
        ReleaseManifest(indexes=(frag1, frag2))


@pytest.mark.parametrize(
    "bad_repo",
    [
        "../evil",
        "evil/..",
        "./evil",
        "evil/.",
        "evil/../evil",
        "evil//evil",
        "/evil",
        "evil/",
        "evil",
        "evil/evil/evil",
        "evil\\evil/foo",
        "evil/foo\\bar",
        "evil/\x00evil",
        "evil/foo bar",
        "evil/foo\nbar",
        "evil/foo\tbar",
    ],
)
def test_validate_repository_name_rejects_traversal_and_invalid_components(
    bad_repo: str,
) -> None:
    """Repository names must be safe, nonempty path components."""
    with pytest.raises(ValueError):
        validate_repository_name(bad_repo)

    with pytest.raises(ValidationError):
        ReleaseSource(
            repository=bad_repo,
            release_id=1,
            tag="v1.0.0",
            published_at=datetime(2026, 8, 15, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "bad_tag",
    [
        "",
        ".",
        "..",
        "v1/../v2",
        "v1/v2",
        "v1\\v2",
        "v1\x00bad",
        "v1 bad",
    ],
)
def test_release_source_rejects_invalid_tag(bad_tag: str) -> None:
    """Release tags must not contain traversal, separators, or whitespace."""
    with pytest.raises(ValidationError):
        ReleaseSource(
            repository="gelstable/gel-cli",
            release_id=1,
            tag=bad_tag,
            published_at=datetime(2026, 8, 15, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "bad_url",
    [
        # Path traversal
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/../v2/artifact",
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/./artifact",
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact/..",
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/../artifact",
        # Query parameters
        (
            "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/"
            "artifact?alternate=1"
        ),
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact?",
        # Fragment identifier
        (
            "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/"
            "artifact#fragment"
        ),
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact#",
        # Wrong scheme / host / port / userinfo
        "http://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact",
        "https://evil.com/gelstable/gel-cli/releases/download/v1.0.0/artifact",
        (
            "https://user:pass@github.com/gelstable/gel-cli/releases/download/"
            "v1.0.0/artifact"
        ),
        "https://github.com:8080/gelstable/gel-cli/releases/download/v1.0.0/artifact",
        # Wrong repo or tag
        "https://github.com/attacker/gel-cli/releases/download/v1.0.0/artifact",
        "https://github.com/gelstable/other-repo/releases/download/v1.0.0/artifact",
        "https://github.com/gelstable/gel-cli/releases/download/v2.0.0/artifact",
        # Malformed path structure
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/artifact/",
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0//artifact",
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/nested/artifact",
        "https://github.com/gelstable/gel-cli/releases/download/v1.0.0/",
        "https://github.com/gelstable/gel-cli/releases/v1.0.0/artifact",
    ],
)
def test_release_record_rejects_adversarial_urls(bad_url: str) -> None:
    """Release URLs must be structurally bound without traversal, query, or fragment."""
    with pytest.raises(ValueError):
        validate_release_url(bad_url, "gelstable/gel-cli", "v1.0.0")

    with pytest.raises(ValidationError):
        ReleaseRecord(
            source={
                "repository": "gelstable/gel-cli",
                "release_id": 1,
                "tag": "v1.0.0",
                "published_at": datetime(2026, 8, 15, tzinfo=UTC),
            },
            replacements=(
                Replacement(
                    sha256="a" * 64,
                    url=bad_url,
                ),
            ),
        )
