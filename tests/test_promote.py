from __future__ import annotations

import json
from pathlib import Path

import pytest

from gel_registry.digest import canonical_json
from gel_registry.promote import PromotionError, promote_release, publish_bootstrap
from gel_registry.render import ContestedIdentityError
from gel_registry.schema import PackageIndex, ReleaseRecord

FIXTURES = Path(__file__).parent / "fixtures"


def _write_bootstrap(repo: Path, package_index_data: dict[str, object]) -> None:
    path = repo / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(PackageIndex.model_validate(package_index_data)))


def _release() -> ReleaseRecord:
    path = FIXTURES / "releases" / "gel-cli" / "1.0.0.json"
    return ReleaseRecord.model_validate_json(path.read_bytes())


def test_publish_bootstrap_builds_selects_and_reports_exact_files(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)

    result = publish_bootstrap(tmp_path)

    assert result.branch == "publish/legacy-2026-08-bootstrap"
    assert result.snapshot
    assert result.changed_paths == tuple(sorted(result.changed_paths))
    assert result.changed_paths == tuple(
        sorted(
            (
                "public/registry.json",
                f"public/s/{result.snapshot}/index/stable-x86_64-unknown-linux-gnu.json",
                f"public/s/{result.snapshot}/registry.json",
                "public/v1/snapshots.json",
                "pointers/latest.json",
            )
        )
    )
    assert (
        json.loads((tmp_path / "pointers/latest.json").read_text())["snapshot"]
        == result.snapshot
    )
    assert (tmp_path / "public/registry.json").exists()
    assert (tmp_path / "public/v1/snapshots.json").exists()


def test_publish_bootstrap_is_idempotent(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)

    first = publish_bootstrap(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    second = publish_bootstrap(tmp_path)

    assert second.snapshot == first.snapshot
    assert second.changed_paths == ()
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_publish_bootstrap_refuses_zero_package_indexes(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    package_index_data["packages"] = []
    _write_bootstrap(tmp_path, package_index_data)

    with pytest.raises(PromotionError, match="no bootstrap packages"):
        publish_bootstrap(tmp_path)

    assert not (tmp_path / "pointers").exists()
    assert not (tmp_path / "public").exists()


def test_promote_release_writes_record_and_composes_bootstrap(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)

    result = promote_release(tmp_path, _release())

    assert result.branch == "promote/gel-cli-1.0.0"
    assert (tmp_path / "releases/gel-cli/1.0.0.json").is_file()
    assert (tmp_path / f"public/s/{result.snapshot}/index").is_dir()
    assert "releases/gel-cli/1.0.0.json" in result.changed_paths


def test_promote_release_repeated_call_is_idempotent(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    first = promote_release(tmp_path, _release())
    second = promote_release(tmp_path, _release())

    assert second.snapshot == first.snapshot
    assert second.changed_paths == ()


def test_differing_existing_record_leaves_public_state_untouched(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    first = promote_release(tmp_path, _release())
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    data = json.loads((tmp_path / "releases/gel-cli/1.0.0.json").read_text())
    data["artifacts"][0]["sha256"] = "c" * 64
    differing = ReleaseRecord.model_validate(data)

    with pytest.raises(PromotionError, match="immutable release record"):
        promote_release(tmp_path, differing)

    assert (
        first.snapshot
        == json.loads((tmp_path / "pointers/latest.json").read_text())["snapshot"]
    )
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_contested_identity_fails_before_publication(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    promote_release(tmp_path, _release())
    duplicate = tmp_path / "releases/gel-cli/duplicate.json"
    data = json.loads((tmp_path / "releases/gel-cli/1.0.0.json").read_text())
    data["artifacts"][0]["sha256"] = "d" * 64
    duplicate.write_bytes(canonical_json(data))
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ContestedIdentityError):
        promote_release(tmp_path, _release())

    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before
