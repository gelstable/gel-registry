"""Build disposable signed HTTP fixtures using the native test package factory.

Run: uv run python tests/clients/fixture.py OUTPUT BASE_URL
Serve OUTPUT on BASE_URL's host/port, then pass BASE_URL to apt.sh/dnf.sh.
OUTPUT must not exist. Private signing material never leaves the temporary tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from gel_registry.native import render_native
from gel_registry.native.sign import sign_native

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native.conftest import native_repo  # noqa: E402


def build(output: Path, base_url: str) -> None:
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo, cache, package = cast(Any, native_repo).__wrapped__(root)
        arch = "arm64" if os.uname().machine == "aarch64" else "amd64"
        for fmt in ("deb", "rpm"):
            package("gel-7", fmt, arch=arch)
        public = repo / "public"
        render_native(repo, cache, public, base_url + "/valid")
        # Exercise retained metadata composition before real clients install.
        for fmt in ("deb", "rpm"):
            package("gel-7", fmt, arch=arch, revision="2")
        render_native(repo, cache, public, base_url + "/valid")
        os.environ["GNUPGHOME"] = str(root / "gnupg")
        listing = subprocess.check_output(
            ["gpg", "--with-colons", "--list-secret-keys"], text=True
        )
        os.environ["GELSTABLE_SIGNING_FPR"] = list(
            line.split(":")[9]
            for line in listing.splitlines()
            if line.startswith("fpr:")
        )[-1]
        sign_native(repo)
        for item in json.loads((repo / "native/packages.lock.json").read_text()):
            path = public / item["format"].replace("deb", "apt") / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache / item["sha256"], path)
        for variant in ("valid", "tampered", "unsigned"):
            shutil.copytree(public, output / variant)
        # Force APT to fetch the corrupted uncompressed index.
        for path in (output / "tampered/apt").rglob("Packages"):
            with path.open("a") as stream:
                stream.write("\nPackage: corrupted\n")
            path.with_suffix(".gz").unlink()
        for path in (output / "tampered/rpm").rglob("*-primary.xml.gz"):
            with path.open("ab") as stream:
                stream.write(b"corrupted")
        for pattern in ("InRelease", "Release.gpg", "repomd.xml.asc"):
            for path in (output / "unsigned").rglob(pattern):
                path.unlink()
        # Separate snapshots prove a testing-only client upgrades to the final.
        for stage, version in (("prerelease", "7.2~rc.1"), ("final", "7.2")):
            for fmt in ("deb", "rpm"):
                package("gel-7", fmt, arch=arch, version=version)
            for pool in (public / "apt/pool", public / "rpm/pool"):
                if pool.exists():
                    shutil.rmtree(pool)
            render_native(repo, cache, public, base_url + "/" + stage)
            sign_native(repo)
            for item in json.loads((repo / "native/packages.lock.json").read_text()):
                path = public / item["format"].replace("deb", "apt") / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cache / item["sha256"], path)
            shutil.copytree(public, output / stage)
    print(f"Signed fixtures ready: {output}")


if __name__ == "__main__":
    build(Path(sys.argv[1]), sys.argv[2].rstrip("/"))
