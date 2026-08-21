"""The hosting boundary: a static tree served verbatim.

Nothing runs in production. Vercel uploads `public/` exactly as it was
committed, and the policy it carries is how long each class of path may be
cached — pinned snapshot trees forever, the moving documents briefly — plus the
rewrites that let a legacy `GEL_PKG_ROOT` client address the selected snapshot
through the paths it already knows. A rewrite resolves to a file that is in the
same uploaded tree, so it adds a name for existing bytes rather than a code
path. Because those rewrites name a snapshot, `vercel.json` is rendered by the
publication transaction and checked here against the selected snapshot rather
than hand maintained. CI decides whether a change touches registry data at all
by looking for the data roots on disk, so that detector is part of the same
boundary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gel_registry.contracts import RootManifest
from gel_registry.render.hosting import (
    MOVING_CACHE_CONTROL,
    PINNED_CACHE_CONTROL,
    hosting_config,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
VERCEL_PATH = REPOSITORY_ROOT / "vercel.json"
DETECTOR = REPOSITORY_ROOT / ".github" / "scripts" / "detect-registry-data.sh"
PRODUCTION_HOSTNAME = "registry.gelstable.com"
STALE_HOSTNAME = "registry.gelstable.org"
HOSTING_PATHS = ("vercel.json", ".github", "docs")

SNAPSHOT_LISTING = REPOSITORY_ROOT / "public" / "v1" / "snapshots.json"
MOVING_ROOT = REPOSITORY_ROOT / "public" / "registry.json"
BLOB_DESTINATION = re.compile(r"^/i/[0-9a-f]{32}\.json$")


def _vercel_config() -> dict[str, Any]:
    if not VERCEL_PATH.is_file():
        pytest.skip("committed hosting configuration is not present yet")
    value = json.loads(VERCEL_PATH.read_bytes())
    assert isinstance(value, dict)
    return value


def _cache_rules(config: dict[str, Any]) -> dict[str, str]:
    """Flatten the header rules, asserting each declares exactly one policy."""

    rules = config["headers"]
    assert isinstance(rules, list)
    result: dict[str, str] = {}
    for rule in rules:
        headers = rule["headers"]
        assert len(headers) == 1
        assert headers[0]["key"] == "Cache-Control"
        result[rule["source"]] = headers[0]["value"]
    return result


def test_vercel_serves_the_public_tree_verbatim_with_no_build_step() -> None:
    config = _vercel_config()

    assert config["outputDirectory"] == "public"
    assert config.get("framework") is None
    for forbidden in ("functions", "buildCommand", "redirects", "routes", "builds"):
        assert forbidden not in config


def test_pinned_trees_are_immutable_and_moving_documents_are_revalidated() -> None:
    """The blob store is pinned-forever too, from its own header rule."""

    assert _cache_rules(_vercel_config()) == {
        "/i/(.*)": PINNED_CACHE_CONTROL,
        "/s/(.*)": PINNED_CACHE_CONTROL,
        "/registry.json": MOVING_CACHE_CONTROL,
        "/v1/(.*)": MOVING_CACHE_CONTROL,
    }


def _moving_manifest() -> RootManifest:
    if not MOVING_ROOT.is_file():
        pytest.skip("no snapshot has been selected yet")
    return RootManifest.model_validate_json(MOVING_ROOT.read_bytes())


def test_legacy_index_rewrites_address_the_selected_snapshot() -> None:
    """The committed configuration matches a fresh render of the selection."""

    assert VERCEL_PATH.read_bytes() == hosting_config(_moving_manifest())


def test_legacy_rewrites_enumerate_exactly_the_published_indexes() -> None:
    """One literal rule per published index, with no pattern to disambiguate."""

    manifest = _moving_manifest()
    rewrites = _vercel_config()["rewrites"]
    suffixes = {"stable": "", "nightly": ".nightly", "testing": ".testing"}

    assert [rule["source"] for rule in rewrites] == [
        f"/archive/.jsonindexes/{item.platform}{suffixes[item.channel]}.json"
        for item in manifest.indexes
    ]
    # Literal sources: nothing can read "...darwin.nightly" as a platform name,
    # so no rule depends on being emitted before or after another.
    assert all(":" not in rule["source"] for rule in rewrites)
    # A (channel, platform) pair without an index gets no rule and 404s, rather
    # than a rule pointing at a blob that was never published.
    assert len(rewrites) == len(manifest.indexes)
    assert len({rule["source"] for rule in rewrites}) == len(rewrites)


def test_rewrite_destinations_exist_in_the_published_tree() -> None:
    manifest = _moving_manifest()
    rewrites = _vercel_config()["rewrites"]
    assert rewrites, "selected snapshot publishes no indexes"

    for rule in rewrites:
        destination = rule["destination"]
        assert BLOB_DESTINATION.fullmatch(destination), destination
        blob = REPOSITORY_ROOT / "public" / destination.lstrip("/")
        assert blob.is_file(), destination
    # Every rewrite lands on a blob the moving root also names, so the legacy
    # path and the manifest path resolve to the same bytes.
    assert {rule["destination"] for rule in rewrites} == {
        f"/{item.ref}" for item in manifest.indexes
    }


def test_tracked_hosting_files_name_only_the_production_hostname() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *HOSTING_PATHS],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    )
    paths = [
        REPOSITORY_ROOT / os.fsdecode(entry)
        for entry in result.stdout.split(b"\0")
        if entry
    ]
    assert DETECTOR in paths
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert PRODUCTION_HOSTNAME in text
    assert STALE_HOSTNAME not in text


@pytest.mark.parametrize(
    "root",
    ["upstream", "bootstrap", "releases", "promotion", "pointers", "public", "sources"],
)
def test_the_data_detector_reports_every_registry_root(
    tmp_path: Path, root: str
) -> None:
    output = tmp_path / "output"
    environment = {**os.environ, "GITHUB_OUTPUT": str(output)}

    def detect() -> str:
        output.write_text("")
        subprocess.run(
            ["bash", str(DETECTOR)], cwd=tmp_path, env=environment, check=True
        )
        return output.read_text()

    assert detect() == "present=false\n"
    (tmp_path / root).mkdir()
    assert detect() == "present=true\n"
