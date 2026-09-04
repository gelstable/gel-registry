"""Deterministic selection from frozen rescue indexes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from gel_registry.__main__ import main
from gel_registry.constants import (
    CAPTURE_ID,
    CLI_PLATFORMS,
    LEGACY_PLATFORMS,
    RESCUE_ORIGIN,
    capture_urls,
)
from gel_registry.digest import canonical_json, hash_bytes
from gel_registry.github.models import GitHubError
from gel_registry.github.transport import API_HEADERS
from gel_registry.rescue.indexes import RescueCaptureManifest
from gel_registry.rescue.models import RescueIndexCapture, RescuePlan
from gel_registry.rescue.selection import (
    GitHubTagSource,
    GitTagSource,
    UpstreamTag,
    _extension,
    _IndexedPackage,
    _ls_versions,
    artifact_name,
    plan_rescue,
)


def _package(version: str, scm_revision: str = "a" * 9) -> dict[str, object]:
    """One index record shaped like the real archive.

    ``scm_revision`` is the abbreviated source commit a build came from
    and is what ties a package to a tag; ``build_hash`` identifies the
    build and differs per platform for the same release.
    """

    return {
        "basename": "gel-cli",
        "name": "gel-cli",
        "version": version,
        "version_details": {
            "major": 7,
            "minor": 9,
            "patch": 0,
            "prerelease": [],
            "metadata": {
                "scm_revision": scm_revision,
                "build_hash": version.split("+")[-1] if "+" in version else "0" * 7,
                "build_revision": "202509152015",
            },
        },
        "version_key": version,
        "revision": "1",
        "build_date": "2026-08-31T12:00:00+00:00",
        "architecture": "x86_64",
        "slot": "",
        "tags": {},
        "installref": "../artifacts/gel-cli",
        "installrefs": [
            {
                "ref": "../artifacts/gel-cli",
                "type": "application/x-pie-executable",
                "encoding": "identity",
                "verification": {"size": 12, "sha256": "a" * 64, "blake2b": "b" * 128},
            }
        ],
    }


def _capture(repo: Path, packages: list[dict[str, object]]) -> None:
    _capture_matrix(
        repo,
        {
            (channel, platform): packages
            for channel, platform, _ in capture_urls(origin=RESCUE_ORIGIN)
        },
    )


def _capture_matrix(
    repo: Path, packages_by_index: dict[tuple[str, str], list[dict[str, object]]]
) -> None:
    """Write a capture whose contents can vary by channel and platform."""

    root = repo / "upstream" / "packages.edgedb.com" / CAPTURE_ID
    (root / "indexes").mkdir(parents=True)
    timestamp = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    evidence: list[RescueIndexCapture] = []
    for channel, platform, source_url in capture_urls(origin=RESCUE_ORIGIN):
        raw_packages = packages_by_index.get((channel, platform), [])
        packages: list[dict[str, object]] = []
        for pkg in raw_packages:
            p = json.loads(json.dumps(pkg))
            for ref in p.get("installrefs", []):
                verif = ref.get("verification", {})
                if (
                    platform == "x86_64-unknown-linux-musl"
                    and p.get("version") == "7.9.0"
                    and verif.get("sha256") == "a" * 64
                ):
                    pass
                else:
                    seed = (
                        f"{p.get('name')}:{p.get('version')}:"
                        f"{channel}:{platform}:{ref.get('ref')}:{verif.get('sha256')}"
                    )
                    verif["sha256"] = hashlib.sha256(seed.encode()).hexdigest()
            packages.append(p)
        body = canonical_json({"packages": packages})
        digest = hash_bytes(body)
        relative = f"indexes/{channel}-{platform}.json"
        (root / relative).write_bytes(body)
        evidence.append(
            RescueIndexCapture(
                source_url=source_url,
                channel=channel,
                platform=platform,
                captured_at=timestamp,
                byte_size=digest.size,
                sha256=digest.sha256,
            )
        )
    manifest = RescueCaptureManifest.create(timestamp, tuple(evidence))
    (root / "capture.json").write_bytes(canonical_json(manifest))


def _server_package(version: str, scm_revision: str) -> dict[str, object]:
    package = _package(f"{version}+{scm_revision}", scm_revision)
    package.update(
        {
            "basename": "gel-server",
            "name": f"gel-server-{version.split('.')[0]}",
            "slot": version.split(".")[0],
            "version_details": {
                "major": int(version.split(".")[0]),
                "minor": int(version.split(".")[1]),
                "patch": None,
                "prerelease": [],
                "metadata": {
                    "scm_revision": scm_revision,
                    "build_hash": scm_revision,
                    "build_revision": "202509152015",
                },
            },
            "installrefs": [
                {
                    "ref": "/archive/server.tar.gz",
                    "type": "application/x-tar",
                    "encoding": "gzip",
                    "verification": {
                        "size": 12,
                        "sha256": "a" * 64,
                        "blake2b": "b" * 128,
                    },
                }
            ],
        }
    )
    return package


def _postgis_package(slot: str, version: str = "3.5.1+c9b5460") -> dict[str, object]:
    package = _package(version)
    package.update(
        {
            "basename": "gel-server-7-ext-postgis",
            "name": "gel-server-7-ext-postgis",
            "slot": slot,
            "tags": {"extension": "postgis", "server_slot": slot},
            "installrefs": [
                {
                    "ref": "/archive/postgis.tar.zst",
                    "type": "application/x-tar",
                    "encoding": "zstd",
                    "verification": {
                        "size": 12,
                        "sha256": "a" * 64,
                        "blake2b": "b" * 128,
                    },
                },
                {
                    "ref": "/archive/postgis.zip",
                    "type": "application/zip",
                    "encoding": "identity",
                    "verification": {
                        "size": 13,
                        "sha256": "c" * 64,
                        "blake2b": "b" * 128,
                    },
                },
            ],
        }
    )
    return package


def test_cli_plan_selects_latest_complete_tags_with_canonical_assets(
    tmp_path: Path,
) -> None:
    """Coverage follows the capture; naming stays canonical.

    The fixture indexes the CLI on every platform, so every platform is
    expected here -- not a fixed CLI_PLATFORMS list.
    """

    _capture(tmp_path, [_package("7.9.0")])

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product=None,
        version=None,
        slot=None,
        tags=(UpstreamTag("v7.9.0", "a" * 40),),
    )

    assert [(release.repository, release.tag) for release in plan.releases] == [
        ("gelstable/gel-cli", "v7.9.0")
    ]
    assets = plan.releases[0].assets
    assert [asset.destination_name for asset in assets] == sorted(
        artifact_name("gel-cli", platform) for platform in LEGACY_PLATFORMS
    )
    identity = next(
        asset
        for asset in assets
        if asset.destination_name == "gel-cli-x86_64-unknown-linux-musl"
    )
    assert (
        identity.source_url == "https://packages.edgedb.com/archive/artifacts/gel-cli"
    )
    assert identity.expected_sha256 == "a" * 64


def test_language_server_version_order_accepts_legacy_dev_builds() -> None:
    """Using strict three-part SemVer must not reject valid captured LS builds."""

    records = tuple(
        SimpleNamespace(
            channel="nightly",
            entry=SimpleNamespace(basename="edgedb-ls", version=version),
        )
        for version in ("8.0-dev.9812+47a730b", "8.0-dev.9813+0000000")
    )

    assert _ls_versions(cast(tuple[_IndexedPackage, ...], records)) == (
        "8.0-dev.9813+0000000",
        "8.0-dev.9812+47a730b",
    )


def test_github_tag_source_paginates_and_peels_annotated_tags() -> None:
    """Using the tag-object SHA breaks server build-prefix matching."""

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if (
            request.url.path.endswith("/refs/tags")
            and request.url.params["page"] == "1"
        ):
            return httpx.Response(
                200,
                json=(
                    [
                        {
                            "ref": "refs/tags/v6.11",
                            "object": {"type": "tag", "sha": "tag-object"},
                        }
                    ]
                    + [
                        {
                            "ref": f"refs/tags/other-{index}",
                            "object": {"type": "commit", "sha": "b" * 40},
                        }
                        for index in range(99)
                    ]
                ),
            )
        if request.url.path.endswith("/git/tags/tag-object"):
            return httpx.Response(
                200, json={"object": {"type": "commit", "sha": "66a1377abcdef"}}
            )
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tags = GitHubTagSource(client).tags("geldata/gel")

        # Discovery pages the refs but peels nothing: the annotated tag's
        # SHA is fetched only when it is read, and reading it must happen
        # while the client is still open.
        assert not any("/git/tags/" in request for request in requests)
        assert UpstreamTag("v6.11", "66a1377abcdef") in tags

    assert sum("/git/tags/" in request for request in requests) == 1
    assert any("page=2" in request for request in requests)


def test_tag_discovery_peels_only_the_tags_a_plan_actually_selects() -> None:
    """Peeling every annotated tag spent the whole hourly API budget.

    ``geldata/gel-cli`` carries thousands of annotated tags while a plan
    keeps a handful, so an eager peel cost one request per tag and 403ed
    partway through before any release was chosen.
    """

    peeled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/git/tags/" in request.url.path:
            sha = request.url.path.rsplit("/", 1)[-1]
            peeled.append(sha)
            return httpx.Response(200, json={"object": {"type": "commit", "sha": sha}})
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[
                    {
                        "ref": f"refs/tags/v1.0.{index}",
                        "object": {"type": "tag", "sha": f"sha{index}"},
                    }
                    for index in range(100)
                ],
            )
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tags = GitHubTagSource(client).tags("geldata/gel-cli")
        assert len(tags) == 100
        assert peeled == []
        # Filtering reads names only; only the chosen tag costs a request.
        chosen = next(tag for tag in tags if tag.name == "v1.0.7")
        assert chosen.commit == "sha7"
        assert peeled == ["sha7"]
        # The resolved SHA is cached, not re-fetched.
        assert chosen.commit == "sha7"
        assert peeled == ["sha7"]


def test_server_one_off_requires_hash_matched_complete_platform_matrix(
    tmp_path: Path,
) -> None:
    """A package whose source commit is not the tag's must not be selected."""

    package = _package("6.11+66a1377", "66a1377")
    package.update(
        {
            "basename": "gel-server",
            "name": "gel-server-6",
            "slot": "6",
            "version_details": {
                "major": 6,
                "minor": 11,
                "patch": None,
                "prerelease": [],
                "metadata": {
                    "scm_revision": "66a1377",
                    "build_hash": "deadbee",
                    "build_revision": "202509152015",
                },
            },
            "installrefs": [
                {
                    "ref": "/archive/server.tar.gz",
                    "type": "application/x-tar",
                    "encoding": "gzip",
                    "verification": {
                        "size": 12,
                        "sha256": "a" * 64,
                        "blake2b": "b" * 128,
                    },
                }
            ],
        }
    )
    _capture(tmp_path, [package])

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product="server",
        version="6.11",
        slot=None,
        tags=(UpstreamTag("v6.11", "66a1377" + "0" * 33),),
    )

    assert plan.releases[0].tag == "v6.11"
    assert len(plan.releases[0].assets) == 8


