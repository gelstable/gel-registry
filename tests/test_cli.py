from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from gel_registry.constants import capture_urls
from gel_registry.digest import canonical_json
from gel_registry.schema import CaptureEntry, CaptureManifest, PackageIndex
from gel_registry.validate import ValidationReport


def _write_bootstrap(repo: Path, package_index_data: dict[str, object]) -> None:
    path = repo / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(PackageIndex.model_validate(package_index_data)))


def _write_empty_capture(repo: Path) -> Path:
    root = repo / "upstream" / "packages.geldata.com" / "legacy-2026-08-bootstrap"
    root.mkdir(parents=True)
    manifest = CaptureManifest(
        capture="legacy-2026-08-bootstrap",
        origin="https://packages.geldata.com",
        captured_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        entries=tuple(
            CaptureEntry(
                channel=channel,
                platform=platform,
                url=url,
                status=404,
            )
            for channel, platform, url in capture_urls()
        ),
    )
    (root / "indexes").mkdir()
    (root / "capture.json").write_bytes(canonical_json(manifest))
    return root


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


def test_select_snapshot_reports_pointer_error_without_building(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gel_registry import __main__ as cli

    pointer = tmp_path / "pointers" / "latest.json"
    pointer.parent.mkdir()
    pointer.write_text("not json")

    def fail_build(_repo: Path) -> str:
        raise AssertionError("select must not build a snapshot")

    monkeypatch.setattr(cli.render, "build_snapshot", fail_build)

    assert cli.main(["select-snapshot", "--repo", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "invalid latest pointer" in captured.err
    assert "Traceback" not in captured.err


def test_normalize_is_local_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gel_registry import __main__ as cli

    _write_empty_capture(tmp_path)

    def fail_client(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("normalize must not construct an HTTP client")

    monkeypatch.setattr(cli.httpx, "Client", fail_client)

    assert cli.main(["normalize", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""
    assert (tmp_path / "bootstrap").is_dir()


def test_validate_defaults_to_local_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gel_registry import __main__ as cli

    calls: list[Path | None] = []

    def local(repository: Path, base: Path | None = None) -> ValidationReport:
        calls.append(base)
        return ValidationReport(checks=("local",))

    def fail_client(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("local validation must not construct an HTTP client")

    monkeypatch.setattr(cli.validate, "validate_local", local)
    monkeypatch.setattr(cli.httpx, "Client", fail_client)

    assert cli.main(["validate", "--repo", str(tmp_path)]) == 0
    assert calls == [None]
    assert capsys.readouterr().err == ""


def test_capture_requires_explicit_timestamp() -> None:
    from gel_registry.__main__ import main

    with pytest.raises(SystemExit) as error:
        main(["capture", "--repo", "/tmp"])
    assert error.value.code != 0


def test_capture_and_live_verification_are_explicit_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gel_registry import __main__ as cli

    calls: list[str] = []

    def capture(*_args: object, **_kwargs: object) -> object:
        calls.append("capture")
        raise AssertionError("capture was not explicitly requested")

    def rehearsal(*_args: object, **_kwargs: object) -> ValidationReport:
        calls.append("rehearsal")
        return ValidationReport(checks=("remote.capture.rehearsal",))

    monkeypatch.setattr(cli.capture, "capture_legacy", capture)
    monkeypatch.setattr(cli.validate, "validate_capture_rehearsal", rehearsal)
    monkeypatch.setattr(
        cli.validate,
        "validate_local",
        lambda _repo, _base=None: ValidationReport(checks=("local",)),
    )

    assert cli.main(["validate", "--repo", str(tmp_path)]) == 0
    assert calls == []
    assert cli.main(["verify-capture-live", "--repo", str(tmp_path)]) == 0
    assert calls == ["rehearsal"]
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "error_message",
    [
        "contested package identity (basename='gel-cli', version='1.0.0', slot='')",
        "immutable path collision: /tmp/releases/gel-cli/1.0.0.json",
    ],
)
def test_operator_errors_are_concise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_message: str,
) -> None:
    from gel_registry import __main__ as cli

    def fail(_repo: Path) -> str:
        raise cli.render.RenderError(error_message)

    monkeypatch.setattr(cli.render, "build_snapshot", fail)

    assert cli.main(["build-snapshot", "--repo", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert error_message in captured.err
    assert "Traceback" not in captured.err


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
