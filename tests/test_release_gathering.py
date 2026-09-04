"""Gather direct release manifests from the GitHub allowlist."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from gel_registry.contracts import ReleaseRecord
from gel_registry.digest import canonical_json
from gel_registry.gather import _record_path, gather_missing, load_repositories
from gel_registry.github import (
    DiscoveredAsset,
    DiscoveredRelease,
    create_github_client,
    fetch_manifest_asset,
)
from gel_registry.github.transport import (
    API_VERSION,
    USER_AGENT,
    resolve_github_token,
)


def _manifest(repository: str, tag: str, name: str = "artifact") -> dict[str, object]:
    return {
        "schema_version": 1,
        "indexes": [
            {
                "channel": "stable",
                "platform": "x86_64-unknown-linux-gnu",
                "packages": [
                    {
                        "basename": "gel-cli",
                        "name": "gel-cli",
                        "version": "1.2.3",
                        "version_details": {
                            "major": 1,
                            "minor": 2,
                            "patch": 3,
                            "prerelease": [],
                            "metadata": {},
                        },
                        "version_key": "1.2.3",
                        "revision": "1",
                        "build_date": "2026-08-15T00:00:00Z",
                        "architecture": "x86_64",
                        "slot": "",
                        "tags": {},
                        "installref": (
                            f"https://github.com/{repository}/releases/download/{tag}/{name}"
                        ),
                        "installrefs": [
                            {
                                "ref": (
                                    f"https://github.com/{repository}/releases/download/{tag}/{name}"
                                ),
                                "type": "application/octet-stream",
                                "encoding": "identity",
                                "verification": {
                                    "size": 3,
                                    "sha256": "a" * 64,
                                    "blake2b": "b" * 128,
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _release(
    repository: str,
    release_id: int,
    tag: str,
    *,
    draft: bool = False,
    manifest: bool = True,
) -> dict[str, object]:
    assets: list[dict[str, str]] = []
    if manifest:
        assets.append(
            {
                "name": "gel-registry.json",
                "url": f"https://api.github.com/assets/{repository}/{release_id}",
            }
        )
    assets.append(
        {
            "name": "artifact",
            "url": f"https://api.github.com/assets/{repository}/{release_id}/artifact",
        }
    )
    return {
        "id": release_id,
        "tag_name": tag,
        "published_at": "2026-08-15T00:00:00Z",
        "draft": draft,
        "assets": assets,
    }


def _add_release_list(
    httpx_mock: HTTPXMock, repository: str, releases: list[dict[str, object]]
) -> None:
    httpx_mock.add_response(
        url=f"https://api.github.com/repos/{repository}/releases?per_page=100",
        json=releases,
    )


def test_manifest_asset_transport_follows_a_github_redirect(
    httpx_mock: HTTPXMock,
) -> None:
    """A GitHub asset redirect still returns the manifest bytes."""
    release = DiscoveredRelease(
        repository="gelstable/gel",
        release_id=2,
        tag="v2",
        published_at=datetime(2026, 8, 15, tzinfo=UTC),
        draft=False,
        assets=(
            DiscoveredAsset(
                name="gel-registry.json",
                api_url="https://api.github.com/assets/2",
            ),
        ),
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/2",
        status_code=302,
        headers={"Location": "https://objects.githubusercontent.com/assets/2"},
    )
    httpx_mock.add_response(
        url="https://objects.githubusercontent.com/assets/2",
        content=b'{"schema_version":1}',
    )

    with httpx.Client() as client:
        assert fetch_manifest_asset(client, release) == b'{"schema_version":1}'


def test_auth_attached_to_github_requests_when_token_present(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host-scoped authentication attaches Bearer token and standard GitHub headers."""
    monkeypatch.setenv("GH_TOKEN", "test-gh-token-123")
    assert resolve_github_token() == "test-gh-token-123"

    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
        json=[],
    )

    with create_github_client() as client:
        response = client.get(
            "https://api.github.com/repos/gelstable/gel/releases?per_page=100"
        )
        assert response.status_code == 200

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    req = requests[0]
    assert req.headers["Authorization"] == "Bearer test-gh-token-123"
    assert req.headers["Accept"] == "application/vnd.github+json"
    assert req.headers["X-GitHub-Api-Version"] == API_VERSION
    assert req.headers["User-Agent"] == USER_AGENT


def test_auth_accepts_github_token_fallback(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GITHUB_TOKEN is accepted when GH_TOKEN is unset."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-fallback-token")
    assert resolve_github_token() == "test-fallback-token"

    httpx_mock.add_response(
        url="https://api.github.com/user",
        json={"login": "gelstable"},
    )

    with create_github_client() as client:
        client.get("https://api.github.com/user")

    req = httpx_mock.get_requests()[-1]
    assert req.headers["Authorization"] == "Bearer test-fallback-token"


