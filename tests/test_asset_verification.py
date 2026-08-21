"""Fetching release assets and proving they are what the release claims.

Downloading is the only point where the registry pulls attacker-influenced
bytes, so it is fenced on every side: assets must belong to the requested
repository, sizes are bounded both as declared and as streamed, and any
credential the client carries is dropped the moment a redirect leaves GitHub's
API origin. Verification then binds the bytes to the tag: digests must match,
the zstd artifact must decompress to the identity artifact byte-for-byte, and
`gh attestation verify` must accept every canonical asset.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import zstandard as zstd
from pydantic import ValidationError
from support import github_release, mock_client

import gel_registry.github.assets as assets_module
from gel_registry.adapters import DeterministicVerificationError, adapter_for
from gel_registry.constants import CLI_PLATFORMS
from gel_registry.github import (
    GITHUB_REPOSITORY,
    GitHubAsset,
    GitHubError,
    GitHubRelease,
    canonical_asset_names,
    discover_cli_releases,
    discover_repository_releases,
    download_assets,
)
from gel_registry.policy import ProductPolicy
from gel_registry.verify import VerificationError, verify_cli_release

CDN = "release-assets.githubusercontent.com"


def _release(repository: str = GITHUB_REPOSITORY) -> GitHubRelease:
    payload = github_release(owner=repository)
    assets = payload["assets"]
    assert isinstance(assets, list)
    for asset in assets:
        assert isinstance(asset, dict)
        asset["url"] = f"https://api.github.com/repos/{repository}/assets/{asset['id']}"
        asset["browser_download_url"] = (
            f"https://github.com/{repository}/releases/download/v1.2.3/{asset['name']}"
        )
    with mock_client(lambda _request: httpx.Response(200, json=[payload])) as client:
        if repository == GITHUB_REPOSITORY:
            return discover_cli_releases(client, known_versions=())[0]
        return discover_repository_releases(client, repository)[0]


def _cli_policy() -> ProductPolicy:
    return ProductPolicy(
        product="gel-cli",
        repository="gelstable/gel-cli",
        adapter="gel-cli",
        channel="stable",
        tag_pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+$",
        platforms=CLI_PLATFORMS,
        encodings=("identity", "zstd"),
    )


def _local_release() -> GitHubRelease:
    """A release whose assets are addressed the way the CLI adapter expects."""

    assets: list[GitHubAsset] = []
    for index, platform in enumerate(CLI_PLATFORMS, 1):
        suffix = ".exe" if platform.endswith("-windows-msvc") else ""
        identity = f"gel-cli-{platform}{suffix}"
        for offset, name in ((0, identity), (10, f"{identity}.zst")):
            assets.append(
                GitHubAsset(
                    name=name,
                    url=(
                        "https://api.github.com/repos/gelstable/gel-cli/assets/"
                        f"{index + offset}"
                    ),
                    browser_download_url=(
                        "https://github.com/gelstable/gel-cli/releases/download/"
                        f"v1.2.3/{name}"
                    ),
                )
            )
    return GitHubRelease(id=123, tag_name="v1.2.3", assets=tuple(assets))


def _local_files(tmp_path: Path, release: GitHubRelease) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for index, asset in enumerate(release.assets):
        identity = asset.name.removesuffix(".zst")
        payload = f"payload-{identity}".encode()
        data = (
            zstd.ZstdCompressor().compress(payload)
            if asset.name.endswith(".zst")
            else payload
        )
        path = tmp_path / str(index)
        path.write_bytes(data)
        files[asset.name] = path
    return files


def test_downloading_streams_the_exact_bytes_of_the_requested_repository(
    tmp_path: Path,
) -> None:
    repository = "gelstable/gel"
    release = _release(repository)
    payloads = {asset.name: f"bytes:{asset.name}".encode() for asset in release.assets}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith(f"/repos/{repository}/assets/")
        assert request.headers["Accept"] == "application/octet-stream"
        asset_id = int(request.url.path.rsplit("/", 1)[-1])
        name = next(asset.name for asset in release.assets if asset.id == asset_id)
        return httpx.Response(200, content=payloads[name])

    with mock_client(handler) as client:
        paths = download_assets(
            client, release, tmp_path / "assets", repository=repository
        )

    assert {name: path.read_bytes() for name, path in paths.items()} == payloads

    # A declared size is optional, but a malformed one is never tolerated.
    base = {
        "name": "asset",
        "url": "https://api.github.com/repos/gelstable/gel-cli/assets/1",
    }
    assert GitHubAsset.model_validate(base).size is None
    assert GitHubAsset.model_validate({**base, "size": None}).size is None
    for size in (True, -1, "1", 1.5):
        with pytest.raises(ValidationError):
            GitHubAsset.model_validate({**base, "size": size})


def test_request_credentials_are_dropped_on_a_cross_origin_redirect(
    tmp_path: Path,
) -> None:
    release = _release()
    observed: list[tuple[str, tuple[str | None, ...]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        observed.append(
            (
                host,
                (
                    request.headers.get("Authorization"),
                    request.headers.get("Proxy-Authorization"),
                    request.headers.get("Cookie"),
                ),
            )
        )
        if host == "api.github.com":
            return httpx.Response(302, headers={"Location": f"https://{CDN}/asset"})
        if request.url.path == "/asset":
            # A second hop, still cross-origin, must not restore the headers.
            return httpx.Response(302, headers={"Location": f"https://{CDN}/final"})
        return httpx.Response(200, content=b"asset")

    with httpx.Client(
        headers={
            "Authorization": "Bearer secret",
            "Proxy-Authorization": "Basic proxy-secret",
            "Cookie": "session=secret",
        },
        transport=httpx.MockTransport(handler),
    ) as client:
        download_assets(
            client, release, tmp_path / "assets", repository=GITHUB_REPOSITORY
        )

    api = [values for host, values in observed if host == "api.github.com"]
    cdn = [values for host, values in observed if host == CDN]
    assert api and cdn
    assert all(
        values == ("Bearer secret", "Basic proxy-secret", "session=secret")
        for values in api
    )
    assert all(values == (None, None, None) for values in cdn)


def test_client_authentication_is_dropped_on_a_cross_origin_redirect(
    tmp_path: Path,
) -> None:
    release = _release()
    observed: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        observed.append((host, request.headers.get("Authorization")))
        if host == "api.github.com":
            return httpx.Response(302, headers={"Location": f"https://{CDN}/asset"})
        return httpx.Response(200, content=b"asset")

    with httpx.Client(
        auth=("user", "secret"), transport=httpx.MockTransport(handler)
    ) as client:
        download_assets(
            client, release, tmp_path / "assets", repository=GITHUB_REPOSITORY
        )

    api = [value for host, value in observed if host == "api.github.com"]
    cdn = [value for host, value in observed if host == CDN]
    assert api and cdn
    assert all(value is not None for value in api)
    assert all(value is None for value in cdn)


def _server_error(
    _monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]]:
    return _release(), lambda _request: httpx.Response(500), {}


def _unsafe_redirect(
    _monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]]:
    return (
        _release(),
        lambda _request: httpx.Response(
            302, headers={"Location": "https://evil.example/payload"}
        ),
        {},
    )


def _foreign_repository(
    _monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]]:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a foreign asset must not be requested: {request.url}")

    return _release(), refuse, {"repository": "gelstable/other"}


def _declared_asset_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]]:
    monkeypatch.setattr(assets_module, "MAX_ASSET_BYTES", 4, raising=False)
    monkeypatch.setattr(assets_module, "MAX_TOTAL_ASSET_BYTES", 100, raising=False)
    release = _release()
    oversized = release.assets[0].model_copy(update={"size": 5})
    release = release.model_copy(update={"assets": (oversized, *release.assets[1:])})

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a declared oversized asset must not be requested")

    return release, refuse, {}


def _declared_aggregate_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]]:
    monkeypatch.setattr(assets_module, "MAX_ASSET_BYTES", 10, raising=False)
    monkeypatch.setattr(assets_module, "MAX_TOTAL_ASSET_BYTES", 15, raising=False)
    release = _release()
    selected = tuple(
        asset.model_copy(update={"size": 8}) for asset in release.assets[:5]
    )
    release = release.model_copy(update={"assets": (*selected, *release.assets[5:])})

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a declared oversized aggregate must not be requested")

    return release, refuse, {}


def _streamed_asset_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]]:
    monkeypatch.setattr(assets_module, "MAX_ASSET_BYTES", 4, raising=False)
    monkeypatch.setattr(assets_module, "MAX_TOTAL_ASSET_BYTES", 100, raising=False)
    release = _release()
    asset = release.assets[0].model_copy(update={"size": None})
    release = release.model_copy(update={"assets": (asset,)})
    return (
        release,
        lambda _request: httpx.Response(200, content=b"12345"),
        {"names": (asset.name,)},
    )


def _streamed_aggregate_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]]:
    monkeypatch.setattr(assets_module, "MAX_ASSET_BYTES", 10, raising=False)
    monkeypatch.setattr(assets_module, "MAX_TOTAL_ASSET_BYTES", 5, raising=False)
    release = _release()
    selected = tuple(
        asset.model_copy(update={"size": None}) for asset in release.assets[:2]
    )
    names = tuple(asset.name for asset in selected)
    release = release.model_copy(update={"assets": selected})
    return (
        release,
        lambda _request: httpx.Response(200, content=b"1234"),
        {"names": names},
    )


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        pytest.param(_server_error, "", id="http-failure"),
        pytest.param(_unsafe_redirect, "redirect", id="unsafe-redirect"),
        pytest.param(_foreign_repository, "", id="foreign-repository"),
        pytest.param(_declared_asset_oversize, "maximum", id="declared-asset-oversize"),
        pytest.param(
            _declared_aggregate_oversize, "aggregate", id="declared-aggregate-oversize"
        ),
        pytest.param(_streamed_asset_oversize, "maximum", id="streamed-asset-oversize"),
        pytest.param(
            _streamed_aggregate_oversize, "aggregate", id="streamed-aggregate-oversize"
        ),
    ],
)
def test_a_refused_download_leaves_no_partial_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[
        [pytest.MonkeyPatch],
        tuple[
            GitHubRelease, Callable[[httpx.Request], httpx.Response], dict[str, object]
        ],
    ],
    message: str,
) -> None:
    release, handler, kwargs = prepare(monkeypatch)
    destination = tmp_path / "assets"

    with (
        mock_client(handler) as client,
        pytest.raises(GitHubError, match=message or None),
    ):
        download_assets(client, release, destination, **kwargs)  # type: ignore[arg-type]

    assert not destination.exists()


def _corrupt_identity(release: GitHubRelease, files: dict[str, Path]) -> GitHubRelease:
    files[release.assets[0].name].write_bytes(b"changed")
    return release


def _break_the_compressed_pair(
    release: GitHubRelease, files: dict[str, Path]
) -> GitHubRelease:
    name = next(asset.name for asset in release.assets if asset.name.endswith(".zst"))
    files[name].write_bytes(zstd.ZstdCompressor().compress(b"different"))
    return release


def _foreign_release_repository(
    release: GitHubRelease, _files: dict[str, Path]
) -> GitHubRelease:
    return GitHubRelease(
        id=release.id,
        tag_name=release.tag_name,
        assets=release.assets,
        repository="fork/gel-cli",
    )


def _spoof_a_canonical_download_url(
    release: GitHubRelease, _files: dict[str, Path]
) -> GitHubRelease:
    spoofed = release.assets[0].model_copy(
        update={"browser_download_url": "https://evil.example/canonical"}
    )
    return release.model_copy(update={"assets": (spoofed, *release.assets[1:])})


@pytest.mark.parametrize(
    ("tamper", "attests", "expected"),
    [
        pytest.param(None, True, None, id="valid-release"),
        pytest.param(_corrupt_identity, True, VerificationError, id="identity-digest"),
        pytest.param(
            _break_the_compressed_pair, True, VerificationError, id="zstd-identity-pair"
        ),
        pytest.param(
            _foreign_release_repository,
            True,
            VerificationError,
            id="foreign-repository",
        ),
        pytest.param(None, False, RuntimeError, id="negative-attestation"),
        pytest.param(
            _spoof_a_canonical_download_url,
            True,
            DeterministicVerificationError,
            id="spoofed-download-url",
        ),
    ],
)
def test_verification_binds_every_canonical_asset_to_the_release(
    tmp_path: Path,
    tamper: Callable[[GitHubRelease, dict[str, Path]], GitHubRelease] | None,
    attests: bool,
    expected: type[Exception] | None,
) -> None:
    release = _local_release()
    files = _local_files(tmp_path, release)

    # A sidecar may point anywhere, because it is never a canonical asset and
    # is never attested.
    sidecar = "gel-cli-x86_64-unknown-linux-musl.sha256"
    (tmp_path / sidecar).write_bytes(b"checksums")
    files[sidecar] = tmp_path / sidecar
    release = release.model_copy(
        update={
            "assets": (
                *release.assets,
                GitHubAsset(
                    name=sidecar,
                    url="https://api.github.com/repos/gelstable/gel-cli/assets/999",
                    browser_download_url=f"https://evil.example/{sidecar}",
                ),
            )
        }
    )
    if tamper is not None:
        release = tamper(release, files)

    attested: list[Path] = []

    def attestor(path: Path) -> None:
        if not attests:
            raise RuntimeError("attestation rejected the asset")
        attested.append(path)

    if expected is not None:
        with pytest.raises(expected):
            adapter_for("gel-cli").verify(_cli_policy(), release, files, attestor)
        return

    record = adapter_for("gel-cli").verify(_cli_policy(), release, files, attestor)

    assert record.version == "1.2.3"
    assert [
        (artifact.platform, artifact.encoding) for artifact in record.artifacts
    ] == sorted(
        (platform, encoding)
        for platform in CLI_PLATFORMS
        for encoding in ("identity", "zstd")
    )
    assert [artifact.media_type for artifact in record.artifacts] == [
        "application/x-mach-binary",
        "application/x-mach-binary",
        "application/x-dosexec",
        "application/x-dosexec",
        "application/x-pie-executable",
        "application/x-pie-executable",
        "application/x-dosexec",
        "application/x-dosexec",
        "application/x-pie-executable",
        "application/x-pie-executable",
    ]
    assert set(attested) == {files[name] for name in canonical_asset_names()}
    assert files[sidecar] not in attested

    # The same verification runs through the product entry point the candidate
    # builder uses, with the real attestor swapped out.
    assert verify_cli_release(_local_release(), files, lambda _path: None).version == (
        "1.2.3"
    )
