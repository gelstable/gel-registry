"""Contract tests for static hosting and registry operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).parents[1]
VERCEL_PATH = REPOSITORY_ROOT / "vercel.json"

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