def test_postgis_one_off_includes_all_stable_references_with_safe_names(
    tmp_path: Path,
) -> None:
    """Dropping an install reference would silently lose a PostGIS artifact."""

    package = _package("3.5.1+c9b5460")
    package.update(
        {
            "basename": "gel-server-7-ext-postgis",
            "name": "gel-server-7-ext-postgis",
            "tags": {"extension": "postgis", "server_slot": "7"},
            "installrefs": [
                {
                    "ref": "/archive/postgis.tar.zst",
                    "type": "application/x-tar",
                    "encoding": "zstd",
                    "verification": {
                        "size": 12,
                        "sha256": "a" * 64,
                        "blake2b": "b" * 128,
                    },
                },
                {
                    "ref": "/archive/postgis.zip",
                    "type": "application/zip",
                    "encoding": "identity",
                    "verification": {
                        "size": 13,
                        "sha256": "c" * 64,
                        "blake2b": "b" * 128,
                    },
                },
            ],
        }
    )
    _capture(tmp_path, [package])

    plan = plan_rescue(
        tmp_path, capture=CAPTURE_ID, product="postgis", version=None, slot="7"
    )

    assert plan.releases[0].tag == "legacy-gel-server-7-ext-postgis"
    assert len(plan.releases[0].assets) == 16
    assert all(
        "3.5.1+c9b5460" in asset.destination_name for asset in plan.releases[0].assets
    )


