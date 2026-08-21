"""Validating a promotion pull request against its base and against upstream.

`validate-candidate` is the reviewer's proxy. Offline, it re-derives the whole
public tree from the committed inputs and refuses any file promotion could not
have emitted. Online, it re-verifies against live GitHub — but only the entries
that are new or changed relative to the base, so a rerun cannot be turned into
a re-audit of history. Evidence it cannot read is a failure, never a skip.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from support import complete_repository, product_policy, release_record
from support import write_source_policy as write_policy

import gel_registry.validation.candidate as candidate_module
import gel_registry.validation.remote as remote_module
from gel_registry import __main__ as cli
from gel_registry.adapters import adapter_for
from gel_registry.contracts import (
    BlockedCategory,
    BlockedManifest,
    BlockedRelease,
    ReleaseRecord,
)
from gel_registry.digest import canonical_json
from gel_registry.github import GitHubRelease, canonical_asset_names
from gel_registry.policy import load_source_policy
from gel_registry.promote import promote_candidate
from gel_registry.render import select_snapshot
from gel_registry.validation import ValidationReport, validate_candidate


def _blocked_entry(release_id: int = 123, tag: str = "v1.2.3") -> BlockedRelease:
    return BlockedRelease(
        product="gel-cli",
        repository="gelstable/gel-cli",
        release_id=release_id,
        tag=tag,
        category=BlockedCategory.MISSING_ARTIFACTS,
        diagnostic="release is missing required artifacts",
    )


def _write_blocked(repo: Path, *entries: BlockedRelease) -> Path:
    path = repo / "promotion" / "blocked.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(BlockedManifest(entries=entries)))
    return path


def _prepare(repo: Path, package_index_data: dict[str, object]) -> Path:
    """Build a valid repository and return a copy of it to use as the base."""

    write_policy(repo, [product_policy()])
    complete_repository(repo, package_index_data)
    base = repo.parent / f"{repo.name}-base"
    shutil.copytree(repo, base)
    return base


def _skip_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        candidate_module,
        "validate_release_remotes",
        lambda *_args, **_kwargs: ValidationReport(),
    )


def test_validate_candidate_rejects_committed_output_it_cannot_reproduce(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _prepare(tmp_path, package_index_data)
    promote_candidate(tmp_path, (release_record("1.2.4"),), ())
    (tmp_path / "public/registry.json").write_bytes(b"{}")
    _skip_remote(monkeypatch)

    assert (
        cli.main(["validate-candidate", "--repo", str(tmp_path), "--base", str(base)])
        == 1
    )
    report = validate_candidate(tmp_path, base)
    assert not report.ok
    assert any(error.startswith("candidate.reproduction:") for error in report.errors)


def _extra_promotion_file(repo: Path, base: Path) -> str:
    path = repo / "promotion" / "extra.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}")
    return "promotion/extra.json"


def _extra_pointer_file(repo: Path, base: Path) -> str:
    (repo / "pointers" / "extra.json").write_bytes(b"{}")
    return "pointers/extra.json"


def _edited_source_policy(repo: Path, base: Path) -> str:
    path = repo / "sources" / "products.json"
    policy = json.loads(path.read_bytes())
    policy["products"][0]["repository"] = "gelstable/gel-cli-fork"
    path.write_bytes(canonical_json(policy))
    return "sources/products.json"


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_extra_promotion_file, id="extra-promotion-file"),
        pytest.param(_extra_pointer_file, id="extra-pointer-file"),
        pytest.param(_edited_source_policy, id="edited-source-policy"),
    ],
)
def test_validate_candidate_rejects_changes_promotion_cannot_emit(
    tmp_path: Path,
    package_index_data: dict[str, object],
    tamper: Callable[[Path, Path], str],
) -> None:
    base = _prepare(tmp_path, package_index_data)
    relative = tamper(tmp_path, base)

    report = validate_candidate(tmp_path, base)

    assert not report.ok
    assert any(
        error.startswith(f"candidate.scope: {relative}:") for error in report.errors
    )


def test_validate_candidate_rejects_a_snapshot_the_pointer_does_not_select(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _prepare(tmp_path, package_index_data)
    promote_candidate(tmp_path, (release_record("1.2.4"),), ())

    # A second, independently promoted snapshot is smuggled into public/s.
    extra = tmp_path.parent / f"{tmp_path.name}-extra"
    shutil.copytree(base, extra)
    promote_candidate(extra, (release_record("1.2.5"),), ())
    identity = json.loads((extra / "pointers/latest.json").read_bytes())["snapshot"]
    shutil.copytree(extra / "public/s" / identity, tmp_path / "public/s" / identity)
    # The smuggled root points into the shared blob store, so its blobs come
    # along with it.
    shutil.copytree(extra / "public/i", tmp_path / "public/i", dirs_exist_ok=True)
    select_snapshot(tmp_path)
    _skip_remote(monkeypatch)

    report = validate_candidate(tmp_path, base)

    assert not report.ok
    assert any(error.startswith("candidate.reproduction:") for error in report.errors)


def test_validate_candidate_rejects_a_blocked_manifest_that_blocks_nothing(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    base = _prepare(tmp_path, package_index_data)
    _write_blocked(tmp_path)

    report = validate_candidate(tmp_path, base)

    assert not report.ok
    assert any(error.startswith("candidate.blocked:") for error in report.errors)


@pytest.mark.parametrize(
    ("changed", "unreadable_base", "expected"),
    [
        pytest.param(False, False, [], id="unchanged-entries-are-not-rechecked"),
        pytest.param(True, False, ["v1.2.4"], id="only-changed-entries-are-rechecked"),
        pytest.param(False, True, None, id="unreadable-base-fails-closed"),
    ],
)
def test_remote_blocked_checks_are_scoped_to_new_or_changed_evidence(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    changed: bool,
    unreadable_base: bool,
    expected: list[str] | None,
) -> None:
    write_policy(tmp_path, [product_policy()])
    complete_repository(tmp_path, package_index_data)
    unchanged = _blocked_entry()
    _write_blocked(tmp_path, unchanged)
    base = tmp_path.parent / f"{tmp_path.name}-base"
    shutil.copytree(tmp_path, base)
    if changed:
        _write_blocked(
            tmp_path,
            unchanged,
            unchanged.model_copy(update={"release_id": 124, "tag": "v1.2.4"}),
        )
    if unreadable_base:
        (base / "promotion" / "blocked.json").write_bytes(b"not-json")

    checked: list[tuple[str, ...]] = []

    def record(
        _repo: Path, entries: tuple[BlockedRelease, ...], **_kwargs: object
    ) -> ValidationReport:
        checked.append(tuple(entry.tag for entry in entries))
        return ValidationReport()

    _skip_remote(monkeypatch)
    monkeypatch.setattr(candidate_module, "validate_blocked_remotes", record)

    report = validate_candidate(tmp_path, base)

    if expected is None:
        # Evidence the validator cannot read is a failure, not an empty diff.
        assert not report.ok
        assert any(
            error.startswith("candidate.blocked.base: promotion/blocked.json:")
            for error in report.errors
        )
        assert checked == []
        return

    assert report.ok, report.errors
    assert [tag for call in checked for tag in call] == expected


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(None, id="faithful-evidence"),
        pytest.param("artifact", id="tampered-artifact-field"),
        pytest.param("diagnostic", id="tampered-volatile-diagnostic"),
    ],
)
def test_remote_reverification_compares_the_record_against_live_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str | None
) -> None:
    write_policy(tmp_path, [product_policy()])
    policy = load_source_policy(tmp_path)
    record = release_record()
    observed = record
    if tamper == "artifact":
        altered = record.artifacts[0].model_copy(
            update={"media_type": "application/octet-stream"}
        )
        observed = record.model_copy(
            update={"artifacts": (altered, *record.artifacts[1:])}
        )

    requested_names: list[tuple[str, ...]] = []
    verified: list[str] = []

    def fake_download(
        _client: httpx.Client,
        release: GitHubRelease,
        _destination: Path,
        **kwargs: object,
    ) -> dict[str, Path]:
        names = kwargs["names"]
        assert isinstance(names, tuple)
        requested_names.append(names)
        return {}

    def fake_verify(
        _policy: object, release: GitHubRelease, _assets: object
    ) -> ReleaseRecord:
        verified.append(release.tag_name)
        return observed

    monkeypatch.setattr(remote_module, "download_assets", fake_download)
    monkeypatch.setattr(adapter_for("gel-cli"), "verify", fake_verify)
    monkeypatch.setattr(
        remote_module,
        "_fetch_release_by_id",
        lambda *_args: remote_module._synthetic_release(record),
    )
    monkeypatch.setattr(
        remote_module,
        "_verify_blocked_entry",
        lambda *_args, **_kwargs: BlockedCategory.MISSING_ARTIFACTS,
        raising=False,
    )

    report = remote_module.validate_release_remotes(tmp_path, (record,), policy=policy)

    # Only the record it was handed is re-fetched, and only its canonical
    # assets are downloaded.
    assert verified == ["v1.2.3"]
    assert requested_names == [canonical_asset_names()]
    if tamper == "artifact":
        assert not report.ok
        assert any("remote artifact differs" in error for error in report.errors)
    else:
        assert report.ok, report.errors

    entry = _blocked_entry()
    if tamper == "diagnostic":
        entry = entry.model_copy(
            update={
                "diagnostic": (
                    "release is missing required artifacts; "
                    "timestamp=2026-08-20T12:34:56Z path=/tmp/assets "
                    "request_id=req-123 raw_command_output=network timeout"
                )
            }
        )

    blocked_report = remote_module.validate_blocked_remotes(tmp_path, (entry,))

    # A blocked diagnostic is a claim about upstream, so it must be exactly the
    # deterministic text the category implies — no timestamps, no paths, no
    # captured command output.
    if tamper == "diagnostic":
        assert not blocked_report.ok
        assert any(
            "blocked diagnostic mismatch" in error for error in blocked_report.errors
        )
    else:
        assert blocked_report.ok, blocked_report.errors
