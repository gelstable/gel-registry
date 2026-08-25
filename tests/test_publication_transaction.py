"""Rendering immutable snapshots and publishing them as one transaction.

A snapshot is a pure composition of the committed bootstrap indexes and the
committed release records, content-addressed by the bytes it contains. Once
installed it never changes. Publication stages the whole repository in a copy,
diffs it against a strict allowlist of mutable paths, and installs the result
with rollback — so either every path moves together or none of them do.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest
from support import (
    FIXTURES,
    copy_release,
    fixture_release,
    package,
    write_bootstrap,
    write_pointer,
)

from gel_registry import __main__ as cli
from gel_registry import storage
from gel_registry.constants import CLI_PLATFORMS, LEGACY_PLATFORMS
from gel_registry.contracts import PackageIndex, ReleaseRecord, RootManifest
from gel_registry.digest import blob_id, canonical_json, snapshot_id
from gel_registry.publication import (
    PublicationError,
    PublicationResult,
    publish_bootstrap,
)
from gel_registry.render import (
    ContestedIdentityError,
    RenderError,
    build_snapshot,
    load_pinned_snapshot,
    select_snapshot,
)
from gel_registry.render.compose import compose_indexes

MUTABLE_PATHS = (
    "pointers/latest.json",
    "public/registry.json",
    "public/v1/snapshots.json",
    "vercel.json",
)
RELEASE_RECORD_SCHEMA = "public/v1/schema/release-record.json"
#: A moving root sits at ``public/``; a pinned root two levels below it. The
#: leading slash a root-absolute URL would carry is what these exclude.
MOVING_URL = re.compile(r"^i/[0-9a-f]{32}\.json$")
PINNED_URL = re.compile(r"^\.\./\.\./i/[0-9a-f]{32}\.json$")


def _write_index(repo: Path, package_index_data: dict[str, object]) -> None:
    path = repo / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(PackageIndex.model_validate(package_index_data)))


def _published_indexes(repo: Path, snapshot: str) -> dict[tuple[str, str], bytes]:
    """Resolve one snapshot's manifest into the index bytes it points at."""

    root = repo / "public" / "s" / snapshot / "registry.json"
    manifest = RootManifest.model_validate_json(root.read_bytes())
    return {
        (item.channel, item.platform): (root.parent / item.ref).resolve().read_bytes()
        for item in manifest.indexes
    }


def _repo_bytes(repo: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(repo): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file()
    }


def _inherit_release_record_schema(repo: Path) -> Path:
    """Install the pre-migration schema this repository inherited from PR1."""

    path = repo / RELEASE_RECORD_SCHEMA
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((FIXTURES / "inherited" / RELEASE_RECORD_SCHEMA).read_bytes())
    return path


@pytest.mark.parametrize("empty_releases_directory", [False, True])
def test_publication_installs_exactly_the_generated_support_files(
    tmp_path: Path,
    package_index_data: dict[str, object],
    empty_releases_directory: bool,
) -> None:
    _write_index(tmp_path, package_index_data)
    if empty_releases_directory:
        (tmp_path / "releases").mkdir()

    result = publish_bootstrap(tmp_path)

    index_bytes = compose_indexes(
        {
            ("stable", "x86_64-unknown-linux-gnu"): PackageIndex.model_validate(
                package_index_data
            )
        },
        (),
    )[("stable", "x86_64-unknown-linux-gnu")]
    identity = blob_id(index_bytes)

    assert isinstance(result, PublicationResult)
    assert not hasattr(result, "branch")
    assert result.changed_paths == (
        *sorted(
            (
                *MUTABLE_PATHS,
                "public/healthz",
                f"public/i/{identity}.json",
                f"public/s/{result.snapshot}/registry.json",
                "public/v1/schema/capture.json",
                "public/v1/schema/package-index.json",
                "public/v1/schema/pointer.json",
                RELEASE_RECORD_SCHEMA,
                "public/v1/schema/root.json",
                "public/v1/schema/snapshot-listing.json",
            )
        ),
    )
    assert json.loads((tmp_path / "pointers/latest.json").read_bytes()) == {
        "snapshot": result.snapshot
    }
    assert not (tmp_path / "promotion").exists()

    # The snapshot is a manifest of pointers, nothing more.
    pinned = tmp_path / "public" / "s" / result.snapshot
    assert {path.name for path in pinned.iterdir()} == {"registry.json"}
    manifest = RootManifest.model_validate_json((pinned / "registry.json").read_bytes())
    assert [item.ref for item in manifest.indexes] == [f"../../i/{identity}.json"]
    assert (tmp_path / "public" / "i" / f"{identity}.json").read_bytes() == index_bytes

    # The snapshot identity is exactly the hash of the *logical* index map --
    # channel/platform to bytes -- and is unaffected by where those bytes are
    # stored. ``index/`` is a namespace for identity, not a directory.
    assert result.snapshot == snapshot_id(
        {
            PurePosixPath("index") / f"{item.channel}-{item.platform}.json": (
                tmp_path / "public" / "i" / Path(item.ref).name
            ).read_bytes()
            for item in manifest.indexes
        }
    )