def test_auth_absent_from_non_github_requests_and_redirects(
    httpx_mock: HTTPXMock,
) -> None:
    """Tokens are never leaked to non-GitHub hosts or redirect locations."""
    release = DiscoveredRelease(
        repository="gelstable/gel",
        release_id=99,
        tag="v99",
        published_at=datetime(2026, 8, 15, tzinfo=UTC),
        draft=False,
        assets=(
            DiscoveredAsset(
                name="gel-registry.json",
                api_url="https://api.github.com/assets/99",
            ),
        ),
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/99",
        status_code=302,
        headers={"Location": "https://objects.githubusercontent.com/assets/99"},
    )
    httpx_mock.add_response(
        url="https://objects.githubusercontent.com/assets/99",
        content=b'{"schema_version":1}',
    )
    httpx_mock.add_response(
        url="https://packages.geldata.com/archive/index.json",
        content=b"{}",
    )
    httpx_mock.add_response(
        url="https://packages.edgedb.com/archive/index.json",
        content=b"{}",
    )

    with create_github_client(token="secret-token-abc") as client:
        assert fetch_manifest_asset(client, release) == b'{"schema_version":1}'
        client.get("https://packages.geldata.com/archive/index.json")
        client.get("https://packages.edgedb.com/archive/index.json")

    requests = httpx_mock.get_requests()
    api_req = requests[0]
    redirect_req = requests[1]
    geldata_req = requests[2]
    edgedb_req = requests[3]

    assert api_req.headers["Authorization"] == "Bearer secret-token-abc"
    assert "Authorization" not in redirect_req.headers
    assert "Authorization" not in geldata_req.headers
    assert "Authorization" not in edgedb_req.headers


def test_local_unauthenticated_use_remains_defined(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no token is present, requests succeed without Authorization."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert resolve_github_token() is None

    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
        json=[],
    )

    with create_github_client() as client:
        response = client.get(
            "https://api.github.com/repos/gelstable/gel/releases?per_page=100"
        )
        assert response.status_code == 200

    req = httpx_mock.get_requests()[-1]
    assert "Authorization" not in req.headers
    assert req.headers["User-Agent"] == USER_AGENT
    assert req.headers["Accept"] == "application/vnd.github+json"


@pytest.mark.parametrize("status_code", [401, 403, 429, 503])
def test_http_failures_abort_whole_gathering(
    tmp_path: Path, httpx_mock: HTTPXMock, status_code: int
) -> None:
    """Auth and server errors abort gathering rather than rejecting releases."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": ["gelstable/gel"], "schema_version": 1})
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
        status_code=status_code,
    )

    with create_github_client() as client, pytest.raises(httpx.HTTPStatusError):
        gather_missing(tmp_path, client)


def test_transport_failure_aborts_whole_gathering(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Connection failures abort gathering rather than rejecting releases."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": ["gelstable/gel"], "schema_version": 1})
    )
    httpx_mock.add_exception(
        httpx.ConnectError("network unreachable"),
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
    )

    with create_github_client() as client, pytest.raises(httpx.ConnectError):
        gather_missing(tmp_path, client)


@pytest.mark.parametrize("status_code", [401, 403, 429, 503])
def test_manifest_asset_http_failure_aborts_whole_gathering(
    tmp_path: Path, httpx_mock: HTTPXMock, status_code: int
) -> None:
    """Errors downloading manifest abort gathering rather than rejecting release."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": ["gelstable/gel"], "schema_version": 1})
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel",
        [_release("gelstable/gel", 10, "v10")],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/10",
        status_code=status_code,
    )

    with create_github_client() as client, pytest.raises(httpx.HTTPStatusError):
        gather_missing(tmp_path, client)


