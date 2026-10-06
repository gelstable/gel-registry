"""Signed repository consistency against real native tools and disposable keys."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gel_registry.__main__ import main
from gel_registry.validation.native import validate_native

from .test_render import render

pytestmark = pytest.mark.native_tools


def sign(repo: Path, home: Path, monkeypatch: Any) -> int:
    monkeypatch.setenv("GNUPGHOME", str(home))
    listing = subprocess.check_output(["gpg", "--with-colons", "--list-secret-keys"])
    fingerprint = next(
        line.split(":")[9]
        for line in listing.decode().splitlines()
        if line.startswith("fpr:")
    )
    monkeypatch.setenv("GELSTABLE_SIGNING_FPR", fingerprint)
    return main(["native", "sign", "--repo", str(repo)])


def signed(native_repo: Any, monkeypatch: Any) -> Path:
    native_repo[2]("gel-cli")
    native_repo[2]("gel-server-7", "rpm")
    repo, _, result = render(native_repo)
    assert result == 0
    assert sign(repo, repo.parent / "gnupg", monkeypatch) == 0
    validate_native(repo)
    return repo


def test_signed_fixture_passes(native_repo: Any, monkeypatch: Any) -> None:
    repo = signed(native_repo, monkeypatch)
    # Public-only validation ignores the operator's home and signing selector.
    monkeypatch.setenv("GNUPGHOME", "/nonexistent")
    monkeypatch.delenv("GELSTABLE_SIGNING_FPR")
    validate_native(repo)


@pytest.mark.parametrize(
    "tamper", ["packages", "repomd", "missing", "pool", "lock", "record"]
)
def test_tampering_fails(native_repo: Any, monkeypatch: Any, tamper: str) -> None:
    repo = signed(native_repo, monkeypatch)
    public = repo / "public"
    if tamper == "packages":
        path = public / "apt/dists/stable/main/binary-amd64/Packages"
        path.write_bytes(path.read_bytes() + b"\n")
    elif tamper == "repomd":
        path = public / "rpm/stable/x86_64/repodata/repomd.xml"
        path.write_bytes(path.read_bytes() + b"\n")
    elif tamper == "missing":
        (public / "apt/dists/stable/Release.gpg").unlink()
    elif tamper == "pool":
        path = public / "apt/pool/extra.deb"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"extra")
    elif tamper == "lock":
        (repo / "native/packages.lock.json").write_text("[]\n")
    else:
        path = next((repo / "releases").rglob("*.json"))
        record = json.loads(path.read_bytes())
        record["native"]["packages"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(record))
    with pytest.raises((ValueError, OSError)):
        validate_native(repo)


def test_other_signer_rejected(
    native_repo: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    repo = signed(native_repo, monkeypatch)
    home = tmp_path / "other"
    home.mkdir(mode=0o700)
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
        env={**os.environ, "GNUPGHOME": str(home)},
        check=True,
        capture_output=True,
    )
    assert sign(repo, home, monkeypatch) == 1
    with pytest.raises(ValueError):
        validate_native(repo)


def test_yanked_and_empty_repositories(native_repo: Any, monkeypatch: Any) -> None:
    repo, _, package = native_repo
    record = package()
    (repo / "native").mkdir()
    (repo / "native/yanked.json").write_text(
        json.dumps(
            [{"sha256": record["native"]["packages"][0]["sha256"], "reason": "fixture"}]
        )
    )
    assert render(native_repo)[2] == 0
    assert sign(repo, repo.parent / "gnupg", monkeypatch) == 0
    validate_native(repo)


def test_signing_subkey_and_validation_registration(
    native_repo: Any, monkeypatch: Any
) -> None:
    from gel_registry.validation.local import validate_local

    repo, _, _ = native_repo
    home = repo.parent / "gnupg"
    monkeypatch.setenv("GNUPGHOME", str(home))
    listing = subprocess.check_output(["gpg", "--with-colons", "--list-secret-keys"])
    fingerprint = next(
        line.split(":")[9]
        for line in listing.decode().splitlines()
        if line.startswith("fpr:")
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
        check=True,
        capture_output=True,
    )
    (repo / "public/keys/gelstable.asc").write_bytes(
        subprocess.check_output(["gpg", "--armor", "--export"])
    )
    assert render(native_repo)[2] == 0
    listing = subprocess.check_output(["gpg", "--with-colons", "--list-secret-keys"])
    subkey = [
        line.split(":")[9]
        for line in listing.decode().splitlines()
        if line.startswith("fpr:")
    ][-1]
    monkeypatch.setenv("GELSTABLE_SIGNING_FPR", subkey)
    assert main(["native", "sign", "--repo", str(repo)]) == 0
    report = validate_local(repo)
    assert "native.integrity" in report.checks
    assert not any(error.startswith("native.integrity:") for error in report.errors)


def test_signed_cleartext_must_match_release(
    native_repo: Any, monkeypatch: Any
) -> None:
    repo = signed(native_repo, monkeypatch)
    release = repo / "public/apt/dists/stable/Release"
    release.write_bytes(release.read_bytes() + b"Origin: Other\n")
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--detach-sign",
            "--output",
            str(release.with_name("Release.gpg")),
            str(release),
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match="cleartext"):
        validate_native(repo)


@pytest.mark.parametrize(
    ("framing", "accepted"),
    [
        ("extra-newline", True),
        ("missing-newline", True),
        ("two-newlines", False),
        ("crlf", False),
        ("changed-content", False),
    ],
)
def test_gpgv_cleartext_final_newline_is_compatible(
    native_repo: Any, monkeypatch: Any, framing: str, accepted: bool
) -> None:
    from gel_registry.native import sign as signing

    repo = signed(native_repo, monkeypatch)
    command = signing.command

    def framed_output(args: list[str]) -> bytes:
        status = command(args)
        if args[0] == "gpgv" and "--output" in args:
            output = Path(args[args.index("--output") + 1])
            data = output.read_bytes()
            variants = {
                "extra-newline": data + b"\n",
                "missing-newline": data.removesuffix(b"\n"),
                "two-newlines": data + b"\n\n",
                "crlf": data.replace(b"\n", b"\r\n"),
                "changed-content": b"Origin: Other\n" + data,
            }
            output.write_bytes(variants[framing])
        return status

    # Real signatures are verified first. Simulate only the framing newline
    # difference between GnuPG versions, leaving detached verification intact.
    monkeypatch.setattr(signing, "command", framed_output)
    if not accepted:
        with pytest.raises(ValueError, match="cleartext"):
            validate_native(repo)
        assert sign(repo, repo.parent / "gnupg", monkeypatch) == 1
        return
    validate_native(repo)
    assert sign(repo, repo.parent / "gnupg", monkeypatch) == 0
    release = repo / "public/apt/dists/stable/Release"
    release.write_bytes(release.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        validate_native(repo)


def test_native_records_require_bootstrap(native_repo: Any) -> None:
    from gel_registry.validation.local import validate_local

    repo, _, package = native_repo
    package()
    report = validate_local(repo)
    assert any(error.startswith("native.integrity:") for error in report.errors)


@pytest.mark.parametrize("metadata", ["apt", "rpm"])
def test_resigned_metadata_must_match_lock(
    native_repo: Any, monkeypatch: Any, metadata: str
) -> None:
    import gzip
    import hashlib
    from xml.etree import ElementTree as ET

    repo = signed(native_repo, monkeypatch)
    if metadata == "apt":
        path = repo / "public/apt/dists/stable/main/binary-amd64/Packages"
        raw = path.read_bytes()
        digest = next(
            line.split(b": ")[1]
            for line in raw.splitlines()
            if line.startswith(b"SHA256:")
        )
        raw = raw.replace(digest, b"0" * 64)
        path.write_bytes(raw)
        path.with_name("Packages.gz").write_bytes(gzip.compress(raw))
        release = path.parents[2] / "Release"
        result = subprocess.check_output(
            ["apt-ftparchive", "release", "."], cwd=release.parent
        )
        release.write_bytes(result)
    else:
        root = repo / "public/rpm/stable/x86_64/repodata"
        path = next(root.glob("*-primary.xml.gz"))
        tree = ET.fromstring(gzip.decompress(path.read_bytes()))
        checksum = tree.find("{*}package/{*}checksum")
        assert checksum is not None
        checksum.text = "0" * 64
        raw = gzip.compress(ET.tostring(tree))
        path.write_bytes(raw)
        repomd = root / "repomd.xml"
        tree = ET.fromstring(repomd.read_bytes())
        for data in tree.findall("{*}data"):
            if data.get("type") == "primary":
                checksum = data.find("{*}checksum")
                size = data.find("{*}size")
                assert checksum is not None and size is not None
                checksum.text = hashlib.sha256(raw).hexdigest()
                size.text = str(len(raw))
        repomd.write_bytes(ET.tostring(tree))
    assert sign(repo, repo.parent / "gnupg", monkeypatch) == 0
    with pytest.raises(ValueError, match="match native lock"):
        validate_native(repo)


def test_conflicting_duplicate_lock_artifact_rejected(
    native_repo: Any, monkeypatch: Any
) -> None:
    repo = signed(native_repo, monkeypatch)
    path = repo / "native/packages.lock.json"
    lock = json.loads(path.read_bytes())
    lock.append({**lock[0], "name": "conflicting-name", "version": "9:99.0-1"})
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="duplicate native lock artifact"):
        validate_native(repo)
