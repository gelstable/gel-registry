"""Normal promotions reuse old package metadata, including after withdrawals."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from gel_registry.native.render import render_native
from gel_registry.validation.native import validate_native_structure

from .test_render import render

pytestmark = pytest.mark.native_tools


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_new_version_never_fetches_old_bytes(
    native_repo: Any, monkeypatch: Any, fmt: str
) -> None:
    repo, cache, package = native_repo
    first = package(fmt=fmt)
    assert render(native_repo)[2] == 0
    old_digest = first["native"]["packages"][0]["sha256"]
    (cache / old_digest).unlink()
    package(fmt=fmt, revision="2")
    from gel_registry.native import render as rendering
    from gel_registry.native.fetch import fetch_package

    fetch = fetch_package

    def new_only(url: str, digest: str, size: int, cache: Path) -> Path:
        assert digest != old_digest, "requested already published package"
        return fetch(url, digest, size, cache)

    monkeypatch.setattr(rendering, "fetch_package", new_only)
    assert render(native_repo)[2] == 0
    entries = json.loads((repo / "native/packages.lock.json").read_bytes())
    assert {entry["version"] for entry in entries} == {"1:7.1-1", "1:7.1-2"}
    validate_native_structure(repo)


@pytest.mark.parametrize("damage", ["apt", "rpm", "packages", "extra", "signature"])
def test_unchanged_lock_repairs_metadata_without_package_bytes(
    native_repo: Any, monkeypatch: Any, damage: str
) -> None:
    repo, cache, package = native_repo
    package()
    package(fmt="rpm")
    assert render(native_repo)[2] == 0
    shutil.rmtree(cache)
    if damage in {"apt", "rpm"}:
        shutil.rmtree(repo / "public" / damage)
    elif damage == "packages":
        (repo / "public/apt/dists/stable/main/binary-amd64/Packages").write_text("bad")
    elif damage == "extra":
        (repo / "public/rpm/extra").write_text("bad")
    # Unsigned metadata also requests signing again rather than a false no-op.
    monkeypatch.setattr(
        "gel_registry.native.render.fetch_package",
        lambda *args: pytest.fail("repair fetched old bytes"),
    )
    assert render_native(repo, cache, repo / "public") is True
    validate_native_structure(repo)


def test_base_url_change_reuses_packages(native_repo: Any, monkeypatch: Any) -> None:
    repo, cache, package = native_repo
    package(fmt="rpm")
    assert render(native_repo)[2] == 0
    shutil.rmtree(cache)
    monkeypatch.setattr(
        "gel_registry.native.render.fetch_package",
        lambda *args: pytest.fail("URL change fetched old bytes"),
    )
    assert render_native(repo, cache, repo / "public", "https://example.com") is True
    primary = next(
        (repo / "public/rpm/stable/x86_64/repodata").glob("*-primary.xml.gz")
    )
    import gzip

    assert b"https://example.com/rpm/" in gzip.decompress(primary.read_bytes())


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_yank_retains_identity_without_fetching_withdrawn_bytes(
    native_repo: Any, monkeypatch: Any, fmt: str
) -> None:
    repo, cache, package = native_repo
    old = package(fmt=fmt)
    assert render(native_repo)[2] == 0
    digest = old["native"]["packages"][0]["sha256"]
    (repo / "native/yanked.json").write_text(
        json.dumps([{"sha256": digest, "reason": "withdrawn"}])
    )
    (cache / digest).unlink()
    from gel_registry.native import render as rendering
    from gel_registry.native.fetch import fetch_package

    fetch = fetch_package

    def live_only(url: str, digest_arg: str, size: int, cache: Path) -> Path:
        assert digest_arg != digest
        return fetch(url, digest_arg, size, cache)

    monkeypatch.setattr(rendering, "fetch_package", live_only)
    assert render(native_repo)[2] == 0
    assert json.loads((repo / "native/packages.lock.json").read_bytes()) == []
    package(fmt=fmt, payload="replacement")
    assert render(native_repo)[2] == 1


def test_debian_zero_epoch_roundtrip(native_repo: Any) -> None:
    import hashlib
    import subprocess

    repo, cache, package = native_repo
    record = package()
    asset = record["native"]["packages"][0]
    old = cache / asset["sha256"]
    unpacked = repo.parent / "unpacked"
    subprocess.run(
        ["dpkg-deb", "-R", str(old), str(unpacked)], check=True, capture_output=True
    )
    control = unpacked / "DEBIAN/control"
    control.write_text(
        control.read_text().replace("Version: 1:7.1-1", "Version: 0:7.1-1")
    )
    updated = repo.parent / "zero.deb"
    subprocess.run(
        ["dpkg-deb", "-b", str(unpacked), str(updated)], check=True, capture_output=True
    )
    raw = updated.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    (cache / digest).write_bytes(raw)
    asset.update(sha256=digest, size=len(raw))
    (repo / "releases/gelstable/gel/1.json").write_text(json.dumps(record))
    assert render(native_repo)[2] == 0
    assert (
        json.loads((repo / "native/packages.lock.json").read_bytes())[0]["version"]
        == "0:7.1-1"
    )
    validate_native_structure(repo)


def test_certificate_update_rechecks_live_rpm_only(
    native_repo: Any, monkeypatch: Any
) -> None:
    import os
    import subprocess

    repo, cache, package = native_repo
    deb = package()
    rpm = package(fmt="rpm")
    assert render(native_repo)[2] == 0
    deb_digest = deb["native"]["packages"][0]["sha256"]
    rpm_digest = rpm["native"]["packages"][0]["sha256"]
    (cache / deb_digest).unlink()
    home = repo.parent / "gnupg"
    env = {**os.environ, "GNUPGHOME": str(home)}
    listing = subprocess.check_output(
        ["gpg", "--with-colons", "--list-secret-keys"], env=env
    ).decode()
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-add-key",
            fingerprint,
            "rsa2048",
            "sign",
            "0",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    (repo / "public/keys/gelstable.asc").write_bytes(
        subprocess.check_output(["gpg", "--armor", "--export"], env=env)
    )
    from gel_registry.native.fetch import fetch_package

    seen = []

    def rpm_only(url: str, digest: str, size: int, cache: Path) -> Path:
        seen.append(digest)
        assert digest == rpm_digest
        return fetch_package(url, digest, size, cache)

    monkeypatch.setattr("gel_registry.native.render.fetch_package", rpm_only)
    assert render(native_repo)[2] == 0
    assert seen == [rpm_digest]
    validate_native_structure(repo)


def test_changed_release_options_rebuilds_without_blobs(
    native_repo: Any, monkeypatch: Any
) -> None:
    from gel_registry.native.metadata import RELEASE_OPTIONS

    repo, cache, package = native_repo
    package()
    assert render(native_repo)[2] == 0
    shutil.rmtree(cache)
    monkeypatch.setitem(RELEASE_OPTIONS, "Label", "Updated label")
    monkeypatch.setattr(
        "gel_registry.native.render.fetch_package",
        lambda *args: pytest.fail("options change fetched old bytes"),
    )
    assert render_native(repo, cache, repo / "public") is True
    assert (
        "Label: Updated label" in (repo / "public/apt/dists/stable/Release").read_text()
    )