def test_bulk_server_selects_the_newest_three_complete_minors_per_major(
    tmp_path: Path,
) -> None:
    """A fourth valid minor must not displace one of each major's newest three."""

    versions = ("5.1", "5.2", "5.3", "5.4", "6.1", "6.2", "6.3", "6.4")
    packages = {
        ("stable", platform): [
            _server_package(version, version.replace(".", "a")) for version in versions
        ]
        for platform in LEGACY_PLATFORMS
    }
    _capture_matrix(tmp_path, packages)
    tags = tuple(
        UpstreamTag(f"v{version}", version.replace(".", "a") + "0" * 38)
        for version in versions
    )

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product=None,
        version=None,
        slot=None,
        tags=tags,
    )

    assert [release.tag for release in plan.releases] == [
        "v5.2",
        "v5.3",
        "v5.4",
        "v6.2",
        "v6.3",
        "v6.4",
    ]


@pytest.mark.parametrize(
    ("packages", "message"),
    [
        (
            {
                ("stable", platform): [_server_package("6.11", "wrong")]
                for platform in LEGACY_PLATFORMS
            },
            "no indexed platform records",
        ),
        (
            {
                ("stable", platform): [_server_package("6.11", "66a1377")]
                for platform in LEGACY_PLATFORMS
            }
            | {
                ("stable", LEGACY_PLATFORMS[0]): [
                    _server_package("6.11", "66a1377"),
                    _server_package("6.11", "66a"),
                ]
            },
            "ambiguous platform record",
        ),
    ],
    ids=("mismatched-build-hash", "ambiguous-platform"),
)
def test_server_one_off_rejects_unverifiable_platform_records(
    tmp_path: Path,
    packages: dict[tuple[str, str], list[dict[str, object]]],
    message: str,
) -> None:
    """Unverifiable or ambiguous bytes are still rejected.

    A build hash that matches no tag leaves nothing to rescue, and two
    records for one platform make the choice of bytes ambiguous. A
    platform the archive simply lacks is neither -- see
    ``test_server_one_off_keeps_the_platforms_the_archive_actually_has``.
    """

    _capture_matrix(tmp_path, packages)

    with pytest.raises(ValueError, match=message):
        plan_rescue(
            tmp_path,
            capture=CAPTURE_ID,
            product="server",
            version="6.11",
            slot=None,
            tags=(UpstreamTag("v6.11", "66a1377" + "0" * 33),),
        )


