"""Native rendering behavior against real package tools."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from gel_registry.__main__ import main

pytestmark = pytest.mark.native_tools


def render(fixture: Any) -> tuple[Path, Path, int]:
    repo, cache, _ = fixture
    out = repo / "public"
    result = main(
        [
            "native",
            "render",
            "--repo",
            str(repo),
            "--cache",
            str(cache),
            "--out",
            str(out),
        ]
    )
    return repo, out, result


def test_render_native_metadata_and_unchanged(native_repo: Any, capsys: Any) -> None:
    _, _, package = native_repo
    package("gel-cli")
    package("gel-server-7", "rpm")
    package("gel-7", arch="arm64")
    repo, out, result = render(native_repo)
    assert result == 0
    assert (
        "Package: gel-cli"
        in (out / "apt/dists/stable/main/binary-amd64/Packages").read_text()
    )
    assert (
        "Package: gel-7"
        in (out / "apt/dists/stable/main/binary-arm64/Packages").read_text()
    )
    for channel in ("stable", "testing"):
        assert (out / f"apt/dists/{channel}/Release").exists()
        for arch in ("x86_64", "aarch64"):
            assert (
                ElementTree.parse(out / f"rpm/{channel}/{arch}/repodata/repomd.xml")
                .getroot()
                .tag.endswith("repomd")
            )
    rpm_data = out / "rpm/stable/x86_64/repodata"
    primary = next(rpm_data.glob("*-primary.xml.gz"))
    root = ElementTree.fromstring(gzip.decompress(primary.read_bytes()))
    assert [node.text for node in root.findall("{*}package/{*}name")] == [
        "gel-server-7"
    ]
    lock = json.loads((repo / "native/packages.lock.json").read_bytes())
    assert {p["name"] for p in lock} == {"gel-cli", "gel-server-7", "gel-7"}
    assert {p["version"] for p in lock} == {"1:7.1-1"}
    assert all(not p["path"].startswith("pool/gelstable/") for p in lock)
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in repo.rglob("*")
        if p.is_file()
    }
    assert render(native_repo)[2] == 0
    assert "unchanged" in capsys.readouterr().out
    assert before == {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in repo.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "name,fmt,arch,signed",
    [
        ("evil", "deb", "amd64", True),
        ("gel-7", "deb", "386", True),
        ("gel-7", "rpm", "amd64", False),
    ],
)
def test_invalid_package_aborts(
    native_repo: Any, name: str, fmt: str, arch: str, signed: bool
) -> None:
    native_repo[2](name, fmt, arch, signed)
    repo, out, result = render(native_repo)
    assert result == 1
    assert not (repo / "native/packages.lock.json").exists()
    assert not (out / "apt").exists()


def test_conflicting_identity_aborts(native_repo: Any) -> None:
    package = native_repo[2]
    package(payload="first")
    package(channel="testing", payload="second")
    assert render(native_repo)[2] == 1


def test_yanked_package_is_omitted(native_repo: Any) -> None:
    repo, _, package = native_repo
    record = package()
    (repo / "native").mkdir()
    (repo / "native/yanked.json").write_text(
        json.dumps(
            [{"sha256": record["native"]["packages"][0]["sha256"], "reason": "fixture"}]
        )
    )
    assert render(native_repo)[2] == 0
    assert json.loads((repo / "native/packages.lock.json").read_bytes()) == []


def test_wrongly_signed_rpm_aborts(native_repo: Any, tmp_path: Path) -> None:
    import os
    import subprocess

    repo, _, package = native_repo
    package(fmt="rpm")
    home = tmp_path / "other-key"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Other <other@example.com>",
            "rsa2048",
            "sign",
            "0",
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    (repo / "public/keys/gelstable.asc").write_bytes(
        subprocess.check_output(
            ["gpg", "--armor", "--export", "other@example.com"], env=env
        )
    )
    assert render(native_repo)[2] == 1


def test_empty_repository_renders_both_channels(native_repo: Any) -> None:
    repo, cache, _ = native_repo
    cache.rmdir()
    assert render(native_repo)[2] == 0
    assert json.loads((repo / "native/packages.lock.json").read_bytes()) == []


def test_manual_apt_install(native_repo: Any, tmp_path: Path) -> None:
    """Exercise apt's index/hash/path resolution and install a tiny fixture."""
    import os
    import shutil
    import subprocess

    if os.geteuid() != 0:
        pytest.skip("apt install acceptance needs a disposable root container")
    repo, cache, package = native_repo
    record = package(
        "gel-cli", arch="arm64" if os.uname().machine == "aarch64" else "amd64"
    )
    assert render(native_repo)[2] == 0
    digest = record["native"]["packages"][0]["sha256"]
    pool = repo / "public/apt/pool/gel-cli/pkg-1"
    pool.mkdir(parents=True)
    shutil.copyfile(cache / digest, pool / "gel-cli-1.deb")
    sources = tmp_path / "sources.list"
    sources.write_text(f"deb [trusted=yes] file:{repo}/public/apt stable main\n")
    lists = tmp_path / "lists"
    lists.mkdir()
    args = [
        "apt-get",
        "-o",
        f"Dir::Etc::sourcelist={sources}",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        f"Dir::State::lists={lists}",
        "-o",
        "APT::Sandbox::User=root",
    ]
    try:
        subprocess.run([*args, "update"], check=True, capture_output=True)
        subprocess.run(
            [*args, "install", "-y", "gel-cli"], check=True, capture_output=True
        )
        assert Path("/usr/share/gel-cli/fixture").read_text() == "hello"
    finally:
        subprocess.run(["dpkg", "--purge", "gel-cli"], check=False, capture_output=True)


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
@pytest.mark.parametrize("channel", ["stable", "testing"])
def test_yank_preserves_identity_reservation(
    native_repo: Any, fmt: str, channel: str, capsys: Any
) -> None:
    repo, _, package = native_repo
    record = package(fmt=fmt, payload="original")
    assert render(native_repo)[2] == 0
    (repo / "native/yanked.json").write_text(
        json.dumps(
            [
                {
                    "sha256": record["native"]["packages"][0]["sha256"],
                    "reason": "withdrawn",
                }
            ]
        )
    )
    assert render(native_repo)[2] == 0
    assert json.loads((repo / "native/packages.lock.json").read_bytes()) == []
    # A duplicate record for the exact bytes remains accepted in either channel.
    duplicate = json.loads(json.dumps(record))
    duplicate["native"]["channel"] = channel
    (repo / "releases/gelstable/gel/duplicate.json").write_text(json.dumps(duplicate))
    assert render(native_repo)[2] == 0
    package(fmt=fmt, channel=channel, payload="replacement")
    assert render(native_repo)[2] == 1
    assert "conflicting native package identity" in capsys.readouterr().err
    assert json.loads((repo / "native/packages.lock.json").read_bytes()) == []
    (repo / "releases/gelstable/gel/2.json").unlink()
    package(fmt=fmt, channel=channel, payload="replacement", revision="2")
    assert render(native_repo)[2] == 0
    lock = json.loads((repo / "native/packages.lock.json").read_bytes())
    assert len(lock) == 1
    assert lock[0]["version"] == "1:7.1-2"
    assert lock[0]["channel"] == channel


