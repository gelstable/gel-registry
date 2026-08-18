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


@pytest.mark.parametrize("command", ["capture", "verify-capture-live"])
def test_one_time_capture_commands_are_not_installed(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from gel_registry.__main__ import main

    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code != 0
    assert "invalid choice" in capsys.readouterr().err


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