def test_bulk_ls_rescues_the_newest_build_on_the_platforms_it_has(
    tmp_path: Path,
) -> None:
    """The archive decides coverage, so a narrower newest build still wins.

    packages.edgedb.com never shipped a Windows language server, and its
    per-build platform set varies; requiring the union of all nightly LS
    platforms rejected the newest build for being narrower than an older
    one.
    """

    newer = _package("8.0-dev.9813+0000000")
    newer.update({"basename": "edgedb-ls", "name": "edgedb-ls"})
    older = _package("8.0-dev.9812+47a730b")
    older.update({"basename": "edgedb-ls", "name": "edgedb-ls"})
    _capture_matrix(
        tmp_path,
        {
            ("nightly", "x86_64-unknown-linux-gnu"): [newer, older],
            ("nightly", "aarch64-apple-darwin"): [older],
        },
    )

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product=None,
        version=None,
        slot=None,
    )

    assert [(release.repository, release.tag) for release in plan.releases] == [
        ("gelstable/gel", "gel-ls-v8.0-dev.9813+0000000")
    ]
    # Only the one platform that actually carries the newest build.
    assert len(plan.releases[0].assets) == 1


@pytest.mark.parametrize(
    "packages",
    [
        {
            ("nightly", "x86_64-unknown-linux-gnu"): [
                {**_package("8.0-dev.9812+47a730b"), "basename": "edgedb-ls"},
                {
                    **_package("8.0-dev.9812+47a730b"),
                    "basename": "edgedb-ls",
                    "slot": "second-copy",
                },
            ]
        },
    ],
    ids=("ambiguous-platform",),
)
def test_ls_one_off_rejects_ambiguous_nightly_records(
    tmp_path: Path, packages: dict[tuple[str, str], list[dict[str, object]]]
) -> None:
    """Two records for one platform still make the bytes ambiguous."""

    _capture_matrix(tmp_path, packages)

    with pytest.raises(ValueError, match="ambiguous platform record"):
        plan_rescue(
            tmp_path,
            capture=CAPTURE_ID,
            product="ls",
            version="8.0-dev.9812+47a730b",
            slot=None,
        )


