"""Building a candidate, and the one rule that decides what a failure means.

`build_candidate` is where discovery, downloading and verification meet, and it
is governed by a single rule:

    A *deterministic* defect in the upstream release — an artifact that is
    missing, oversized, corrupt, wrongly described, or that fails a completed
    attestation check — becomes a `blocked` entry, and the run continues.
    Anything *ambiguous* — a verification error the adapter did not classify, a
    tool that could not run, a transport failure — aborts the run and writes
    nothing.

Never the reverse. Recording an ambiguous failure as `blocked` would publish a
claim about upstream that nobody verified; aborting on a deterministic defect
would let one bad release stop every good one.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
import zstandard as zstd
from support import github_release, mock_client, product_policy, release_record
from support import write_source_policy as write_policy

import gel_registry.github.assets as assets_module
from gel_registry.adapters import DeterministicVerificationError, VerificationError
from gel_registry.candidate import (
    Candidate,
    CandidateError,
    build_candidate,
    load_releases,
)
from gel_registry.contracts import BlockedCategory
from gel_registry.digest import canonical_json
from gel_registry.github import GITHUB_REPOSITORY, GitHubError

Payload = dict[str, object]
Attestor = Callable[[Path], None]


def _payload(
    tag: str, release_id: int, *, repository: str = GITHUB_REPOSITORY
) -> Payload:
    """A GitHub release payload whose every URL points at `repository`."""

    payload = github_release(tag=tag, release_id=release_id, owner=repository)
    payload["url"] = f"https://api.github.com/repos/{repository}/releases/{release_id}"
    payload["html_url"] = f"https://github.com/{repository}/releases/tag/{tag}"
    assets = payload["assets"]
    assert isinstance(assets, list)
    for asset in assets:
        assert isinstance(asset, dict)
        asset["url"] = f"https://api.github.com/repos/{repository}/assets/{asset['id']}"
        asset["browser_download_url"] = (
            f"https://github.com/{repository}/releases/download/{tag}/{asset['name']}"
        )
    return payload


def _asset_bodies(releases: list[Payload]) -> dict[int, bytes]:
    bodies: dict[int, bytes] = {}
    for release in releases:
        assets = release["assets"]
        assert isinstance(assets, list)
        for asset in assets:
            assert isinstance(asset, dict)
            name = asset["name"]
            assert isinstance(name, str)
            identity = f"payload:{name.removesuffix('.zst')}".encode()
            bodies[int(str(asset["id"]))] = (
                zstd.ZstdCompressor().compress(identity)
                if name.endswith(".zst")
                else identity
            )
    return bodies


def _client(
    releases: list[Payload],
    requested: list[str] | None = None,
    bodies: dict[int, bytes] | None = None,
) -> httpx.Client:
    served = bodies if bodies is not None else _asset_bodies(releases)
    seen = requested if requested is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=releases)
        if "/assets/" in request.url.path:
            return httpx.Response(
                200, content=served[int(request.url.path.rsplit("/", 1)[-1])]
            )
        raise AssertionError(f"unexpected request: {request.url}")

    return mock_client(handler)


def _accept(_path: Path) -> None:
    return None


@pytest.mark.parametrize("already_committed", [False, True])
def test_one_fetch_per_repository_serves_every_product_and_canonical_assets_only(
    tmp_path: Path, already_committed: bool
) -> None:
    repository = "gelstable/gel"
    write_policy(
        tmp_path,
        [
            product_policy(repository=repository),
            product_policy("gel-tools", repository=repository),
        ],
    )
    releases = [
        _payload("v1.2.3", 123, repository=repository),
        _payload("gel-server-v1.0.0", 100, repository=repository),
        _payload("nightly", 101, repository=repository),
        _payload("not-a-version", 102, repository=repository),
    ]
    sidecar = {
        "id": 999,
        "name": "checksums.txt",
        "url": f"https://api.github.com/repos/{repository}/assets/999",
        "browser_download_url": "https://evil.example/checksums.txt",
    }
    assets = releases[0]["assets"]
    assert isinstance(assets, list)
    assets.append(sidecar)

    if already_committed:
        # A record the repository already holds is loaded as base evidence and
        # never re-verified.
        path = tmp_path / "releases" / "gel-cli" / "1.2.3.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            canonical_json(
                release_record("1.2.3").model_copy(
                    update={
                        "source": release_record("1.2.3").source.model_copy(
                            update={"repository": repository, "release_id": 123}
                        )
                    }
                )
            )
        )

    requested: list[str] = []
    attested: list[str] = []
    with _client(releases, requested) as client:
        candidate = build_candidate(
            tmp_path, client, attestor=lambda path: attested.append(path.name)
        )

    # One repository, one fetch, however many products read from it. Tags the
    # adapters do not recognise are ignored rather than blocked.
    assert requested.count(f"/repos/{repository}/releases") == 1
    assert [(record.product, record.version) for record in candidate.records] == (
        [("gel-tools", "1.2.3")]
        if already_committed
        else [("gel-cli", "1.2.3"), ("gel-tools", "1.2.3")]
    )
    assert candidate.blocked == ()
    assert len(candidate.base_records) == (1 if already_committed else 0)

    # Only the canonical asset set is fetched, and only it is attested — ten
    # assets per product that still needs verifying.
    assert "/assets/999" not in requested
    assert "checksums.txt" not in attested
    assert len(attested) == (10 if already_committed else 20)


def _drop_a_compressed_asset(payload: Payload) -> None:
    assets = payload["assets"]
    assert isinstance(assets, list)
    assets.pop()


def _drop_a_canonical_asset(payload: Payload) -> None:
    assets = payload["assets"]
    assert isinstance(assets, list)
    assets.pop(0)


def _declare_an_oversized_asset(payload: Payload) -> None:
    assets = payload["assets"]
    assert isinstance(assets, list)
    first = assets[0]
    assert isinstance(first, dict)
    first["size"] = 5


@pytest.mark.parametrize(
    ("mutate", "limits", "attestor", "category", "downloads"),
    [
        pytest.param(
            None,
            False,
            "gh-verified-negative",
            BlockedCategory.ATTESTATION_FAILED,
            True,
            id="verified-negative-attestation",
        ),
        pytest.param(
            _drop_a_compressed_asset,
            False,
            _accept,
            BlockedCategory.MISSING_ARTIFACTS,
            False,
            id="missing-compressed-artifact",
        ),
        pytest.param(
            _drop_a_canonical_asset,
            False,
            _accept,
            BlockedCategory.MISSING_ARTIFACTS,
            False,
            id="missing-canonical-artifact",
        ),
        pytest.param(
            _declare_an_oversized_asset,
            True,
            _accept,
            BlockedCategory.INVALID_METADATA,
            False,
            id="oversized-artifact",
        ),
        pytest.param(
            None,
            False,
            None,
            BlockedCategory.ARTIFACT_MISMATCH,
            True,
            id="corrupt-compressed-artifact",
        ),
        pytest.param(
            None,
            False,
            "deterministic",
            BlockedCategory.CHECKSUM_MISMATCH,
            True,
            id="classified-verification-failure",
        ),
    ],
)
def test_a_deterministic_upstream_defect_becomes_a_blocked_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[Payload], None] | None,
    limits: bool,
    attestor: Attestor | str | None,
    category: BlockedCategory,
    downloads: bool,
) -> None:
    write_policy(tmp_path)
    release = _payload("v1.2.3", 123)
    if mutate is not None:
        mutate(release)
    if limits:
        monkeypatch.setattr(assets_module, "MAX_ASSET_BYTES", 4)
        monkeypatch.setattr(assets_module, "MAX_TOTAL_ASSET_BYTES", 100)

    bodies = _asset_bodies([release])
    corrupt_name: str | None = None
    if category is BlockedCategory.ARTIFACT_MISMATCH:
        assets = release["assets"]
        assert isinstance(assets, list)
        target = next(
            asset
            for asset in assets
            if isinstance(asset, dict) and str(asset["name"]).endswith(".zst")
        )
        corrupt_name = str(target["name"])
        bodies[int(str(target["id"]))] = b"not a zstd stream"

    def classify(path: Path) -> None:
        raise DeterministicVerificationError(
            BlockedCategory.CHECKSUM_MISMATCH,
            "checksum mismatch was verified",
            assets=(path.name,),
        )

    production = attestor == "gh-verified-negative"
    if production:
        # The real attestor: `gh` ran to completion and said no.
        monkeypatch.setattr(
            "gel_registry.adapters.cli.subprocess.run",
            _gh_failure(1, "✗ Verification failed"),
        )
    resolved: Attestor = classify if attestor == "deterministic" else _accept
    requested: list[str] = []
    with _client([release], requested, bodies) as client:
        candidate = (
            build_candidate(tmp_path, client)
            if production
            else build_candidate(tmp_path, client, attestor=resolved)
        )

    assert candidate.records == ()
    assert len(candidate.blocked) == 1
    assert candidate.blocked[0].category is category
    assert any("/assets/" in path for path in requested) is downloads
    if production:
        assets = release["assets"]
        assert isinstance(assets, list)
        first = assets[0]
        assert isinstance(first, dict)
        assert candidate.blocked[0].diagnostic == (
            f"attestation failed for asset(s): {first['name']}"
        )
    if corrupt_name is not None:
        assert candidate.blocked[0].diagnostic == (
            f"compressed artifact does not match identity for asset(s): {corrupt_name}"
        )


def _rate_limited(_path: Path) -> NoReturn:
    raise RuntimeError("attestation service rate limited")


def _untyped_verification_error(_path: Path) -> NoReturn:
    raise VerificationError("attestation service rate limited")


def _checksum_flavoured_verification_error(_path: Path) -> NoReturn:
    raise VerificationError("checksum service timeout")


def _attestor_subprocess_failure(_path: Path) -> NoReturn:
    raise subprocess.CalledProcessError(
        1, "custom attestor", stderr="Verification failed while unavailable"
    )


def _missing_tool(_path: Path) -> NoReturn:
    raise FileNotFoundError("gh")


def _gh_failure(returncode: int, stderr: str) -> Callable[..., NoReturn]:
    def run(*_args: object, **_kwargs: object) -> NoReturn:
        raise subprocess.CalledProcessError(
            returncode, "gh attestation verify", stderr=stderr
        )

    return run


@pytest.mark.parametrize(
    ("attestor", "error"),
    [
        pytest.param(_rate_limited, VerificationError, id="unclassified-cause"),
        pytest.param(
            _untyped_verification_error, VerificationError, id="unclassified-no-cause"
        ),
        pytest.param(
            _checksum_flavoured_verification_error,
            VerificationError,
            id="deterministic-sounding-message",
        ),
        pytest.param(
            _attestor_subprocess_failure,
            subprocess.CalledProcessError,
            id="attestor-subprocess-failure",
        ),
        pytest.param(_missing_tool, FileNotFoundError, id="attestation-tool-missing"),
        pytest.param(
            "gh-could-not-run",
            subprocess.CalledProcessError,
            id="attestation-tool-outage",
        ),
        pytest.param("offline", GitHubError, id="transport-failure"),
    ],
)
def test_an_ambiguous_failure_aborts_the_run_without_blocking_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attestor: Attestor | str,
    error: type[Exception],
) -> None:
    write_policy(tmp_path)

    if attestor == "offline":

        def offline(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        client = mock_client(offline)
    else:
        client = _client([_payload("v1.2.3", 123)])
    if attestor == "gh-could-not-run":
        # `gh` never reached a verdict, so the release is unproven, not bad.
        monkeypatch.setattr(
            "gel_registry.adapters.cli.subprocess.run",
            _gh_failure(4, "authentication required"),
        )

    with client, pytest.raises(error) as raised:
        if isinstance(attestor, str):
            build_candidate(tmp_path, client)
        else:
            build_candidate(tmp_path, client, attestor=attestor)

    if attestor == "gh-could-not-run":
        assert raised.value.returncode == 4  # type: ignore[attr-defined]
    assert not (tmp_path / "promotion" / "blocked.json").exists()


def test_records_and_blocked_entries_are_emitted_in_a_deterministic_order(
    tmp_path: Path,
) -> None:
    write_policy(tmp_path)
    good_new = _payload("v2.0.0", 200)
    good_old = _payload("v1.10.0", 110)
    blocked_old = _payload("v1.2.3", 123)
    blocked_new = _payload("v1.3.0", 130)
    for release in (blocked_old, blocked_new):
        _drop_a_compressed_asset(release)

    with _client([good_new, blocked_new, good_old, blocked_old]) as client:
        candidate = build_candidate(tmp_path, client, attestor=_accept)

    assert [
        (record.version, record.source.release_id) for record in candidate.records
    ] == [("1.10.0", 110), ("2.0.0", 200)]
    assert [(entry.release_id, entry.tag) for entry in candidate.blocked] == [
        (123, "v1.2.3"),
        (130, "v1.3.0"),
    ]


def _symlinked_record(repo: Path) -> None:
    target = repo / "outside.json"
    target.write_bytes(canonical_json(release_record()))
    path = repo / "releases" / "gel-cli" / "1.2.3.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(target)


def _unexpected_path(repo: Path) -> None:
    path = repo / "releases" / "gel-cli" / "notes.txt"
    path.parent.mkdir(parents=True)
    path.write_text("not a release record")


def _noncanonical_record(repo: Path) -> None:
    path = repo / "releases" / "gel-cli" / "1.2.3.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(release_record()) + b"\n")


def _identity_mismatch(repo: Path) -> None:
    path = repo / "releases" / "gel-cli" / "1.2.4.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json(release_record("1.2.3")))


def _duplicate_identity(repo: Path) -> None:
    for version in ("1.2.3", "1.2.4"):
        path = repo / "releases" / "gel-cli" / f"{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Both records name the same upstream release id.
        path.write_bytes(canonical_json(release_record(version)))


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        pytest.param(_symlinked_record, "symlink", id="symlink"),
        pytest.param(_unexpected_path, "unexpected release path", id="unexpected-path"),
        pytest.param(_noncanonical_record, "not canonical", id="non-canonical-bytes"),
        pytest.param(
            _identity_mismatch, "does not match record identity", id="identity-mismatch"
        ),
        pytest.param(
            _duplicate_identity, "duplicate release identity", id="duplicate-identity"
        ),
    ],
)
def test_committed_release_records_must_be_exactly_what_their_path_claims(
    tmp_path: Path, prepare: Callable[[Path], None], message: str
) -> None:
    prepare(tmp_path)

    with pytest.raises(CandidateError, match=message):
        load_releases(tmp_path)


def test_the_candidate_command_finishes_discovery_before_it_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gel_registry import __main__ as cli
    from gel_registry.promote import PromotionResult

    events: list[str] = []

    class RecordingClient:
        def __enter__(self) -> RecordingClient:
            events.append("open")
            return self

        def __exit__(self, *_args: object) -> None:
            events.append("close")

    def discover(*_args: object, **_kwargs: object) -> Candidate:
        events.append("discover")
        return Candidate(base_records=(), records=(), blocked=())

    def promote(*_args: object, **_kwargs: object) -> PromotionResult:
        events.append("promote")
        return PromotionResult(
            snapshot="candidate-snapshot", branch="promote/registry", changed_paths=()
        )

    monkeypatch.setattr(cli.httpx, "Client", RecordingClient)
    monkeypatch.setattr(cli, "build_candidate", discover)
    monkeypatch.setattr(cli, "promote_candidate", promote)

    assert cli.main(["build-candidate", "--repo", str(tmp_path)]) == 0

    # The network client is closed before the repository is touched, so a
    # promotion never runs while a fetch is still in flight.
    assert events == ["open", "discover", "close", "promote"]
    assert capsys.readouterr().out == "candidate-snapshot\n"

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("discovery failed")

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("promotion must not start after a failed discovery")

    monkeypatch.setattr(cli, "build_candidate", fail)
    monkeypatch.setattr(cli, "promote_candidate", forbidden)
    assert cli.main(["build-candidate", "--repo", str(tmp_path)]) == 1
