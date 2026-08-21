"""Installing a candidate with PR1's transaction, widened by exactly one set.

Promotion does not introduce a second write path. It is `publish_bootstrap`'s
staging-diff-install transaction parameterized with a wider set of mutable
paths: the release records and the blocked manifest join the pointer and the
two moving documents. Everything else still holds — the bootstrap indexes and
every already-installed snapshot are immutable, and a failure anywhere rolls
the whole repository back.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from support import fixture_release, product_policy, release_record
from support import write_source_policy as write_policy

from gel_registry import __main__ as cli
from gel_registry import storage
from gel_registry.contracts import (
    BlockedCategory,
    BlockedRelease,
    PackageIndex,
    ReleaseRecord,
)
from gel_registry.digest import canonical_json
from gel_registry.promote import promote_candidate
from gel_registry.promote.result import PromotionError
from gel_registry.promote.staging import _prepare_release
from gel_registry.publication import publish_bootstrap
from gel_registry.render import ContestedIdentityError, load_pinned_snapshot

BOOTSTRAP_SNAPSHOT = "0f776b71381237c8"


def _blocked() -> tuple[BlockedRelease, ...]:
    return (
        BlockedRelease(
            product="gel-cli",
            repository="gelstable/gel-cli",
            release_id=456,
            tag="v2.1.0",
            category=BlockedCategory.MISSING_ARTIFACTS,
            diagnostic="release is missing required artifacts",
        ),
    )


def _prepare_repository(repo: Path, package_index_data: dict[str, object]) -> None:
    write_policy(repo, [product_policy()])
    for platform in ("x86_64-unknown-linux-gnu", "x86_64-unknown-linux-musl"):
        path = repo / "bootstrap" / f"stable-{platform}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            canonical_json(PackageIndex.model_validate(package_index_data))
        )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_promotion_installs_records_blocked_state_and_the_pointer_together(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gel_registry.candidate import Candidate

    _prepare_repository(tmp_path, package_index_data)
    original = publish_bootstrap(tmp_path)
    original_tree = _tree_bytes(tmp_path / "public" / "s" / original.snapshot)

    result = promote_candidate(
        tmp_path, (release_record("1.0.0"), release_record("2.0.0")), _blocked()
    )

    assert result.branch == "promote/registry"
    assert result.snapshot != original.snapshot
    assert json.loads((tmp_path / "pointers/latest.json").read_bytes()) == {
        "snapshot": result.snapshot
    }
    assert (tmp_path / "releases/gel-cli/1.0.0.json").is_file()
    assert (tmp_path / "releases/gel-cli/2.0.0.json").is_file()
    assert json.loads((tmp_path / "promotion/blocked.json").read_bytes()) == {
        "schema_version": 1,
        "entries": [
            {
                "category": "missing-artifacts",
                "diagnostic": "release is missing required artifacts",
                "product": "gel-cli",
                "release_id": 456,
                "repository": "gelstable/gel-cli",
                "tag": "v2.1.0",
            }
        ],
    }

    # The new versions are composed into the snapshot; the previous snapshot is
    # still exactly the bytes it was installed with.
    versions: set[str] = set()
    _, index_bytes = load_pinned_snapshot(tmp_path, result.snapshot)
    for data in index_bytes.values():
        index = PackageIndex.model_validate_json(data)
        versions.update(
            entry.version for entry in index.packages if entry.name == "gel-cli"
        )
    assert versions == {"1.0.0", "1.2.3", "2.0.0"}
    assert _tree_bytes(tmp_path / "public" / "s" / original.snapshot) == original_tree

    # `build-candidate` runs this same transaction, once, for whatever the
    # producer found.
    monkeypatch.setattr(
        cli,
        "build_candidate",
        lambda *_args, **_kwargs: Candidate(
            base_records=(),
            records=(release_record("1.2.4"), release_record("1.2.5")),
            blocked=(),
        ),
    )
    assert cli.main(["build-candidate", "--repo", str(tmp_path)]) == 0

    snapshot = json.loads((tmp_path / "pointers/latest.json").read_bytes())["snapshot"]
    _, index_bytes = load_pinned_snapshot(tmp_path, snapshot)
    index = PackageIndex.model_validate_json(
        index_bytes["stable-x86_64-unknown-linux-musl.json"]
    )
    assert sorted(entry.version for entry in index.packages) == [
        "1.0.0",
        "1.2.3",
        "1.2.4",
        "1.2.5",
        "2.0.0",
    ]


def test_clearing_blocked_state_does_not_rebuild_the_snapshot(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _prepare_repository(tmp_path, package_index_data)

    first = promote_candidate(tmp_path, (), _blocked())
    second = promote_candidate(tmp_path, (), ())

    assert second.snapshot == first.snapshot
    assert second.changed_paths == ("promotion/blocked.json",)
    assert not (tmp_path / "promotion" / "blocked.json").exists()


def test_a_failed_pointer_write_rolls_the_whole_repository_back(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_repository(tmp_path, package_index_data)
    promote_candidate(tmp_path, (release_record("1.0.0"),), ())
    before = _tree_bytes(tmp_path)
    original_replace = storage.replace_atomic
    failed = False

    def fail_pointer_once(path: Path, data: bytes) -> None:
        nonlocal failed
        if path == tmp_path / "pointers" / "latest.json" and not failed:
            failed = True
            raise OSError("injected pointer failure")
        original_replace(path, data)

    monkeypatch.setattr(storage, "replace_atomic", fail_pointer_once)
    with pytest.raises(PromotionError):
        promote_candidate(tmp_path, (release_record("2.0.0"),), _blocked())

    assert failed
    # Not just the pointer: the new record, the blocked manifest and the whole
    # public tree are back where they were.
    assert _tree_bytes(tmp_path) == before


def _traversing_product(repo: Path) -> ReleaseRecord:
    return fixture_release().model_copy(update={"product": "../outside"})


def _absolute_product(repo: Path) -> ReleaseRecord:
    return fixture_release().model_copy(update={"product": str(repo / "outside")})


def _off_policy_repository(repo: Path) -> ReleaseRecord:
    release = fixture_release()
    return release.model_copy(
        update={
            "source": release.source.model_copy(
                update={"repository": "gelstable/other"}
            )
        }
    )


def _conflicting_existing_record(repo: Path) -> ReleaseRecord:
    promote_candidate(repo, (release_record("1.0.0"),), ())
    data = json.loads((repo / "releases/gel-cli/1.0.0.json").read_bytes())
    data["artifacts"][0]["sha256"] = "c" * 64
    return ReleaseRecord.model_validate(data)


def _contested_release_identity(repo: Path) -> ReleaseRecord:
    promote_candidate(repo, (release_record("1.0.0"),), ())
    data = json.loads((repo / "releases/gel-cli/1.0.0.json").read_bytes())
    data["artifacts"][0]["sha256"] = "d" * 64
    (repo / "releases" / "gel-cli" / "duplicate.json").write_bytes(canonical_json(data))
    return release_record("1.0.0")


@pytest.mark.parametrize(
    ("build", "error", "message"),
    [
        pytest.param(
            _traversing_product,
            PromotionError,
            "product policy",
            id="traversing-product",
        ),
        pytest.param(
            _absolute_product, PromotionError, "product policy", id="absolute-product"
        ),
        pytest.param(
            _off_policy_repository, PromotionError, "product policy", id="off-policy"
        ),
        pytest.param(
            _conflicting_existing_record,
            PromotionError,
            "immutable release record",
            id="conflicting-existing-record",
        ),
        pytest.param(
            _contested_release_identity,
            ContestedIdentityError,
            "",
            id="contested-identity",
        ),
    ],
)
def test_promotion_refuses_a_record_it_cannot_own(
    tmp_path: Path,
    package_index_data: dict[str, object],
    build: Callable[[Path], ReleaseRecord],
    error: type[Exception],
    message: str,
) -> None:
    _prepare_repository(tmp_path, package_index_data)
    record = build(tmp_path)
    before = _tree_bytes(tmp_path)

    with pytest.raises(error, match=message or None):
        promote_candidate(tmp_path, (record,), ())

    assert _tree_bytes(tmp_path) == before
    assert not (tmp_path / "outside").exists()

    # The staging layer refuses the same unsafe component on its own, before
    # any transaction has been opened.
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(PromotionError, match="safe release product path component"):
        _prepare_release(
            stage, fixture_release().model_copy(update={"product": "../x"})
        )
    assert not (stage / "x").exists()


def test_promotion_preserves_the_committed_bootstrap_snapshot(tmp_path: Path) -> None:
    source = Path(__file__).parents[1]
    for name in ("bootstrap", "pointers", "public", "sources"):
        shutil.copytree(source / name, tmp_path / name)
    bootstrap_before = _tree_bytes(tmp_path / "bootstrap")
    snapshot_before = _tree_bytes(tmp_path / "public" / "s" / BOOTSTRAP_SNAPSHOT)

    neutral = publish_bootstrap(tmp_path)
    result = promote_candidate(tmp_path, (release_record("1.0.0"),), _blocked())

    assert neutral.snapshot == BOOTSTRAP_SNAPSHOT
    assert result.snapshot != BOOTSTRAP_SNAPSHOT
    assert _tree_bytes(tmp_path / "bootstrap") == bootstrap_before
    assert (
        _tree_bytes(tmp_path / "public" / "s" / BOOTSTRAP_SNAPSHOT) == snapshot_before
    )