def test_yanked_rpm_reservation_survives_trust_key_rotation(
    native_repo: Any, tmp_path: Path, capsys: Any
) -> None:
    import os
    import subprocess

    repo, _, package = native_repo
    record = package(fmt="rpm")
    assert render(native_repo)[2] == 0
    env = {**os.environ, "GNUPGHOME": str(tmp_path / "gnupg")}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Replacement <replacement@example.com>",
            "rsa2048",
            "sign",
            "0",
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    (repo / "public/keys/gelstable.asc").write_bytes(
        subprocess.check_output(
            ["gpg", "--armor", "--export", "replacement@example.com"], env=env
        )
    )
    # A withdrawn key cannot authenticate live RPMs, including on an unchanged lock.
    assert render(native_repo)[2] == 1
    (repo / "native/yanked.json").write_text(
        json.dumps(
            [
                {
                    "sha256": record["native"]["packages"][0]["sha256"],
                    "reason": "key withdrawn",
                }
            ]
        )
    )
    assert render(native_repo)[2] == 0
    assert json.loads((repo / "native/packages.lock.json").read_bytes()) == []
    package(
        fmt="rpm",
        payload="replacement",
        channel="testing",
        signer="replacement@example.com",
    )
    assert render(native_repo)[2] == 1
    assert "conflicting native package identity" in capsys.readouterr().err
    (repo / "releases/gelstable/gel/2.json").unlink()
    package(
        fmt="rpm", payload="replacement", revision="2", signer="replacement@example.com"
    )
    assert render(native_repo)[2] == 0
    lock = json.loads((repo / "native/packages.lock.json").read_bytes())
    assert len(lock) == 1 and lock[0]["version"] == "1:7.1-2"
    package(fmt="rpm", revision="3", signed=False)
    assert render(native_repo)[2] == 1


def test_yanked_bytes_still_require_manifest_digest(
    native_repo: Any, monkeypatch: Any, capsys: Any
) -> None:
    from io import BytesIO

    repo, cache, package = native_repo
    record = package()
    assert render(native_repo)[2] == 0
    digest = record["native"]["packages"][0]["sha256"]
    (repo / "native/yanked.json").write_text(
        json.dumps([{"sha256": digest, "reason": "withdrawn"}])
    )
    blob = cache / digest
    raw = blob.read_bytes()
    blob.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    # Serve the same corrupted bytes locally; no GitHub request in this regression.
    monkeypatch.setattr(
        "gel_registry.native.fetch.urlopen",
        lambda url, timeout: BytesIO(blob.read_bytes()),
    )
    assert render(native_repo)[2] == 1
    assert "native package SHA-256 or size mismatch" in capsys.readouterr().err
