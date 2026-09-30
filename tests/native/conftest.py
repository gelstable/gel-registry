"""Real native package fixtures, built with nFPM and disposable signing keys."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from gel_registry.digest import canonical_json

TOOLS = (
    "nfpm",
    "dpkg-deb",
    "apt-ftparchive",
    "createrepo_c",
    "rpm",
    "rpmkeys",
    "rpmsign",
    "gpg",
)


@pytest.fixture
def native_repo(tmp_path: Path) -> tuple[Path, Path, Callable[..., dict[str, object]]]:
    missing = [tool for tool in TOOLS if not shutil.which(tool)]
    if missing:
        if os.environ.get("REQUIRE_NATIVE_TOOLS"):
            pytest.fail(f"missing native tools: {missing}")
        pytest.skip(f"missing native tools: {missing}")
    repo = tmp_path / "repo"
    cache = tmp_path / "cache"
    cache.mkdir()
    (repo / "sources").mkdir(parents=True)
    (repo / "releases" / "gelstable" / "gel").mkdir(parents=True)
    (repo / "sources" / "github.json").write_bytes(
        canonical_json(
            {
                "schema_version": 2,
                "repositories": ["gelstable/gel", "gelstable/gel-cli"],
                "native_package_names": {
                    "gelstable/gel": ["^gel-(server-)?[0-9]+$"],
                    "gelstable/gel-cli": ["^gel-cli$"],
                },
            }
        )
    )
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Fixture <fixture@example.com>",
            "rsa2048",
            "sign",
            "0",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    key = subprocess.check_output(
        ["gpg", "--armor", "--export", "fixture@example.com"], env=env
    )
    (repo / "public" / "keys").mkdir(parents=True)
    (repo / "public" / "keys" / "gelstable.asc").write_bytes(key)
    count = 0

    def package(
        name: str = "gel-server-7",
        fmt: str = "deb",
        arch: str = "amd64",
        signed: bool = True,
        channel: str = "stable",
        payload: str = "hello",
        revision: str = "1",
        signer: str = "fixture@example.com",
    ) -> dict[str, object]:
        nonlocal count
        count += 1
        content = tmp_path / f"content-{count}"
        content.write_text(payload)
        config = tmp_path / f"nfpm-{count}.json"
        config.write_text(
            json.dumps(
                {
                    "name": name,
                    "arch": arch,
                    "platform": "linux",
                    "version_schema": "none",
                    "epoch": "1",
                    "version": "7.1",
                    "release": revision,
                    "maintainer": "Fixture <fixture@example.com>",
                    "description": "Native test package",
                    "contents": [
                        {"src": str(content), "dst": f"/usr/share/{name}/fixture"}
                    ],
                }
            )
        )
        asset = f"{name}-{count}.{fmt}"
        path = tmp_path / asset
        subprocess.run(
            [
                "nfpm",
                "package",
                "--config",
                str(config),
                "--packager",
                fmt,
                "--target",
                str(path),
            ],
            check=True,
            capture_output=True,
        )
        if fmt == "rpm" and signed:
            subprocess.run(
                [
                    "rpmsign",
                    "--define",
                    "_gpg_name " + signer,
                    "--define",
                    "_gpg_path " + str(home),
                    "--define",
                    "__gpg /usr/bin/gpg",
                    "--addsign",
                    str(path),
                ],
                env=env,
                check=True,
                capture_output=True,
            )
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        (cache / digest).write_bytes(raw)
        repository = "gelstable/gel-cli" if name == "gel-cli" else "gelstable/gel"
        record = {
            "schema_version": 2,
            "replacements": [],
            "indexes": [],
            "native": {
                "channel": channel,
                "packages": [
                    {
                        "url": f"https://github.com/{repository}/releases/download/pkg-{count}/{asset}",
                        "sha256": digest,
                        "size": len(raw),
                    }
                ],
            },
            "source": {
                "repository": repository,
                "release_id": count,
                "tag": f"pkg-{count}",
                "published_at": "2026-09-30T00:00:00Z",
            },
        }
        directory = repo / "releases" / repository
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{count}.json").write_bytes(canonical_json(record))
        return record

    return repo, cache, package
