"""Contract tests for static hosting and registry operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).parents[1]
VERCEL_PATH = REPOSITORY_ROOT / "vercel.json"
OPERATIONS_PATH = REPOSITORY_ROOT / "docs" / "operations.md"
OBSERVATIONS_PATH = REPOSITORY_ROOT / "docs" / "hosting-observations.md"

MOVING_CACHE_CONTROL = (
    "public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400"
)
PINNED_CACHE_CONTROL = "public, max-age=31536000, s-maxage=31536000, immutable"


def _vercel_config() -> dict[str, Any]:
    with VERCEL_PATH.open(encoding="utf-8") as stream:
        value = json.load(stream)
    assert isinstance(value, dict)
    return value


def _cache_rules(config: dict[str, Any]) -> dict[str, str]:
    rules = config.get("headers")
    assert isinstance(rules, list)
    result: dict[str, str] = {}
    for rule in rules:
        assert isinstance(rule, dict)
        source = rule.get("source")
        headers = rule.get("headers")
        assert isinstance(source, str)
        assert isinstance(headers, list)
        assert len(headers) == 1
        header = headers[0]
        assert isinstance(header, dict)
        assert header.get("key") == "Cache-Control"
        value = header.get("value")
        assert isinstance(value, str)
        result[source] = value
    return result


def test_vercel_is_a_verbatim_public_static_deployment() -> None:
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


def test_vercel_declares_exact_moving_and_pinned_cache_policies() -> None:
    config = _vercel_config()
    assert _cache_rules(config) == {
        "/s/(.*)": PINNED_CACHE_CONTROL,
        "/registry.json": MOVING_CACHE_CONTROL,
        "/v1/(.*)": MOVING_CACHE_CONTROL,
    }


def test_operations_runbook_covers_validation_protection_and_static_operations() -> (
    None
):
    text = OPERATIONS_PATH.read_text(encoding="utf-8")
    normalized = text.lower()

    required_fragments = (
        "Registry validation / local",
        "uv run pytest -q",
        "uv run mypy src tests",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "disable force pushes",
        "Actions",
        "Vercel",
        "Promote stable Gel CLI releases",
        "rollback changes the pointer",
        "public/registry.json",
        "public/v1/snapshots.json",
        "does not rewrite pinned snapshots",
        "registry-host outage",
        "artifact-host outage",
        "stale-if-error",
        "no functions",
        "no rewrites",
        "no redirects",
    )
    for fragment in required_fragments:
        assert fragment.lower() in normalized, (
            f"operations runbook is missing {fragment!r}"
        )


def test_hosting_observations_define_repeatable_header_observations() -> None:
    text = OBSERVATIONS_PATH.read_text(encoding="utf-8")

    for command in (
        "curl -I https://registry.gelstable.org/registry.json",
        "curl -I https://registry.gelstable.org/v1/snapshots.json",
        "curl -I https://registry.gelstable.org/s/<SNAPSHOT_ID>/registry.json",
        "curl -I https://registry.gelstable.org/s/<SNAPSHOT_ID>/index/<INDEX>.json",
    ):
        assert command in text
    for field in ("URL", "status", "Cache-Control", "Age", "x-vercel-cache"):
        assert field in text
    assert "requested policy, not an availability guarantee" in text
