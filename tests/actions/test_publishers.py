"""Shared publisher contracts use fixture releases, indexes and final bytes."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/actions/native-plan/plan.py"


def planner() -> Any:
    spec = importlib.util.spec_from_file_location("native_plan", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def asset(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "browser_download_url": "https://github.com/gelstable/gel/releases/download/v7.1/"
        + name,
        "digest": "sha256:" + "a" * 64,
        "size": 123,
    }


def server_source(tag: str = "v7.1") -> dict[str, Any]:
    return {
        "tag_name": tag,
        "draft": False,
        "assets": [
            asset(f"gel-server-{arch}-unknown-linux-gnu.tar.zst")
            for arch in ("x86_64", "aarch64")
        ],
    }


@pytest.mark.parametrize(
    "published,repack,want", [([], False, 1), ([1], False, None), ([1, 2], True, 3)]
)
def test_server_revision_plan(
    published: list[int], repack: bool, want: int | None
) -> None:
    releases = [{"tag_name": f"pkg-gel-7-7.1-{r}", "draft": False} for r in published]
    releases.append({"tag_name": "pkg-gel-7-7.1-99", "draft": True})
    plan = planner().build_plan("server", server_source(), releases, {}, repack)
    if want is None:
        assert plan == []
    else:
        assert len(plan) == 1
        assert plan[0]["revision"] == want
        assert plan[0]["tag"] == f"pkg-gel-7-7.1-{want}"
        assert set(plan[0]["sources"]) == {"x86_64", "aarch64"}


@pytest.mark.parametrize("repack,want", [(False, None), (True, 2)])
def test_combined_product_release_reserves_native_revision(
    repack: bool, want: int | None
) -> None:
    source = server_source()
    source["assets"].append(asset("gel-7_1%3A7.1-1_amd64.deb"))
    result = planner().build_plan("server", source, [source], {}, repack)
    assert (result[0]["revision"] if result else None) == want


def test_prerelease_version_plan() -> None:
    item = planner().build_plan("server", server_source("v7.2-rc.1"), [], {}, False)[0]
    assert item["native_version"] == "7.2~rc.1"
    assert item["slot"] == "7"


def test_legacy_postgis_builds_selected_from_real_portable_indexes() -> None:
    import json

    root = Path(__file__).parents[2] / "public"
    manifest = json.loads((root / "registry.json").read_text())
    indexes = {
        i["platform"].split("-", 1)[0]: json.loads((root / i["ref"]).read_text())[
            "packages"
        ]
        for i in manifest["indexes"]
        if i["channel"] == "stable" and i["platform"].endswith("unknown-linux-gnu")
    }
    assets = []
    for packages in indexes.values():
        for package in packages:
            if package["basename"] == "gel-server-6-ext-postgis":
                for ref in package["installrefs"]:
                    if ref["ref"].endswith(".zip"):
                        assets.append(asset(ref["ref"].rsplit("/", 1)[1]))
    source = {
        "tag_name": "legacy-gel-server-6-ext-postgis",
        "draft": False,
        "assets": assets,
    }
    # A later release in the index must not displace this release's builds.
    import copy

    for packages in indexes.values():
        newer = copy.deepcopy(
            next(p for p in packages if p["basename"] == "gel-server-6-ext-postgis")
        )
        newer["version_details"]["metadata"]["build_revision"] = "99999999999999"
        for ref in newer["installrefs"]:
            ref["ref"] = ref["ref"].replace("3.5.1", "3.5.2")
        packages.append(newer)
    item = planner().build_plan("postgis", source, [], indexes, False)[0]
    assert "c6766e7" in item["sources"]["x86_64"]["url"]
    assert "edaa7ce" in item["sources"]["aarch64"]["url"]
    assert set(item["server_sources"]) == {"x86_64", "aarch64"}


def test_manifest_writer_and_contract_checker(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    directory = tmp_path / "packages"
    directory.mkdir()
    (directory / "gel-cli-8.0-1-amd64.deb").write_bytes(b"final-package")
    script = Path(__file__).parents[2] / ".github/actions/native-manifest/manifest.py"
    output = subprocess.check_output(
        [sys.executable, str(script), str(directory), "gelstable/gel-cli", "v8.0"],
        text=True,
    )
    native = json.loads(output)
    assert native == {
        "packages": [
            {
                "url": "https://github.com/gelstable/gel-cli/releases/download/v8.0/gel-cli-8.0-1-amd64.deb",
                "sha256": (
                    "1127fbca3b48cd287aa8cbc7595ab9d5126325cb60e86c2a230475cf94d922dc"
                ),
                "size": 13,
            }
        ]
    }
    manifest = tmp_path / "gel-registry.json"
    manifest.write_text(json.dumps({"schema_version": 2, "native": native}))
    from gel_registry.__main__ import main

    assert main(["manifest", "check", str(manifest)]) == 0
    native["channel"] = "stable"
    manifest.write_text(json.dumps({"schema_version": 2, "native": native}))
    assert main(["manifest", "check", str(manifest)]) == 1


@pytest.mark.parametrize("outcome", ["success", "mismatch", "publish-response-lost"])
def test_publication_checks_uploaded_bytes_and_cleans_own_draft(
    tmp_path: Path, monkeypatch: Any, outcome: str
) -> None:
    import hashlib
    import json
    import subprocess

    script = (
        Path(__file__).parents[2] / ".github/actions/publish-native-release/publish.py"
    )
    spec = importlib.util.spec_from_file_location("publish_native", script)
    assert spec and spec.loader
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    directory = tmp_path / "packages"
    directory.mkdir()
    package = directory / "gel-cli-8.0-1-amd64.deb"
    package.write_bytes(b"fixture")
    calls = []

    def gh(*args: str) -> str:
        calls.append(args)
        if args[0] == "api" and "--paginate" in args:
            return "[[]]"
        if args[0] == "api":
            assets = [
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in directory.iterdir()
            ]
            if outcome == "mismatch":
                assets[0]["digest"] = "sha256:" + "0" * 64
            return json.dumps(
                {
                    "draft": not any(call[:2] == ("release", "edit") for call in calls),
                    "assets": assets,
                }
            )
        if args[:2] == ("release", "edit") and outcome == "publish-response-lost":
            raise subprocess.CalledProcessError(1, args)
        return ""

    real_check_output = subprocess.check_output

    def check_output(args: list[str], **kwargs: Any) -> Any:
        if args[0] == "dpkg-deb":
            return "1:8.0-1"
        return real_check_output(args, **kwargs)

    monkeypatch.setattr(publisher, "gh", gh)
    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    if outcome == "publish-response-lost":
        with pytest.raises(subprocess.CalledProcessError):
            publisher.publish(directory, "gelstable/gel-cli", "v8.0")
        assert not any(call[:2] == ("release", "delete") for call in calls)
    elif outcome == "mismatch":
        with pytest.raises(ValueError, match="digest"):
            publisher.publish(directory, "gelstable/gel-cli", "v8.0")
        assert any(call[:2] == ("release", "delete") for call in calls)
        assert not any(call[:2] == ("release", "edit") for call in calls)
    else:
        publisher.publish(directory, "gelstable/gel-cli", "v8.0")
        assert any(call[:2] == ("release", "edit") for call in calls)
        assert not any(call[:2] == ("release", "delete") for call in calls)


def test_publish_refuses_existing_release_before_writing_manifest(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import json

    script = (
        Path(__file__).parents[2] / ".github/actions/publish-native-release/publish.py"
    )
    spec = importlib.util.spec_from_file_location("publish_existing", script)
    assert spec and spec.loader
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    (tmp_path / "gel-cli-8.0-1-amd64.deb").write_bytes(b"fixture")
    monkeypatch.setattr(
        publisher,
        "gh",
        lambda *args: json.dumps([[{"tag_name": "v8.0", "draft": False}]]),
    )
    with pytest.raises(ValueError, match="already exists"):
        publisher.publish(tmp_path, "gelstable/gel-cli", "v8.0")
    assert not (tmp_path / "gel-registry.json").exists()
