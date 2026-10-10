"""Publish one native-only release after checking final bytes and API digests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def publish(directory: Path, repository: str, tag: str) -> None:
    actions = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "native_manifest", actions / "native-manifest/manifest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native = module.native_manifest(directory, repository, tag)
    pages = json.loads(
        gh("api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100")
    )
    if any(release["tag_name"] == tag for page in pages for release in page):
        raise ValueError("release tag already exists; refusing to replace it")
    manifest = directory / "gel-registry.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": 2, "replacements": [], "indexes": [], "native": native},
            indent=2,
        )
        + "\n"
    )
    subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(actions.parents[1]),
            "gel-registry",
            "manifest",
            "check",
            str(manifest.resolve()),
        ],
        check=True,
    )
    # Package-manager versions, rather than filenames, decide prerelease status.
    prerelease = False
    for package in native["packages"]:
        path = directory / package["url"].rsplit("/", 1)[1]
        args = (
            ["dpkg-deb", "--field", str(path), "Version"]
            if path.suffix == ".deb"
            else ["rpm", "-qp", "--queryformat", "%{VERSION}", str(path)]
        )
        version = subprocess.check_output(args, text=True).strip()
        prerelease |= (
            "~" in version.rsplit("-", 1)[0]
            if path.suffix == ".deb"
            else "~" in version
        )
    flags = ["--prerelease"] if prerelease else []
    created = False
    try:
        gh(
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--draft",
            "--title",
            tag,
            "--target",
            os.environ["GITHUB_SHA"],
            *flags,
        )
        created = True
        paths = [directory / p["url"].rsplit("/", 1)[1] for p in native["packages"]] + [
            manifest
        ]
        gh(
            "release",
            "upload",
            tag,
            "--repo",
            repository,
            *(str(path) for path in paths),
        )
        release = json.loads(
            gh("api", f"repos/{repository}/releases/tags/{quote(tag, safe='')}")
        )
        assets = {asset["name"]: asset for asset in release["assets"]}
        if set(assets) != {path.name for path in paths}:
            raise ValueError("uploaded asset inventory differs")
        for path in paths:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if (assets[path.name].get("digest"), assets[path.name]["size"]) != (
                "sha256:" + digest,
                path.stat().st_size,
            ):
                raise ValueError(f"uploaded digest or size differs: {path.name}")
        gh("release", "edit", tag, "--repo", repository, "--draft=false")
    except BaseException:
        if created:
            # A lost publication response may mean the release is already live.
            # Never delete published assets during draft cleanup.
            current = json.loads(
                gh("api", f"repos/{repository}/releases/tags/{quote(tag, safe='')}")
            )
            if current.get("draft") is True:
                gh("release", "delete", tag, "--repo", repository, "--yes")
        raise


if __name__ == "__main__":
    publish(
        Path(os.environ["INPUT_DIRECTORY"]).resolve(),
        os.environ["GITHUB_REPOSITORY"],
        os.environ["INPUT_TAG"],
    )