def test_publication_is_idempotent_and_composes_committed_releases(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_index(tmp_path, package_index_data)
    for platform in LEGACY_PLATFORMS:
        write_bootstrap(tmp_path, "stable", platform, [package(version="0.9.0")])
    release = copy_release(tmp_path, "1.0.0")
    duplicate = tmp_path / "releases" / "gel-cli" / "duplicate.json"
    duplicate.write_bytes(release.read_bytes())

    first = publish_bootstrap(tmp_path)
    before = _repo_bytes(tmp_path)
    second = publish_bootstrap(tmp_path)

    assert second.snapshot == first.snapshot
    assert second.changed_paths == ()
    assert _repo_bytes(tmp_path) == before

    # A committed release is composed into the platform indexes its artifacts
    # name, and only those; the release file itself is an input, never output.
    published = _published_indexes(tmp_path, first.snapshot)
    for (channel, platform), data in published.items():
        if channel != "stable":
            continue
        versions = [
            entry.version for entry in PackageIndex.model_validate_json(data).packages
        ]
        assert ("1.0.0" in versions) is (platform in CLI_PLATFORMS)
    assert release.relative_to(tmp_path).as_posix() not in first.changed_paths

    # Composition follows the record's own artifact platforms, so a product the
    # legacy matrix never knew about still renders.
    source = ReleaseRecord.model_validate_json(release.read_bytes())
    future = source.model_copy(
        update={
            "product": "future-product",
            "channel": "testing",
            "artifacts": tuple(
                artifact.model_copy(update={"platform": "future-x86_64"})
                for artifact in source.artifacts
            ),
        }
    )
    rendered = compose_indexes({}, (future,))
    assert [
        entry.version
        for entry in PackageIndex.model_validate_json(
            rendered[("testing", "future-x86_64")]
        ).packages
    ] == ["1.0.0"]

    # Composition is a set operation: the order the inputs arrive in cannot
    # change a single byte of the result.
    entries = PackageIndex.model_validate(
        {"packages": [package(version=version) for version in ("2.0.0", "1.0.0")]}
    )
    releases = (fixture_release("2.0.0"), fixture_release("1.0.0"))
    key = ("stable", LEGACY_PLATFORMS[0])
    assert compose_indexes({key: entries}, releases) == compose_indexes(
        {key: PackageIndex(packages=tuple(reversed(entries.packages)))},
        tuple(reversed(releases)),
    )

    # promoted_at is provenance, not content: it reaches neither the rendered
    # packages nor the snapshot identity.
    key = ("stable", "x86_64-unknown-linux-musl")
    rendered_bytes = published[key]
    data = json.loads(release.read_text())
    data["promoted_at"] = "2036-08-15T12:00:00Z"
    release.write_bytes(canonical_json(data))
    duplicate.write_bytes(release.read_bytes())
    assert build_snapshot(tmp_path) == first.snapshot
    assert _published_indexes(tmp_path, first.snapshot)[key] == rendered_bytes
    assert b"2036-08-15" not in rendered_bytes


def test_the_pointer_alone_selects_which_snapshot_the_moving_documents_serve(
    tmp_path: Path,
    package_index_data: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_index(tmp_path, package_index_data)
    copy_release(tmp_path, "1.0.0")

    # Building a snapshot neither reads nor writes the pointer.
    unreadable = tmp_path / "pointers" / "latest.json"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("not json")
    assert cli.main(["build-snapshot", "--repo", str(tmp_path)]) == 0
    old = capsys.readouterr().out.strip()
    assert len(old) == 16
    assert unreadable.read_text() == "not json"
    assert not (tmp_path / "public" / "registry.json").exists()
    assert not (tmp_path / "public" / "v1" / "snapshots.json").exists()

    (tmp_path / "releases" / "gel-cli" / "1.0.0.json").unlink()
    copy_release(tmp_path, "2.0.0")
    new = build_snapshot(tmp_path)
    assert old != new
    pinned_before = {
        path.relative_to(tmp_path): path.read_bytes()
        for root in ("s", "i")
        for path in (tmp_path / "public" / root).rglob("*")
        if path.is_file()
    }

    write_pointer(tmp_path, old)
    assert cli.main(["select-snapshot", "--repo", str(tmp_path)]) == 0
    root = json.loads((tmp_path / "public" / "registry.json").read_bytes())
    listing = json.loads((tmp_path / "public" / "v1" / "snapshots.json").read_bytes())
    # The moving root points into the shared blob store, not into a snapshot
    # directory, and resolves to exactly the bytes the pinned root does.
    assert {entry["ref"] for entry in root["indexes"]} == {
        f"i/{blob_id(data)}.json" for data in _published_indexes(tmp_path, old).values()
    }
    assert listing["latest"] == old
    assert set(listing["snapshots"]) == {old, new}

    write_pointer(tmp_path, new)
    assert cli.main(["select-snapshot", "--repo", str(tmp_path)]) == 0
    assert (
        json.loads((tmp_path / "public" / "v1" / "snapshots.json").read_bytes())[
            "latest"
        ]
        == new
    )

    # Republishing rewrote only the two moving documents; every pinned tree is
    # byte-for-byte what it was when it was installed.
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for root in ("s", "i")
        for path in (tmp_path / "public" / root).rglob("*")
        if path.is_file()
    } == pinned_before


def _mutate_existing_snapshot(repo: Path) -> None:
    publish_bootstrap(repo)
    target = next((repo / "public" / "s").rglob("*.json"))
    target.write_bytes(target.read_bytes() + b"\n")


def _arbitrary_schema_drift(repo: Path) -> None:
    path = repo / RELEASE_RECORD_SCHEMA
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"arbitrary-drift\n")


