from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from support import release_record

import gel_registry.validation.remote as remote_module
from gel_registry.contracts import ReleaseRecord
from gel_registry.github import GitHubRelease


def test_scoped_release_remote_checks_only_supplied_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = release_record()
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

    monkeypatch.setattr(remote_module, "download_assets", fake_download)
    monkeypatch.setattr(remote_module, "verify_cli_release", fake_verify)

    report = remote_module.validate_release_remotes(tmp_path, (record,))

    assert report.ok, report.errors
    assert downloads == ["v1.2.3"]
    assert verifications == ["v1.2.3"]
