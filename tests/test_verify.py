from __future__ import annotations

from pathlib import Path

import pytest
import zstandard as zstd

from gel_registry.constants import CLI_PLATFORMS
from gel_registry.github import GitHubAsset, GitHubRelease
from gel_registry.verify import VerificationError, verify_cli_release


def _release() -> GitHubRelease:
    assets: list[GitHubAsset] = []
    for index, platform in enumerate(CLI_PLATFORMS, 1):
        suffix = ".exe" if platform.endswith("-windows-msvc") else ""
        identity = f"gel-cli-{platform}{suffix}"
        assets.extend(
            (
                GitHubAsset(
                    name=identity,
                    url=f"https://api.github.com/repos/gelstable/gel-cli/assets/{index}",
                    browser_download_url=(
                        "https://github.com/gelstable/gel-cli/releases/download/"
                        f"v1.2.3/{identity}"
                    ),
                ),
                GitHubAsset(
                    name=f"{identity}.zst",
                    url=(
                        "https://api.github.com/repos/gelstable/gel-cli/assets/"
                        f"{index + 10}"
                    ),
                    browser_download_url=(
                        "https://github.com/gelstable/gel-cli/releases/download/"
                        f"v1.2.3/{identity}.zst"
                    ),
                ),
            )
        )
    return GitHubRelease(id=123, tag_name="v1.2.3", assets=tuple(assets))


def _files(tmp_path: Path, release: GitHubRelease) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for index, asset in enumerate(release.assets):
        identity = asset.name.removesuffix(".zst")
        if asset.name.endswith(".zst"):
            data = f"payload-{identity}".encode()
            data = zstd.ZstdCompressor().compress(data)
        else:
            data = f"payload-{asset.name}".encode()
        path = tmp_path / str(index)
        path.write_bytes(data)
        result[asset.name] = path
    sidecar = tmp_path / "checksums.txt"
    sidecar.write_bytes(b"checksums")
    result["checksums.txt"] = sidecar
    return result


def test_verify_cli_release_checks_bytes_pairs_media_types_and_attestations(
    tmp_path: Path,
) -> None:
    release = _release()
    assets = _files(tmp_path, release)
    assets.pop("checksums.txt")
    sidecar_name = "gel-cli-x86_64-unknown-linux-musl.sha256"
    (tmp_path / sidecar_name).write_bytes(b"checksums")
    assets[sidecar_name] = tmp_path / sidecar_name
    release = GitHubRelease(
        id=release.id,
        tag_name=release.tag_name,
        assets=(
            *release.assets,
            GitHubAsset(
                name=sidecar_name,
                url="https://api.github.com/repos/gelstable/gel-cli/assets/999",
                browser_download_url=(
                    "https://github.com/gelstable/gel-cli/releases/download/"
                    f"v1.2.3/{sidecar_name}"
                ),
            ),
        ),
    )
    calls: list[Path] = []

    record = verify_cli_release(release, assets, calls.append)

    assert record.version == "1.2.3"
    assert len(record.artifacts) == 10
    assert [artifact.encoding for artifact in record.artifacts] == [
        encoding for _ in CLI_PLATFORMS for encoding in ("identity", "zstd")
    ]
    assert len(calls) == len(release.assets)
    assert all(path.is_file() for path in calls)
    assert all(artifact.media_type for artifact in record.artifacts)


@pytest.mark.parametrize(
    "case",
    ["digest", "pair", "repo", "attestation"],
)
def test_verify_cli_release_fails_closed(tmp_path: Path, case: str) -> None:
    release = _release()
    assets = _files(tmp_path, release)

    def attestor(path: Path) -> None:
        return None

    if case == "digest":
        assets[release.assets[0].name].write_bytes(b"changed")
    elif case == "pair":
        zst_asset = next(
            asset for asset in release.assets if asset.name.endswith(".zst")
        )
        assets[zst_asset.name].write_bytes(zstd.ZstdCompressor().compress(b"different"))
    elif case == "repo":
        release = GitHubRelease(
            id=release.id,
            tag_name=release.tag_name,
            assets=release.assets,
            repository="fork/gel-cli",
        )
    elif case == "attestation":

        def fail(path: Path) -> None:
            raise RuntimeError("bad attestation")

        attestor = fail
    with pytest.raises((VerificationError, RuntimeError)):
        verify_cli_release(release, assets, attestor)