def _support_schema_drift(repo: Path) -> None:
    publish_bootstrap(repo)
    path = repo / "public" / "v1" / "schema" / "root.json"
    path.write_bytes(path.read_bytes() + b"\n")


def _contested_release_identity(repo: Path) -> None:
    release = copy_release(repo)
    data = json.loads(release.read_bytes())
    data["artifacts"][0]["sha256"] = "e" * 64
    (repo / "releases" / "gel-cli" / "duplicate.json").write_bytes(canonical_json(data))


def _symlinked_snapshot_parent(repo: Path) -> None:
    copy_release(repo)
    (repo / "public").mkdir(exist_ok=True)
    (repo / "outside").mkdir()
    (repo / "public" / "s").symlink_to(repo / "outside", target_is_directory=True)


def _missing_selected_snapshot(repo: Path) -> None:
    write_pointer(repo, "0" * 16)


def _empty_bootstrap(repo: Path) -> None:
    _write_index(repo, {"packages": []})


@pytest.mark.parametrize(
    ("prepare", "action", "error", "message"),
    [
        pytest.param(
            _mutate_existing_snapshot,
            publish_bootstrap,
            RenderError,
            "immutable|mismatch|drift",
            id="mutated-existing-snapshot",
        ),
        pytest.param(
            _arbitrary_schema_drift,
            publish_bootstrap,
            RenderError,
            "immutable support document mismatch",
            id="arbitrary-release-schema-drift",
        ),
        pytest.param(
            _support_schema_drift,
            publish_bootstrap,
            RenderError,
            "immutable support document mismatch",
            id="non-release-schema-drift",
        ),
        pytest.param(
            _contested_release_identity,
            publish_bootstrap,
            ContestedIdentityError,
            "",
            id="contested-release-identity",
        ),
        pytest.param(
            _symlinked_snapshot_parent,
            build_snapshot,
            RenderError,
            "snapshot",
            id="symlinked-public-s",
        ),
        pytest.param(
            _missing_selected_snapshot,
            select_snapshot,
            RenderError,
            "",
            id="missing-selected-snapshot",
        ),
        pytest.param(
            _empty_bootstrap,
            publish_bootstrap,
            PublicationError,
            "no bootstrap packages",
            id="empty-bootstrap",
        ),
    ],
)
def test_publication_refuses_to_run_against_an_untrustworthy_repository(
    tmp_path: Path,
    package_index_data: dict[str, object],
    prepare: Callable[[Path], None],
    action: Callable[[Path], object],
    error: type[Exception],
    message: str,
) -> None:
    _write_index(tmp_path, package_index_data)
    prepare(tmp_path)
    before = _repo_bytes(tmp_path)

    with pytest.raises(error, match=message or None):
        action(tmp_path)

    assert _repo_bytes(tmp_path) == before
    if (tmp_path / "outside").is_dir():
        assert not tuple((tmp_path / "outside").iterdir())


