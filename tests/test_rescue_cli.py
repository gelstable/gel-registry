"""The rescue CLI commands, driven through ``main``."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from gel_registry import __main__ as main_module
from gel_registry.__main__ import main
from gel_registry.digest import canonical_json
from gel_registry.rescue import (
    RescueAsset,
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


def test_rescue_publish_dry_run_prints_operations_and_mutates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dry run performs the whole preflight without POSTing anything."""

    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.url.host == "packages.edgedb.com":
            return httpx.Response(200)
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    monkeypatch.setattr(
        main_module,
        "create_github_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json(_cli_plan()))

    assert (
        main(
            [
                "rescue-publish",
                "--plan",
                str(plan_path),
                "--dry-run",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "dry run: no releases or assets were created" in output
    assert "create draft release gelstable/gel@legacy-v7" in output
    assert "total uploads: 2" in output
    assert [method for method, _ in requests] == ["GET", "GET", "HEAD"]


def test_rescue_publish_reports_a_preflight_conflict_as_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        main_module,
        "create_github_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(404))
        ),
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json(_cli_plan(repository="attacker/repo")))

    assert main(["rescue-publish", "--plan", str(plan_path)]) == 1
    assert "not allowlisted" in capsys.readouterr().err
