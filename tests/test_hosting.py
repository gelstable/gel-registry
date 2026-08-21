"""The hosting boundary: a static tree served verbatim.

Nothing runs in production. Vercel uploads `public/` exactly as it was
committed, and the only policy it carries is how long each class of path may be
cached — pinned snapshot trees forever, the two moving documents briefly. CI
decides whether a change touches registry data at all by looking for the data
roots on disk, so that detector is part of the same boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
VERCEL_PATH = REPOSITORY_ROOT / "vercel.json"
DETECTOR = REPOSITORY_ROOT / ".github" / "scripts" / "detect-registry-data.sh"
PRODUCTION_HOSTNAME = "registry.gelstable.com"
STALE_HOSTNAME = "registry.gelstable.org"
HOSTING_PATHS = ("vercel.json", ".github", "docs")

MOVING_CACHE_CONTROL = (
    "public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400"
)
PINNED_CACHE_CONTROL = "public, max-age=31536000, s-maxage=31536000, immutable"


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
    for forbidden in (
        "functions",
        "buildCommand",
        "rewrites",
        "redirects",
        "routes",
        "builds",
    ):
        assert forbidden not in config


def test_pinned_trees_are_immutable_and_moving_documents_are_revalidated() -> None:
    """The blob store is pinned-forever too, from its own header rule."""

    assert _cache_rules(_vercel_config()) == {
        "/i/(.*)": PINNED_CACHE_CONTROL,
        "/s/(.*)": PINNED_CACHE_CONTROL,
        "/registry.json": MOVING_CACHE_CONTROL,
        "/v1/(.*)": MOVING_CACHE_CONTROL,
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
    ["upstream", "bootstrap", "releases", "pointers", "public"],
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