@pytest.mark.parametrize("failing_path", [*MUTABLE_PATHS, RELEASE_RECORD_SCHEMA])
def test_a_failed_write_rolls_back_every_publication_path(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    failing_path: str,
) -> None:
    _write_index(tmp_path, package_index_data)
    if failing_path == RELEASE_RECORD_SCHEMA:
        # The schema migration is the one immutable path publication may
        # rewrite, so its rollback is exercised by failing the pointer write
        # that follows it.
        _inherit_release_record_schema(tmp_path)
        failing_path = "pointers/latest.json"
    else:
        publish_bootstrap(tmp_path)
        updated = json.loads(json.dumps(package_index_data))
        updated["packages"][0]["version"] = "1.3.0"
        updated["packages"][0]["version_key"] = "1.3.0"
        updated["packages"][0]["version_details"]["patch"] = 0
        _write_index(tmp_path, updated)

    before = _repo_bytes(tmp_path)
    original_replace = storage.replace_atomic
    failed = False

    def fail_once(path: Path, data: bytes) -> None:
        nonlocal failed
        if path == tmp_path / failing_path and not failed:
            failed = True
            raise OSError("injected write failure")
        original_replace(path, data)

    monkeypatch.setattr(storage, "replace_atomic", fail_once)
    with pytest.raises(PublicationError):
        publish_bootstrap(tmp_path)

    assert failed
    assert _repo_bytes(tmp_path) == before