def test_gather_collects_every_valid_manifest_missing_from_committed_main(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Committed identities are skipped; every other valid manifest is returned."""
    committed = _manifest("gelstable/gel", "v1")
    committed["source"] = {
        "repository": "gelstable/gel",
        "release_id": 1,
        "tag": "v1",
        "published_at": "2026-08-15T00:00:00Z",
    }
    path = tmp_path / "releases" / "gelstable" / "gel" / "1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(ReleaseRecord.model_validate(committed)))
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json(
            {
                "repositories": ["gelstable/gel", "gelstable/gel-cli"],
                "schema_version": 1,
            }
        )
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel",
        [
            _release("gelstable/gel", 1, "v1"),
            _release("gelstable/gel", 2, "v2"),
            _release("gelstable/gel", 3, "v3", manifest=False),
        ],
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel-cli",
        [_release("gelstable/gel-cli", 4, "v4", draft=True)],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/2",
        content=json.dumps(_manifest("gelstable/gel", "v2")).encode(),
    )

    with httpx.Client() as client:
        result = gather_missing(tmp_path, client)

    assert [(r.source.repository, r.source.release_id) for r in result.records] == [
        ("gelstable/gel", 2)
    ]
    assert result.records[0].source.tag == "v2"
    assert result.rejected == ()


def test_bad_manifest_rejects_one_release_without_hiding_later_valid_releases(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """A publisher defect is visible but cannot wedge promotion for its peers."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": ["gelstable/gel"], "schema_version": 1})
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel",
        [_release("gelstable/gel", 10, "bad"), _release("gelstable/gel", 11, "good")],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/10", content=b"not json"
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/11",
        content=json.dumps(_manifest("gelstable/gel", "good")).encode(),
    )

    with httpx.Client() as client:
        result = gather_missing(tmp_path, client)

    assert [(r.source.repository, r.source.release_id) for r in result.records] == [
        ("gelstable/gel", 11)
    ]
    assert [(r.repository, r.release_id) for r in result.rejected] == [
        ("gelstable/gel", 10)
    ]


@pytest.mark.parametrize(
    "bad_repo",
    [
        "../evil",
        "evil/..",
        "./evil",
        "evil/.",
        "evil/../evil",
        "evil//evil",
        "/evil",
        "evil/",
        "evil",
        "evil/evil/evil",
        "evil\\evil/foo",
        "evil/foo\\bar",
        "evil/\x00evil",
        "evil/foo bar",
    ],
)
def test_load_repositories_rejects_traversal_and_invalid_allowlist(
    tmp_path: Path, bad_repo: str
) -> None:
    """GitHub source allowlist must reject traversal and invalid path components."""
    (tmp_path / "sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": [bad_repo], "schema_version": 1})
    )
    with pytest.raises(ValueError):
        load_repositories(tmp_path)


def test_record_path_rejects_escaping_repository(tmp_path: Path) -> None:
    """_record_path rejects repositories that would escape releases/."""
    # Directly test that an invalid repository component raises ValueError
    dummy_source = {
        "repository": "gelstable/gel-cli",
        "release_id": 1,
        "tag": "v1.0.0",
        "published_at": datetime(2026, 8, 15, tzinfo=UTC),
    }
    dummy_manifest = {
        "schema_version": 1,
        "replacements": [
            {
                "sha256": "a" * 64,
                "url": (
                    "https://github.com/gelstable/gel-cli/releases/download/"
                    "v1.0.0/artifact"
                ),
            }
        ],
    }
    valid_record = ReleaseRecord.model_validate(
        {**dummy_manifest, "source": dummy_source}
    )
    # Valid record resolves inside releases/
    record_file = _record_path(tmp_path, valid_record)
    assert record_file.is_relative_to(tmp_path / "releases")

    # If repository attempts traversal, validate_repository_name halts it
    object.__setattr__(valid_record.source, "repository", "../evil")
    with pytest.raises(ValueError):
        _record_path(tmp_path, valid_record)


@pytest.mark.parametrize(
    "bad_url",
    [
        # Path traversal
        "https://github.com/gelstable/gel/releases/download/v1/../v2/artifact",
        # Query parameter
        "https://github.com/gelstable/gel/releases/download/v1/artifact?alternate=1",
        # Fragment
        "https://github.com/gelstable/gel/releases/download/v1/artifact#fragment",
    ],
)
def test_gather_rejects_releases_with_adversarial_manifest_urls(
    tmp_path: Path, httpx_mock: HTTPXMock, bad_url: str
) -> None:
    """Gather rejects releases whose manifests contain traversal or query URLs."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "github.json").write_bytes(
        canonical_json({"repositories": ["gelstable/gel"], "schema_version": 1})
    )
    _add_release_list(
        httpx_mock,
        "gelstable/gel",
        [_release("gelstable/gel", 99, "v1")],
    )
    adversarial_manifest = {
        "schema_version": 1,
        "replacements": [
            {
                "sha256": "a" * 64,
                "url": bad_url,
            }
        ],
    }
    httpx_mock.add_response(
        url="https://api.github.com/assets/gelstable/gel/99",
        content=json.dumps(adversarial_manifest).encode(),
    )

    with httpx.Client() as client:
        result = gather_missing(tmp_path, client)

    assert result.records == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].repository == "gelstable/gel"
    assert result.rejected[0].release_id == 99
    assert "ReleaseRecord" in result.rejected[0].reason
