"""The custody bundle is streamed to exactly the intended Environment secrets."""

import json
import os
import subprocess
from pathlib import Path


def test_signing_secrets_streams_key_without_printing_it(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "automation-subkey.asc").write_text("PRIVATE-FIXTURE-MATERIAL")
    (bundle / "fingerprints.json").write_text(json.dumps({"signing": "A" * 40}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\ncat >> "$INPUTS"\n')
    gh.chmod(0o755)
    calls = tmp_path / "calls"
    inputs = tmp_path / "inputs"
    result = subprocess.run(
        ["bash", "scripts/set-signing-secrets.sh", str(bundle)],
        input="yes\n",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CALLS": str(calls),
            "INPUTS": str(inputs),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "PRIVATE-FIXTURE-MATERIAL" not in result.stdout + result.stderr
    commands = calls.read_text().splitlines()
    assert len(commands) == 8
    for repo, environment, prefix in [
        ("gel-registry", "registry-signing", "REGISTRY"),
        ("gel", "package-signing", "PACKAGE"),
        ("gel-cli", "package-signing", "PACKAGE"),
        ("gel-postgis", "package-signing", "PACKAGE"),
    ]:
        assert (
            f"secret set {prefix}_SIGNING_KEY --repo gelstable/{repo} "
            f"--env {environment}" in commands
        )
        assert (
            f"secret set {prefix}_SIGNING_FPR --repo gelstable/{repo} "
            f"--env {environment}" in commands
        )
    assert inputs.read_text().count("PRIVATE-FIXTURE-MATERIAL") == 4