def test_publication_migrates_the_inherited_release_record_schema(
    tmp_path: Path,
    package_index_data: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_index(tmp_path, package_index_data)
    schema_path = _inherit_release_record_schema(tmp_path)
    inherited = schema_path.read_bytes()
    expected = canonical_json(ReleaseRecord.model_json_schema())
    assert inherited != expected

    assert cli.main(["publish-bootstrap", "--repo", str(tmp_path)]) == 0

    snapshot = capsys.readouterr().out.strip()
    assert schema_path.read_bytes() == expected
    assert json.loads((tmp_path / "pointers" / "latest.json").read_bytes()) == {
        "snapshot": snapshot
    }


def _blob_names(repo: Path) -> set[str]:
    return {path.name for path in (repo / "public" / "i").glob("*.json")}


def test_an_unchanged_index_is_stored_once_and_shared_by_every_snapshot(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    """The whole point of the layout: a release pays only for what it changed."""

    for platform in LEGACY_PLATFORMS:
        write_bootstrap(tmp_path, "stable", platform, [package(version="0.9.0")])
    first = build_snapshot(tmp_path)
    first_blobs = _blob_names(tmp_path)
    first_manifest = RootManifest.model_validate_json(
        (tmp_path / "public" / "s" / first / "registry.json").read_bytes()
    )

    # Change exactly one index.
    write_bootstrap(
        tmp_path,
        "stable",
        LEGACY_PLATFORMS[0],
        [package(version="0.9.0"), package(version="1.1.0")],
    )
    second = build_snapshot(tmp_path)
    second_manifest = RootManifest.model_validate_json(
        (tmp_path / "public" / "s" / second / "registry.json").read_bytes()
    )

    assert second != first
    assert _blob_names(tmp_path) - first_blobs != set()
    assert len(_blob_names(tmp_path)) == len(first_blobs) + 1

    first_urls = {(i.channel, i.platform): i.ref for i in first_manifest.indexes}
    second_urls = {(i.channel, i.platform): i.ref for i in second_manifest.indexes}
    changed = {key for key in first_urls if first_urls[key] != second_urls.get(key)}
    assert changed == {("stable", LEGACY_PLATFORMS[0])}
    # Every other index is referenced by URL, not copied.
    assert len(first_urls) - len(changed) == len(first_urls) - 1


def test_rebuilding_an_installed_snapshot_replays_without_error(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    """An interrupted publication must be resumable, so replay is a no-op."""

    _write_index(tmp_path, package_index_data)
    snapshot = build_snapshot(tmp_path)
    before = _repo_bytes(tmp_path)

    assert build_snapshot(tmp_path) == snapshot
    assert _repo_bytes(tmp_path) == before


def test_a_corrupted_blob_is_refused_by_its_content_address(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_index(tmp_path, package_index_data)
    snapshot = build_snapshot(tmp_path)
    # Canonical, valid, and simply not the index this blob is filed under: the
    # content address is the only thing left that can catch it.
    blob = next((tmp_path / "public" / "i").glob("*.json"))
    blob.write_bytes(canonical_json(PackageIndex(packages=())))

    with pytest.raises(RenderError, match="content address"):
        load_pinned_snapshot(tmp_path, snapshot)


def test_a_missing_blob_is_refused(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_index(tmp_path, package_index_data)
    snapshot = build_snapshot(tmp_path)
    next((tmp_path / "public" / "i").glob("*.json")).unlink()

    with pytest.raises(RenderError, match="index blob"):
        load_pinned_snapshot(tmp_path, snapshot)


def test_both_roots_resolve_to_the_same_index_files(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    """Mirrors the gel-cli contract test over the moving and pinned roots.

    Each manifest's URLs are document-relative and resolved against that
    document's own directory -- exactly as ``Source::File`` does in the client,
    where a root-absolute URL would discard the base and escape the mirror.
    """

    _write_index(tmp_path, package_index_data)
    for platform in LEGACY_PLATFORMS:
        write_bootstrap(tmp_path, "stable", platform, [package(version="0.9.0")])
    result = publish_bootstrap(tmp_path)

    public = tmp_path / "public"
    pinned_root = public / "s" / result.snapshot / "registry.json"
    moving_root = public / "registry.json"

    def resolve(root: Path) -> dict[tuple[str, str], Path]:
        manifest = RootManifest.model_validate_json(root.read_bytes())
        resolved: dict[tuple[str, str], Path] = {}
        for item in manifest.indexes:
            assert not item.ref.startswith("/"), item.ref
            resolved[(item.channel, item.platform)] = (root.parent / item.ref).resolve(
                strict=True
            )
        return resolved

    assert resolve(moving_root) == resolve(pinned_root)

    # And the URL forms each document must carry, given where it lives.
    moving = RootManifest.model_validate_json(moving_root.read_bytes())
    pinned = RootManifest.model_validate_json(pinned_root.read_bytes())
    assert all(MOVING_URL.fullmatch(item.ref) for item in moving.indexes)
    assert all(PINNED_URL.fullmatch(item.ref) for item in pinned.indexes)