def test_bulk_postgis_uses_stable_records_and_excludes_prerelease_slots(
    tmp_path: Path,
) -> None:
    """Testing records and dev/alpha/beta/RC slots must never produce releases."""

    _capture_matrix(
        tmp_path,
        {
            ("stable", "x86_64-unknown-linux-gnu"): [_postgis_package("7")],
            ("stable", "aarch64-apple-darwin"): [_postgis_package("7")],
            ("stable", "x86_64-unknown-linux-musl"): [
                _postgis_package("7-dev"),
                _postgis_package("7-alpha"),
                _postgis_package("7-beta"),
                _postgis_package("7-RC"),
            ],
            ("testing", "x86_64-unknown-linux-gnu"): [_postgis_package("8")],
        },
    )

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product=None,
        version=None,
        slot=None,
    )

    postgis = [release for release in plan.releases if "postgis" in release.repository]
    assert [(release.repository, release.tag) for release in postgis] == [
        ("gelstable/gel-postgis", "legacy-gel-server-7-ext-postgis")
    ]
    # Package version and platform once each, then the real extension --
    # not the upstream filename appended to a name that already repeats it.
    assert {asset.destination_name for asset in postgis[0].assets} == {
        "gel-server-7-ext-postgis-3.5.1+c9b5460-aarch64-apple-darwin.tar.zst",
        "gel-server-7-ext-postgis-3.5.1+c9b5460-aarch64-apple-darwin.zip",
        "gel-server-7-ext-postgis-3.5.1+c9b5460-x86_64-unknown-linux-gnu.tar.zst",
        "gel-server-7-ext-postgis-3.5.1+c9b5460-x86_64-unknown-linux-gnu.zip",
    }


def _command_capture(repo: Path) -> None:
    """Create the smallest capture that supports every rescue-plan CLI mode."""

    ls = _package("8.0-dev.9812+47a730b")
    ls.update({"basename": "edgedb-ls", "name": "edgedb-ls"})
    packages: dict[tuple[str, str], list[dict[str, object]]] = {}
    for platform in CLI_PLATFORMS:
        packages.setdefault(("stable", platform), []).append(_package("7.9.0"))
    for platform in LEGACY_PLATFORMS:
        packages.setdefault(("stable", platform), []).extend(
            [_server_package("6.11", "66a1377"), _postgis_package("7")]
        )
    for platform in ("x86_64-unknown-linux-gnu", "aarch64-apple-darwin"):
        packages[("nightly", platform)] = [ls]
    _capture_matrix(repo, packages)


def _command_tags(_: object, repository: str) -> tuple[UpstreamTag, ...]:
    if repository == "geldata/gel-cli":
        return (UpstreamTag("v7.9.0", "a" * 40),)
    if repository == "geldata/gel":
        return (UpstreamTag("v6.11", "66a1377" + "0" * 33),)
    raise AssertionError(f"unexpected tag repository: {repository}")


