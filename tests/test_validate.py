from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import gel_registry.validate as validate_module
from gel_registry.constants import CLI_PLATFORMS, capture_urls
from gel_registry.digest import canonical_json, hash_bytes
from gel_registry.github import GitHubRelease
from gel_registry.render import build_snapshot, render_schemas, select_snapshot
from gel_registry.schema import (
    Artifact,
    CaptureEntry,
    CaptureManifest,
    PackageIndex,
    Pointer,
    ReleaseRecord,
    ReleaseSource,
)
from gel_registry.validate import (
    ValidationReport,
    validate_capture_local,
    validate_local,
)

CAPTURED_AT = datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)


def _complete_repository(
    root: Path, package_index_data: dict[str, object]
) -> CaptureManifest:
    index = PackageIndex.model_validate(package_index_data)
    body = canonical_json(index)
    digests = hash_bytes(body)
    capture_root = (
        root / "upstream" / "packages.geldata.com" / "legacy-2026-08-bootstrap"
    )
    (capture_root / "indexes").mkdir(parents=True)
    entries: list[CaptureEntry] = []
    for channel, platform, url in capture_urls():
        path = f"indexes/{channel}-{platform}.json"
        (capture_root / path).write_bytes(body)
        entries.append(
            CaptureEntry(
                channel=channel,
                platform=platform,
                url=url,
                status=200,
                final_url=url,
                size=digests.size,
                sha256=digests.sha256,
                blake2b=digests.blake2b,
                path=path,
            )
        )
    manifest = CaptureManifest(
        capture="legacy-2026-08-bootstrap",
        origin="https://packages.geldata.com",
        captured_at=CAPTURED_AT,
        entries=tuple(entries),
    )
    (capture_root / "capture.json").write_bytes(canonical_json(manifest))

    from gel_registry.normalize import normalize_capture

    normalize_capture(capture_root, root / "bootstrap")
    snapshot = build_snapshot(root)
    (root / "pointers").mkdir()
    (root / "pointers" / "latest.json").write_bytes(
        canonical_json(Pointer(snapshot=snapshot))
    )
    select_snapshot(root)
    render_schemas(root)
    return manifest


def test_clean_local_validation_is_offline_and_checks_every_layer(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete_repository(tmp_path, package_index_data)

    def fail_socket(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("local validation attempted a network connection")

    monkeypatch.setattr("socket.socket", fail_socket)
    monkeypatch.setattr(
        "gel_registry.validate.httpx.Client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local validation constructed an HTTP client")
        ),
    )

    report = validate_local(tmp_path)

    assert isinstance(report, ValidationReport)
    assert report.ok
    assert {
        "capture.manifest",
        "capture.bodies",
        "normalization.drift",
        "schema.validity",
        "render.drift",
        "pointer.integrity",
        "snapshot.listing",
        "snapshot.internals",
    }.issubset(report.checks)


