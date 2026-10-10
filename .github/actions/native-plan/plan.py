"""Resolve verified portable sources and the next unused native revision."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen

ARCHES = ("x86_64", "aarch64")


def asset_source(asset: dict[str, Any]) -> dict[str, Any]:
    digest = asset.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"source asset has no API SHA-256: {asset['name']}")
    size = asset.get("size")
    if type(size) is not int or size <= 0:
        raise ValueError("source asset has invalid size")
    return {"url": asset["browser_download_url"], "sha256": digest[7:], "size": size}


def build_revision(package: dict[str, Any]) -> str:
    return str(
        package.get("version_details", {})
        .get("metadata", {})
        .get("build_revision", package.get("revision", ""))
    )


def index_source(
    packages: list[dict[str, Any]], basename: str, suffix: str
) -> dict[str, Any]:
    candidates = [
        (build_revision(p), ref)
        for p in packages
        if p["basename"] == basename
        or (
            p["basename"] == "gel-server"
            and basename == "gel-server-" + str(p.get("slot"))
        )
        for ref in p["installrefs"]
        if ref["ref"].endswith(suffix)
    ]
    if not candidates:
        raise ValueError(f"no portable index source for {basename}")
    _, ref = max(candidates, key=lambda pair: pair[0])
    verification = ref["verification"]
    if (
        not re.fullmatch(r"[0-9a-f]{64}", verification["sha256"])
        or type(verification["size"]) is not int
        or verification["size"] <= 0
    ):
        raise ValueError("invalid portable index verification")
    return {
        "url": ref["ref"],
        "sha256": verification["sha256"],
        "size": verification["size"],
    }


def build_plan(
    product: str,
    source: dict[str, Any],
    releases: list[dict[str, Any]],
    indexes: dict[str, list[dict[str, Any]]],
    repack: bool,
) -> list[dict[str, Any]]:
    if source.get("draft"):
        raise ValueError("source release is a draft")
    sources = {}
    versions = set()
    slots = set()
    for arch in ARCHES:
        if product == "server":
            candidates = [
                a
                for a in source["assets"]
                if a["name"] == f"gel-server-{arch}-unknown-linux-gnu.tar.zst"
            ]
            match = re.fullmatch(
                r"v?([0-9]+(?:\.[0-9]+)*(?:-[A-Za-z0-9.]+)?)", source["tag_name"]
            )
            if not match:
                raise ValueError("server source tag has no product version")
            version = match[1]
            slot = version.split(".")[0].split("-")[0]
        elif product == "postgis":
            pattern = (
                r"gel-server-([0-9]+)-ext-postgis-"
                r"([0-9.]+(?:-[A-Za-z0-9.]+)?)(?:\+[A-Za-z0-9]+)?-"
                rf"{arch}-unknown-linux-gnu\.zip"
            )
            candidates = [
                a for a in source["assets"] if re.fullmatch(pattern, a["name"])
            ]
            if not candidates:
                raise ValueError(f"no PostGIS source for {arch}")
            if len(candidates) > 1:
                slot_match = re.fullmatch(pattern, candidates[0]["name"])
                assert slot_match
                chosen = index_source(
                    indexes[arch], f"gel-server-{slot_match[1]}-ext-postgis", ".zip"
                )
                candidates = [
                    a
                    for a in candidates
                    if a["name"] == chosen["url"].rsplit("/", 1)[1]
                ]
            if len(candidates) != 1:
                raise ValueError(
                    "portable index does not resolve a unique source asset"
                )
            match = re.fullmatch(pattern, candidates[0]["name"])
            assert match
            slot, version = match[1], match[2]
        else:
            raise ValueError("unsupported native product")
        if len(candidates) != 1:
            raise ValueError(f"source release must have one glibc asset for {arch}")
        sources[arch] = asset_source(candidates[0])
        versions.add(version)
        slots.add(slot)
    if len(versions) != 1 or len(slots) != 1:
        raise ValueError("source architectures disagree on version or slot")
    version, slot = versions.pop(), slots.pop()
    native_version = version.replace("-", "~", 1)
    prefix = (
        f"pkg-gel-{slot}-{version}-"
        if product == "server"
        else f"pkg-gel-server-{slot}-ext-postgis-{version}-"
    )
    revisions = [
        int(match[1])
        for release in releases
        if not release.get("draft")
        if (
            match := re.fullmatch(
                re.escape(prefix) + r"([1-9][0-9]*)", release["tag_name"]
            )
        )
    ]
    if revisions and not repack:
        return []
    revision = max(revisions, default=0) + 1
    item = {
        "slot": slot,
        "version": version,
        "native_version": native_version,
        "revision": revision,
        "tag": prefix + str(revision),
        "sources": sources,
    }
    if product == "postgis":
        item["server_sources"] = {
            arch: index_source(indexes[arch], f"gel-server-{slot}", ".tar.zst")
            for arch in ARCHES
        }
    return [item]


def read_url(url: str) -> Any:
    with urlopen(url, timeout=60) as response:
        return json.load(response)


def main() -> None:
    product = os.environ["INPUT_PRODUCT"]
    repository = "gelstable/gel" if product == "server" else "gelstable/gel-postgis"
    tag = os.environ["INPUT_SOURCE_TAG"]
    source = json.loads(
        subprocess.check_output(
            ["gh", "api", f"repos/{repository}/releases/tags/{quote(tag, safe='')}"],
            text=True,
        )
    )
    pages = json.loads(
        subprocess.check_output(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/releases?per_page=100",
            ],
            text=True,
        )
    )
    releases = [release for page in pages for release in page]
    indexes = {}
    if product == "postgis":
        base = os.environ["INPUT_REGISTRY_URL"].rstrip("/")
        manifest = read_url(base + "/registry.json")
        for index in manifest["indexes"]:
            if index["channel"] == "stable" and index["platform"] in {
                f"{arch}-unknown-linux-gnu" for arch in ARCHES
            }:
                indexes[index["platform"].split("-", 1)[0]] = read_url(
                    base + "/" + index["ref"]
                )["packages"]
    plan = build_plan(
        product, source, releases, indexes, os.environ["INPUT_REPACK"].lower() == "true"
    )
    encoded = json.dumps(plan, separators=(",", ":"))
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("plan=" + encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()