def test_rescue_plan_bulk_cli_prints_only_canonical_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Replacing output serialization or adding status text breaks automation."""

    _command_capture(tmp_path)
    monkeypatch.setattr(GitTagSource, "tags", _command_tags)

    result = main(
        ["rescue-plan", "--repo", str(tmp_path), "--capture", CAPTURE_ID, "--bulk"]
    )

    captured = capsys.readouterr()
    plan = RescuePlan.model_validate_json(captured.out.encode())
    assert result == 0
    assert captured.err == ""
    assert captured.out.encode() == canonical_json(plan)
    assert {(release.repository, release.tag) for release in plan.releases} == {
        ("gelstable/gel-cli", "v7.9.0"),
        ("gelstable/gel", "v6.11"),
        ("gelstable/gel", "gel-ls-v8.0-dev.9812+47a730b"),
        ("gelstable/gel-postgis", "legacy-gel-server-7-ext-postgis"),
    }


@pytest.mark.parametrize(
    ("arguments", "destination"),
    [
        (("--product", "cli", "--version", "7.9.0"), ("gelstable/gel-cli", "v7.9.0")),
        (("--product", "server", "--version", "6.11"), ("gelstable/gel", "v6.11")),
        (
            ("--product", "ls", "--version", "8.0-dev.9812+47a730b"),
            ("gelstable/gel", "gel-ls-v8.0-dev.9812+47a730b"),
        ),
        (
            ("--product", "postgis", "--slot", "7"),
            ("gelstable/gel-postgis", "legacy-gel-server-7-ext-postgis"),
        ),
    ],
    ids=("cli", "server", "ls", "postgis"),
)
def test_rescue_plan_one_off_cli_modes_print_a_release_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    destination: tuple[str, str],
) -> None:
    """Each documented one-off command must succeed at the command boundary."""

    _command_capture(tmp_path)
    monkeypatch.setattr(GitTagSource, "tags", _command_tags)

    result = main(
        ["rescue-plan", "--repo", str(tmp_path), "--capture", CAPTURE_ID, *arguments]
    )

    captured = capsys.readouterr()
    plan = RescuePlan.model_validate_json(captured.out.encode())
    assert result == 0
    assert captured.err == ""
    assert [(release.repository, release.tag) for release in plan.releases] == [
        destination
    ]


def test_bulk_server_rescues_a_narrow_minor_and_keeps_every_product(
    tmp_path: Path,
) -> None:
    """A tag captured on one platform is rescued with that one platform.

    packages.edgedb.com never published Windows server builds, so a fixed
    eight-platform requirement rejected every server tag and produced a
    plan with no server releases at all.
    """

    complete = ("6.1", "6.2", "6.3")
    ls = _package("8.0-dev.9812+47a730b")
    ls.update({"basename": "edgedb-ls", "name": "edgedb-ls"})
    packages: dict[tuple[str, str], list[dict[str, object]]] = {}
    for platform in CLI_PLATFORMS:
        packages.setdefault(("stable", platform), []).append(_package("7.9.0"))
    for platform in LEGACY_PLATFORMS:
        packages.setdefault(("stable", platform), []).extend(
            [
                _server_package(version, version.replace(".", "a"))
                for version in complete
            ]
        )
        packages[("stable", platform)].append(_postgis_package("7"))
    # v6.4 was only ever captured on one platform; it is still rescued.
    packages[("stable", LEGACY_PLATFORMS[0])].append(_server_package("6.4", "6a4"))
    for platform in ("x86_64-unknown-linux-gnu", "aarch64-apple-darwin"):
        packages[("nightly", platform)] = [ls]
    _capture_matrix(tmp_path, packages)
    tags = (
        UpstreamTag("v7.9.0", "a" * 40),
        UpstreamTag("v6.4", "6a4" + "0" * 37),
        *(
            UpstreamTag(f"v{version}", version.replace(".", "a") + "0" * 37)
            for version in complete
        ),
    )

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product=None,
        version=None,
        slot=None,
        tags=tags,
    )

    assert {(release.repository, release.tag) for release in plan.releases} == {
        ("gelstable/gel-cli", "v7.9.0"),
        # The three newest minors: 6.4 counts, so 6.1 falls off the end.
        ("gelstable/gel", "v6.2"),
        ("gelstable/gel", "v6.3"),
        ("gelstable/gel", "v6.4"),
        ("gelstable/gel", "gel-ls-v8.0-dev.9812+47a730b"),
        ("gelstable/gel-postgis", "legacy-gel-server-7-ext-postgis"),
    }


def test_server_one_off_keeps_the_platforms_the_archive_actually_has(
    tmp_path: Path,
) -> None:
    """One captured platform is a release, not a failure."""

    _capture_matrix(
        tmp_path,
        {("stable", LEGACY_PLATFORMS[0]): [_server_package("6.4", "6a4")]},
    )

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product="server",
        version="6.4",
        slot=None,
        tags=(UpstreamTag("v6.4", "6a4" + "0" * 37),),
    )

    assert [release.tag for release in plan.releases] == ["v6.4"]
    assert {
        asset.destination_name.rsplit("-", 1)[0] for asset in plan.releases[0].assets
    } == {f"gel-server-{LEGACY_PLATFORMS[0]}".rsplit("-", 1)[0]}


def test_server_one_off_fails_loudly_when_nothing_was_captured(
    tmp_path: Path,
) -> None:
    """A version with no records at all is still an error, not an empty plan."""

    _capture_matrix(tmp_path, {})

    with pytest.raises(ValueError, match="no indexed platform records"):
        plan_rescue(
            tmp_path,
            capture=CAPTURE_ID,
            product="server",
            version="6.4",
            slot=None,
            tags=(UpstreamTag("v6.4", "6a4" + "0" * 37),),
        )


def test_cli_assets_are_direct_source_artifacts_with_required_sha256(
    tmp_path: Path,
) -> None:
    """Every planned asset is a direct source artifact with an exact SHA-256."""

    _capture(tmp_path, [_package("7.9.0")])

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product="cli",
        version="7.9.0",
        slot=None,
        tags=(UpstreamTag("v7.9.0", "a" * 40),),
    )

    assets = plan.releases[0].assets
    assert len(assets) == len(LEGACY_PLATFORMS)
    assert not any(asset.destination_name.endswith(".zst") for asset in assets)
    assert all(asset.expected_sha256 for asset in assets)


def test_tag_discovery_uses_the_shared_github_transport_settings() -> None:
    """Anonymous, header-less tag reads get rate-limited off GitHub."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/git/tags/tag-object"):
            return httpx.Response(200, json={"object": {"type": "commit", "sha": "c"}})
        if request.url.params.get("page") == "1":
            return httpx.Response(
                200,
                json=[
                    {
                        "ref": "refs/tags/v6.11",
                        "object": {"type": "tag", "sha": "tag-object"},
                    }
                ],
            )
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tags = GitHubTagSource(client).tags("geldata/gel")
        # Reading the SHA is what issues the peel, so force it here.
        assert tags[0].commit == "c"

    assert len(seen) == 2
    for request in seen:
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        assert request.headers["user-agent"] == API_HEADERS["User-Agent"]
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 60.0,
            "write": 60.0,
            "pool": 60.0,
        }


