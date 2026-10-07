"""The hosting boundary: a static tree served verbatim.

Nothing runs in production. Vercel uploads `public/` exactly as it was
committed, and the policy it carries is how long each class of path may be
cached — pinned snapshot trees forever, the moving documents briefly — plus the
rewrites that let a legacy `GEL_PKG_ROOT` client address the selected snapshot
through the paths it already knows. A rewrite resolves to a file that is in the
same uploaded tree, so it adds a name for existing bytes rather than a code
path. Because those rewrites name a snapshot, `vercel.toml` is rendered by the
publication transaction and checked here against the selected snapshot rather
than hand maintained.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from gel_registry.contracts import RootManifest
from gel_registry.gather import load_repositories
from gel_registry.render.hosting import (
    MOVING_CACHE_CONTROL,
    PINNED_CACHE_CONTROL,
    hosting_config,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
VERCEL_PATH = REPOSITORY_ROOT / "vercel.toml"
PRODUCTION_HOSTNAME = "registry.gelstable.com"
STALE_HOSTNAME = "registry.gelstable.org"
HOSTING_PATHS = ("vercel.toml", ".github", "docs")

SNAPSHOT_LISTING = REPOSITORY_ROOT / "public" / "v1" / "snapshots.json"
MOVING_ROOT = REPOSITORY_ROOT / "public" / "registry.json"
BLOB_DESTINATION = re.compile(r"^/i/[0-9a-f]{32}\.json$")


def _vercel_config() -> dict[str, Any]:
    if not VERCEL_PATH.is_file():
        pytest.skip("committed hosting configuration is not present yet")
    value = tomllib.loads(VERCEL_PATH.read_text(encoding="utf-8"))
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
    for forbidden in ("functions", "buildCommand", "routes", "builds"):
        assert forbidden not in config


def test_pinned_trees_are_immutable_and_moving_documents_are_revalidated() -> None:
    """The blob store is pinned-forever too, from its own header rule."""

    assert _cache_rules(_vercel_config()) == {
        "/i/(.*)": PINNED_CACHE_CONTROL,
        "/s/(.*)": PINNED_CACHE_CONTROL,
        "/registry.json": MOVING_CACHE_CONTROL,
        "/v1/(.*)": MOVING_CACHE_CONTROL,
        "/apt/dists/(.*)": "public, max-age=0, s-maxage=60",
        "/rpm/(.*)": "public, max-age=0, s-maxage=60",
        "/keys/(.*)": "public, max-age=0, s-maxage=60",
        "/apt/pool/(.*)": "public, max-age=300",
        "/rpm/pool/(.*)": "public, max-age=300",
    }


def _moving_manifest() -> RootManifest:
    if not MOVING_ROOT.is_file():
        pytest.skip("no snapshot has been selected yet")
    return RootManifest.model_validate_json(MOVING_ROOT.read_bytes())


def test_legacy_index_rewrites_address_the_selected_snapshot() -> None:
    """The committed configuration matches a fresh render of the selection."""

    assert VERCEL_PATH.read_bytes() == hosting_config(
        _moving_manifest(), load_repositories(REPOSITORY_ROOT)
    )


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
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert PRODUCTION_HOSTNAME in text
    assert STALE_HOSTNAME not in text


def test_pool_redirect_is_the_single_allowlisted_external_route() -> None:
    repositories = load_repositories(REPOSITORY_ROOT)
    assert _vercel_config()["redirects"] == [
        {
            "source": "/:format(apt|rpm)/pool/:repo("
            + "|".join(repo.split("/")[1] for repo in repositories)
            + ")/:tag/:asset",
            "destination": "https://github.com/gelstable/:repo/releases/download/:tag/:asset",
            "permanent": False,
        }
    ]
    custom = tomllib.loads(
        hosting_config(_moving_manifest(), ("gelstable/gel-cli",)).decode()
    )
    assert (
        custom["redirects"][0]["source"]
        == "/:format(apt|rpm)/pool/:repo(gel-cli)/:tag/:asset"
    )


@pytest.mark.native_tools
def test_configured_signed_tree_validates_and_rejects_static_drift(
    tmp_path: Path,
) -> None:
    import shutil

    from gel_registry.validation import validate_local

    for name in (
        "upstream",
        "bootstrap",
        "releases",
        "pointers",
        "sources",
        "public",
        "native",
    ):
        source = REPOSITORY_ROOT / name
        if source.exists():
            shutil.copytree(source, tmp_path / name)
    shutil.copy2(VERCEL_PATH, tmp_path / "vercel.toml")
    report = validate_local(tmp_path)
    assert report.ok, report.errors
    assert "native.integrity" in report.checks
    for relative in (
        "gelstable.sources",
        "gelstable.repo",
        "gelstable-testing.sources",
        "gelstable-testing.repo",
        "keys/gelstable.fingerprint",
        "unknown-file",
    ):
        path = tmp_path / "public" / relative
        previous = path.read_bytes() if path.exists() else None
        path.write_bytes(b"unexpected\n")
        report = validate_local(tmp_path)
        assert not report.ok
        assert any(f"public/{relative}" in error for error in report.errors)
        if previous is None:
            path.unlink()
        else:
            path.write_bytes(previous)


def test_dotted_pool_repository_is_literal() -> None:
    custom = tomllib.loads(
        hosting_config(_moving_manifest(), ("gelstable/gel.extra",)).decode()
    )
    rule = custom["redirects"][0]
    assert rule["source"] == r"/:format(apt|rpm)/pool/:repo(gel\.extra)/:tag/:asset"
    assert (
        rule["destination"]
        == "https://github.com/gelstable/:repo/releases/download/:tag/:asset"
    )
    assert rule["permanent"] is False


@pytest.mark.parametrize(
    "repository",
    [
        "other/gel.extra",
        "gelstable/..",
        "gelstable/gel/extra",
        "gelstable/gel%2eextra",
        "gelstable/gel|extra",
        "gelstable/gel\\extra",
    ],
)
def test_unsafe_pool_repository_is_rejected(repository: str) -> None:
    from gel_registry.render.errors import RenderError

    with pytest.raises(RenderError, match="unsupported pool repository"):
        hosting_config(_moving_manifest(), (repository,))
