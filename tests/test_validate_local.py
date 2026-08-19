from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from support import complete_repository

from gel_registry.contracts import Pointer
from gel_registry.digest import canonical_json
from gel_registry.validation import (
    ValidationReport,
    validate_capture_local,
    validate_local,
)


def test_clean_local_validation_is_offline_and_checks_every_layer(
    tmp_path: Path,
    package_index_data: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete_repository(tmp_path, package_index_data)

    def fail_socket(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("local validation attempted a network connection")

    monkeypatch.setattr("socket.socket", fail_socket)
    monkeypatch.setattr(
        "gel_registry.validation.remote.httpx.Client",
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
    complete_repository(tmp_path, package_index_data)
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
    complete_repository(tmp_path, package_index_data)
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


def test_pointer_and_moving_documents_are_validated_as_a_pair(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    complete_repository(tmp_path, package_index_data)
    pointer = tmp_path / "pointers" / "latest.json"
    pointer.write_bytes(canonical_json(Pointer(snapshot="0" * 16)))

    report = validate_local(tmp_path)

    assert not report.ok
    assert any("pointers/latest.json" in error for error in report.errors)
    assert any("public/registry.json" in error for error in report.errors)


def test_fresh_render_rejects_an_unexpected_public_path(
    tmp_path: Path, package_index_data: dict[str, object]
) -> None:
    complete_repository(tmp_path, package_index_data)
    extra = tmp_path / "public" / "unexpected.txt"
    extra.write_bytes(b"unexpected")

    report = validate_local(tmp_path)

    assert not report.ok
    assert any("public/unexpected.txt" in error for error in report.errors)
