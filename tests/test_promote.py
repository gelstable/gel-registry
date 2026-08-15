from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import gel_registry.promote as promote_module
from gel_registry.digest import canonical_json
from gel_registry.promote import (
    PromotionError,
    PromotionResult,
    promote_release,
    publish_bootstrap,
)
from gel_registry.render import ContestedIdentityError
from gel_registry.schema import PackageIndex, ReleaseRecord, ReleaseSource

FIXTURES = Path(__file__).parent / "fixtures"


def _write_bootstrap(repo: Path, package_index_data: dict[str, object]) -> None:
    path = repo / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(PackageIndex.model_validate(package_index_data)))


def _release() -> ReleaseRecord:
    path = FIXTURES / "releases" / "gel-cli" / "1.0.0.json"
    return ReleaseRecord.model_validate_json(path.read_bytes())


def test_promotion_module_exposes_transaction_interfaces(tmp_path: Path) -> None:
    assert callable(publish_bootstrap)
    assert callable(promote_release)
    assert PromotionResult.__name__ == "PromotionResult"


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


def test_publish_bootstrap_refuses_missing_indexes(tmp_path: Path) -> None:
    with pytest.raises(PromotionError, match="bootstrap indexes"):
        publish_bootstrap(tmp_path)


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


def test_incomplete_record_is_rejected_without_writing(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    incomplete = ReleaseRecord.model_construct(
        product="gel-cli",
        channel="stable",
        version="1.0.0",
        source=ReleaseSource(
            repository="gelstable/gel-cli", release_tag="v1.0.0", release_id=1
        ),
        promoted_at=datetime(2026, 8, 15, tzinfo=UTC),
        artifacts=(),
    )

    with pytest.raises(PromotionError, match="invalid release record"):
        promote_release(tmp_path, incomplete)

    assert not (tmp_path / "releases").exists()
    assert not (tmp_path / "pointers").exists()
    assert not (tmp_path / "public").exists()


def test_render_failure_is_staged_before_working_tree_mutation(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    def fail(_repo: Path) -> str:
        raise RuntimeError("injected render failure")

    monkeypatch.setattr(promote_module, "build_snapshot", fail)
    with pytest.raises(RuntimeError, match="injected"):
        publish_bootstrap(tmp_path)

    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize("failure_call", [1, 3, 99])
def test_immutable_install_failure_rolls_back_new_files(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    real_create = promote_module._create_file
    calls = 0

    def fail_create(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise PromotionError("injected immutable install failure")
        return real_create(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(promote_module, "_create_file", fail_create)
    if failure_call == 99:
        promote_release(tmp_path, _release())
    else:
        with pytest.raises(PromotionError, match="injected"):
            promote_release(tmp_path, _release())

    if failure_call != 99:
        assert not (tmp_path / "releases").exists()
        assert not (tmp_path / "public").exists()
        assert not (tmp_path / "pointers").exists()


@pytest.mark.parametrize("fail_label", ["public/registry.json", "pointers/latest.json"])
def test_mutable_install_failure_rolls_back_new_immutable_files(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    fail_label: str,
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    real_replace = promote_module._replace_file
    failed = False

    def fail_replace(*args: object, **kwargs: object) -> None:
        nonlocal failed
        label = args[3]
        if not failed and label == fail_label:
            failed = True
            raise PromotionError("injected mutable install failure")
        real_replace(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(promote_module, "_replace_file", fail_replace)
    with pytest.raises(PromotionError, match="injected"):
        promote_release(tmp_path, _release())

    assert not (tmp_path / "releases").exists()
    assert not (tmp_path / "public").exists()
    assert not (tmp_path / "pointers").exists()


def test_branch_names_are_deterministic(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _write_bootstrap(tmp_path, package_index_data)
    bootstrap = publish_bootstrap(tmp_path)
    assert bootstrap.branch == "publish/legacy-2026-08-bootstrap"

    release_repo = tmp_path / "release"
    _write_bootstrap(release_repo, package_index_data)
    release = promote_release(release_repo, _release())
    assert release.branch == "promote/gel-cli-1.0.0"