_LIGHTWEIGHT_SHA = "b" * 40
_LS_REMOTE = (
    "70345d80d2f74cfdeb2802d99dda4a9f4cbc9371\trefs/tags/v1.0.0\n"
    "dc8a26de18c17ee8199e335017832030a9002f41\trefs/tags/v1.0.0^{}\n"
    "4cd2718eba18dad9a0701489536fca8d065144d8\trefs/tags/v7.9.0\n"
    "feb7f8ccfa0695f7c2c048ebf707817feacdff47\trefs/tags/v7.9.0^{}\n"
    f"{_LIGHTWEIGHT_SHA}\trefs/tags/lightweight\n"
)


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == 60.0
        env = kwargs["env"]
        assert isinstance(env, dict)
        # A missing or private remote must fail, never sit on a prompt.
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_git_tag_source_reads_every_tag_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REST path cost a request per page plus one per annotated tag."""

    calls = _fake_git(monkeypatch, stdout=_LS_REMOTE)

    tags = GitTagSource().tags("geldata/gel-cli")

    assert calls == [
        ["git", "ls-remote", "--tags", "--", "https://github.com/geldata/gel-cli"]
    ]
    assert [tag.name for tag in tags] == ["lightweight", "v1.0.0", "v7.9.0"]


def test_git_tag_source_prefers_the_peeled_commit_of_an_annotated_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching a build against a tag object's own SHA never succeeds."""

    _fake_git(monkeypatch, stdout=_LS_REMOTE)

    tags = {tag.name: tag.commit for tag in GitTagSource().tags("geldata/gel-cli")}

    assert tags["v7.9.0"] == "feb7f8ccfa0695f7c2c048ebf707817feacdff47"
    assert tags["v1.0.0"] == "dc8a26de18c17ee8199e335017832030a9002f41"
    # A lightweight tag has no ^{} line and keeps its own SHA.
    assert tags["lightweight"] == _LIGHTWEIGHT_SHA


def test_git_tag_source_reports_a_failed_ls_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_git(
        monkeypatch,
        returncode=128,
        stderr="fatal: repository 'https://github.com/geldata/nope' not found\n",
    )

    with pytest.raises(ValueError, match="not found"):
        GitTagSource().tags("geldata/nope")


