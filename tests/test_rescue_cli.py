"""The rescue-capture and rescue-plan commands, driven through ``main``."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gel_registry import __main__ as main_module
from gel_registry.__main__ import main
from gel_registry.digest import canonical_json
from gel_registry.rescue import (
    RescueAsset,
    RescueCaptureManifest,
    RescuePlan,
    RescueRelease,
)


def _cli_plan(*, repository: str = "gelstable/gel") -> RescuePlan:
    return RescuePlan(
        schema_version=1,
        capture="legacy-2026-08-bootstrap",
        releases=(
            RescueRelease(
                repository=repository,
                tag="legacy-v7",
                target_commitish="abc123",
                assets=(
                    RescueAsset(
                        source_url=(
                            "https://packages.edgedb.com/archive/gel-server-1.0.0"
                        ),
                        destination_name="server.tar.gz",
                        content_type="application/gzip",
                        expected_size=3,
                        expected_sha256="ba7816bf8f01cfea414140de5dae2223b00361a396"
                        "177a9cb410ff61f20015ad",
                    ),
                ),
            ),
        ),
    )


def test_rescue_plan_requires_one_explicit_selection_mode() -> None:
    """Removing argument validation would let an ambiguous transfer plan be made."""

    with pytest.raises(SystemExit) as error:
        main(["rescue-plan", "--capture", "legacy-2026-08-bootstrap"])
    assert error.value.code == 2


def test_rescue_capture_requires_captured_at() -> None:
    """Rescue capture requires an explicit RFC3339 timestamp."""

    with pytest.raises(SystemExit) as error:
        main(["rescue-capture"])
    assert error.value.code == 2


def test_rescue_capture_runs_and_prints_capture_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_manifest = MagicMock(spec=RescueCaptureManifest)
    fake_manifest.capture = "legacy-2026-08-bootstrap"

    monkeypatch.setattr(
        main_module,
        "capture_rescue_indexes",
        lambda _client, _repo, _captured_at: fake_manifest,
    )

    exit_code = main(
        [
            "rescue-capture",
            "--captured-at",
            "2026-08-31T12:00:00Z",
        ]
    )
    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "legacy-2026-08-bootstrap"


def test_rescue_plan_prints_canonical_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _cli_plan()
    monkeypatch.setattr(
        main_module,
        "plan_rescue",
        lambda *args, **kwargs: plan,
    )

    exit_code = main(
        [
            "rescue-plan",
            "--capture",
            "legacy-2026-08-bootstrap",
            "--bulk",
        ]
    )
    assert exit_code == 0
    assert capsys.readouterr().out == canonical_json(plan).decode("utf-8")
