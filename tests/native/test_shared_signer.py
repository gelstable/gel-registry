"""The shared action signs final RPMs with the selected disposable subkey."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gel_registry.native.inspect import inspect_package

pytestmark = pytest.mark.native_tools


def test_shared_signer_uses_caller_subkey(native_repo: Any, tmp_path: Path) -> None:
    repo, cache, package = native_repo
    record = package(fmt="rpm", signed=False)
    digest = record["native"]["packages"][0]["sha256"]
    directory = tmp_path / "packages"
    directory.mkdir()
    rpm = directory / "gel-server-7.rpm"
    shutil.copyfile(cache / digest, rpm)
    env = {**os.environ, "GNUPGHOME": str(tmp_path / "gnupg")}
    listing = subprocess.check_output(
        ["gpg", "--with-colons", "--list-secret-keys"], env=env, text=True
    )
    fingerprint = [
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    ][-1]
    secret = subprocess.check_output(
        ["gpg", "--armor", "--export-secret-subkeys", fingerprint + "!"],
        env=env,
        text=True,
    )
    script = Path(__file__).parents[2] / ".github/actions/sign-rpms/sign-rpms.sh"
    subprocess.run(
        ["bash", str(script), str(directory)],
        env={
            **os.environ,
            "PACKAGE_SIGNING_KEY": secret,
            "PACKAGE_SIGNING_FPR": fingerprint,
        },
        check=True,
        capture_output=True,
    )
    identity = inspect_package(
        rpm, "rpm", ["gel-server-7"], repo / "public/keys/gelstable.asc"
    )
    assert identity.version == "1:7.1-1"
    writer = script.parent.parent / "native-manifest/manifest.py"
    result = subprocess.check_output(
        ["python3", str(writer), str(directory), "gelstable/gel", "pkg-gel-7-7.1-1"],
        text=True,
    )
    native = json.loads(result)
    assert native["packages"][0]["size"] == rpm.stat().st_size
    assert native["packages"][0]["sha256"] != digest
