"""A real unsigned artifact becomes a validated promotion without package access."""

from __future__ import annotations

import importlib.util
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from support import complete_repository, package

from gel_registry.publication import publish_registry
from gel_registry.validation import validate_local

from .test_sign_validate import sign

pytestmark = pytest.mark.native_tools


def test_unsigned_artifact_sign_and_publish_without_package_cache(
    native_repo: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    script = Path(__file__).parents[2] / ".github/scripts/promote.py"
    spec = importlib.util.spec_from_file_location("promotion_boundary", script)
    assert spec and spec.loader
    promote = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(promote)
    repo, cache, make_package = native_repo
    complete_repository(repo, {"packages": [package()]})
    monkeypatch.chdir(repo)
    run = cast(Callable[[list[str]], str], promote._run)
    run(["git", "init", "-b", "main"])
    run(["git", "add", "."])
    run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-m",
            "Base",
        ]
    )
    base = run(["git", "rev-parse", "HEAD"]).strip()
    checkout = tmp_path / "signer"
    run(["git", "clone", "--no-hardlinks", str(repo), str(checkout)])

    # Mock discovery and remote operations, while exercising real rendering,
    # archive transfer, signing, validation, and commit commands.
    def build_run(command: list[str]) -> str:
        if command[:2] == ["git", "ls-remote"]:
            return ""
        if command[:2] == ["git", "fetch"] or command[:2] == ["git", "switch"]:
            return ""
        if command[:2] == ["gel-registry", "build-candidate"]:
            make_package("gel-cli")
            make_package("gel-server-7", "rpm")
            publish_registry(repo)
            return '{"rejected": []}'
        return run(command)

    result = promote.build(run=build_run, cache=str(cache))
    assert result["base_oid"] == base
    assert result["native_changed"] is True
    assert not (repo / "public/apt/dists/stable/InRelease").exists()
    artifact = tmp_path / "artifact"
    promote.write_artifact(artifact, result)
    shutil.rmtree(cache)
    monkeypatch.chdir(checkout)
    run(["git", "update-ref", "refs/remotes/origin/main", base])
    promote.apply_artifact(artifact)
    assert not list(checkout.rglob("*.deb"))
    assert not list(checkout.rglob("*.rpm"))
    assert not validate_local(checkout).ok
    home = repo.parent / "gnupg"
    assert sign(checkout, home, monkeypatch) == 0
    shutil.rmtree(home)
    monkeypatch.delenv("GNUPGHOME")
    monkeypatch.delenv("GELSTABLE_SIGNING_FPR")
    external: list[list[str]] = []

    def publish_run(command: list[str]) -> str:
        if command[:2] == ["git", "push"] or command[0] == "gh":
            external.append(command)
            return "[]" if command[:3] == ["gh", "pr", "list"] else ""
        assert command[:3] != ["gel-registry", "native", "render"]
        assert "GNUPGHOME" not in os.environ
        return run(command)

    promote.publish(result, run=publish_run)
    assert run(["gel-registry", "validate", "--repo", "."]).strip() == "ok"
    assert not run(["git", "status", "--porcelain"])
    assert any(cmd[:3] == ["gh", "pr", "create"] for cmd in external)
    assert any(cmd[:2] == ["git", "push"] for cmd in external)
    assert "native/packages.lock.json" in run(
        ["git", "show", "--format=", "--name-only"]
    )

    unknown = checkout / "public/unexpected"
    unknown.write_text("unexpected")
    report = validate_local(checkout)
    assert any("public/unexpected" in error for error in report.errors)
    unknown.unlink()
    release = checkout / "public/apt/dists/stable/Release"
    release.write_bytes(release.read_bytes() + b"tampered")
    report = validate_local(checkout)
    assert any("native.integrity" in error for error in report.errors)
