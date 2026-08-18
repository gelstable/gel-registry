from __future__ import annotations

from pathlib import Path

import pytest

from gel_registry.digest import canonical_json
from gel_registry.schema import PackageIndex
from gel_registry.validate import ValidationReport


def _write_bootstrap(repo: Path, package_index_data: dict[str, object]) -> None:
    path = repo / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(PackageIndex.model_validate(package_index_data)))


def test_build_snapshot_does_not_read_pointer(
    tmp_path: Path,
    package_index_data: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gel_registry.__main__ import main

    _write_bootstrap(tmp_path, package_index_data)
    pointer = tmp_path / "pointers" / "latest.json"
    pointer.parent.mkdir()
    pointer.write_text("not json")

    assert main(["build-snapshot", "--repo", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    snapshot = captured.out.strip()
    assert len(snapshot) == 16
    assert all(character in "0123456789abcdef" for character in snapshot)
    assert pointer.read_text() == "not json"
    assert captured.err == ""


def test_validation_errors_are_nonzero_and_path_qualified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gel_registry import __main__ as cli

    monkeypatch.setattr(
        cli.validate,
        "validate_local",
        lambda _repo, _base=None: ValidationReport(
            errors=("pointer.integrity: pointers/latest.json: invalid snapshot",),
            checks=("pointer.integrity",),
        ),
    )

    assert cli.main(["validate", "--repo", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "pointer.integrity: pointers/latest.json: invalid snapshot" in captured.err
    assert "Traceback" not in captured.err