def test_local_validation_reports_immutable_history_path(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _complete_repository(tmp_path, package_index_data)
    base = tmp_path.parent / "base"
    shutil.copytree(tmp_path, base)
    body_path = tmp_path / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    body_path.write_bytes(body_path.read_bytes() + b"\n")

    report = validate_local(tmp_path, base)

    assert not report.ok
    assert any(
        "bootstrap/stable-x86_64-unknown-linux-gnu.json" in error
        for error in report.errors
    )


def test_capture_validation_does_not_require_publication_state(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _complete_repository(tmp_path, package_index_data)
    shutil.rmtree(tmp_path / "public")
    shutil.rmtree(tmp_path / "pointers")

    report = validate_capture_local(tmp_path)

    assert report.ok, report.errors
    assert {
        "capture.manifest",
        "capture.bodies",
        "bootstrap.schema",
        "normalization.drift",
    }.issubset(report.checks)
    assert "pointer.integrity" not in report.checks
    assert "render.drift" not in report.checks


def test_capture_validation_checks_only_capture_and_bootstrap_history(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _complete_repository(tmp_path, package_index_data)
    base = tmp_path.parent / "capture-base"
    shutil.copytree(tmp_path, base)
    shutil.rmtree(tmp_path / "public")
    shutil.rmtree(tmp_path / "pointers")
    body_path = tmp_path / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    body_path.write_bytes(body_path.read_bytes() + b"\n")

    report = validate_capture_local(tmp_path, base)

    assert not report.ok
    assert any(
        "bootstrap/stable-x86_64-unknown-linux-gnu.json" in error
        for error in report.errors
    )


def test_pointer_and_moving_documents_are_validated_as_a_pair(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _complete_repository(tmp_path, package_index_data)
    pointer = tmp_path / "pointers" / "latest.json"
    pointer.write_bytes(canonical_json(Pointer(snapshot="0" * 16)))

    report = validate_local(tmp_path)

    assert not report.ok
    assert any("pointers/latest.json" in error for error in report.errors)
    assert any("public/registry.json" in error for error in report.errors)


def test_fresh_render_rejects_an_unexpected_public_path(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _complete_repository(tmp_path, package_index_data)
    extra = tmp_path / "public" / "unexpected.txt"
    extra.write_bytes(b"unexpected")

    report = validate_local(tmp_path)

    assert not report.ok
    assert any("public/unexpected.txt" in error for error in report.errors)


def test_local_validation_does_not_construct_remote_client_for_empty_release_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    class ExplodingClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal calls
            calls += 1
            raise AssertionError("remote client should not be constructed")

    monkeypatch.setattr("gel_registry.validate.httpx.Client", ExplodingClient)

    report = validate_module.validate_release_remotes(tmp_path, ())

    assert report.ok
    assert calls == 0


def _release_record(version: str = "1.2.3") -> ReleaseRecord:
    artifacts: list[Artifact] = []
    for platform in CLI_PLATFORMS:
        suffix = ".exe" if platform.endswith("-windows-msvc") else ""
        base = f"gel-cli-{platform}{suffix}"
        for encoding, name in (("identity", base), ("zstd", f"{base}.zst")):
            artifacts.append(
                Artifact(
                    platform=platform,
                    encoding=encoding,
                    media_type=(
                        "application/x-dosexec"
                        if platform.endswith("-windows-msvc")
                        else "application/x-mach-binary"
                        if platform.endswith("-apple-darwin")
                        else "application/x-pie-executable"
                    ),
                    url=(
                        "https://github.com/gelstable/gel-cli/releases/download/"
                        f"v{version}/{name}"
                    ),
                    size=3,
                    sha256="a" * 64,
                    blake2b="b" * 128,
                )
            )
    return ReleaseRecord(
        product="gel-cli",
        channel="stable",
        version=version,
        source=ReleaseSource(
            repository="gelstable/gel-cli",
            release_tag=f"v{version}",
            release_id=123,
        ),
        promoted_at=CAPTURED_AT,
        artifacts=tuple(artifacts),
    )


def test_clean_repository_with_a_valid_release_record_passes(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    _complete_repository(tmp_path, package_index_data)
    record = _release_record("2.0.0")
    release_path = tmp_path / "releases" / "gel-cli" / "2.0.0.json"
    release_path.parent.mkdir(parents=True)
    release_path.write_bytes(canonical_json(record))
    snapshot = build_snapshot(tmp_path)
    (tmp_path / "pointers" / "latest.json").write_bytes(
        canonical_json(Pointer(snapshot=snapshot))
    )
    select_snapshot(tmp_path)

    report = validate_local(tmp_path)

    assert report.ok, report.errors


def test_scoped_release_remote_checks_only_supplied_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _release_record()
    downloads: list[str] = []
    verifications: list[str] = []

    def fake_download(
        _client: httpx.Client, release: GitHubRelease, _destination: Path
    ) -> dict[str, Path]:
        downloads.append(release.tag_name)
        return {}

    def fake_verify(release: GitHubRelease, _assets: object) -> ReleaseRecord:
        verifications.append(release.tag_name)
        return record

    monkeypatch.setattr(validate_module, "download_assets", fake_download)
    monkeypatch.setattr(validate_module, "verify_cli_release", fake_verify)

    report = validate_module.validate_release_remotes(tmp_path, (record,))

    assert report.ok, report.errors
    assert downloads == ["v1.2.3"]
    assert verifications == ["v1.2.3"]
