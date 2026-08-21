"""Shared HTTP configuration for every GitHub request."""

from __future__ import annotations

import httpx

GITHUB_API = "https://api.github.com"

API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "gel-registry-promoter/1",
}
ASSET_HEADERS = {
    **API_HEADERS,
    "Accept": "application/octet-stream",
}
REQUEST_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=60.0,
    write=60.0,
    pool=60.0,
)