@pytest.mark.parametrize(
    "line",
    [
        "not-a-sha\trefs/tags/v1.0.0",
        _LIGHTWEIGHT_SHA + "\trefs/heads/main",
        _LIGHTWEIGHT_SHA + " refs/tags/v1.0.0",
    ],
)
def test_git_tag_source_rejects_unreadable_output(
    monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    """Silently skipping a line it cannot parse would drop a release."""

    _fake_git(monkeypatch, stdout=line + "\n")

    with pytest.raises(ValueError, match="unreadable git ls-remote line"):
        GitTagSource().tags("geldata/gel-cli")


def test_git_tag_source_refuses_a_repository_that_is_not_owner_slash_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity is interpolated into a URL, so it is validated first."""

    calls = _fake_git(monkeypatch, stdout="")

    with pytest.raises(GitHubError, match="owner/repository"):
        GitTagSource().tags("--upload-pack=touch /tmp/pwned")

    assert calls == []


def test_selection_ties_a_package_to_a_tag_by_scm_revision_not_build_hash(
    tmp_path: Path,
) -> None:
    """build_hash differs per platform, so it never matched a tag commit.

    Real records for gel-server 7.1 carry build_hash ec15259, 2e294a8 and
    3e1ee05 on three platforms while all three share scm_revision
    e0ef1b92d -- the commit tag v7.1 points at.
    """

    commit = "e0ef1b92dec1910bb40585c7a818944c4717ec29"
    packages = {}
    for index, platform in enumerate(LEGACY_PLATFORMS[:3]):
        package = _server_package("7.1", "e0ef1b92d")
        details = cast(dict[str, object], package["version_details"])
        # Each platform builds its own artifact, with its own build_hash.
        details["metadata"] = {
            "scm_revision": "e0ef1b92d",
            "build_hash": f"bui1d{index:02d}",
            "build_revision": "202512051818",
        }
        packages[("stable", platform)] = [package]
    _capture_matrix(tmp_path, packages)

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product="server",
        version="7.1",
        slot=None,
        tags=(UpstreamTag("v7.1", commit),),
    )

    assert [release.tag for release in plan.releases] == ["v7.1"]
    assert len(plan.releases[0].assets) == 3


def test_selection_takes_the_newest_rebuild_of_one_source_commit(
    tmp_path: Path,
) -> None:
    """The archive holds several builds of one release, weeks apart.

    gel-cli 7.9.0 appears as both 7.9.0+070b371 and 7.9.0+c84d665 on the
    same platform, sharing scm_revision feb7f8ccf. They are dated, not
    ambiguous, so the newest build_revision wins deterministically.
    """

    def _build(build_hash: str, build_revision: str) -> dict[str, object]:
        package = _package(f"7.9.0+{build_hash}", "feb7f8ccf")
        cast(dict[str, object], package["version_details"])["metadata"] = {
            "scm_revision": "feb7f8ccf",
            "build_hash": build_hash,
            "build_revision": build_revision,
        }
        reference = cast(list[dict[str, object]], package["installrefs"])[0]
        reference["ref"] = f"../artifacts/gel-cli-7.9.0+{build_hash}"
        return package

    older = _build("070b371", "202509152015")
    newer = _build("c84d665", "202510221744")
    _capture_matrix(tmp_path, {("stable", LEGACY_PLATFORMS[0]): [older, newer]})

    plan = plan_rescue(
        tmp_path,
        capture=CAPTURE_ID,
        product="cli",
        version="7.9.0",
        slot=None,
        tags=(UpstreamTag("v7.9.0", "feb7f8ccfa0695f7c2c048ebf707817feacdff47"),),
    )

    identity = plan.releases[0].assets[0]
    assert "c84d665" in identity.source_url
    assert "070b371" not in identity.source_url


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        # A dotted version must not be mistaken for the extension.
        ("/archive/aarch64-apple-darwin/edgedb-server-5.6+adb9e77.tar.gz", ".tar.gz"),
        (
            "/archive/x86_64-unknown-linux-gnu/gel-server-7.1+ec15259.tar.zst",
            ".tar.zst",
        ),
        ("/archive/postgis-3.5.1+c9b5460.zip", ".zip"),
        ("/archive/gel-cli-7.9.0+070b371.exe", ".exe"),
        ("/archive/gel-cli-7.9.0+070b371", ""),
    ],
)
def test_extension_reads_the_real_suffix_not_everything_after_a_dot(
    reference: str, expected: str
) -> None:
    """Splitting at the first dot named an asset ...darwin.6+adb9e77.tar.gz."""

    assert _extension(reference) == expected
