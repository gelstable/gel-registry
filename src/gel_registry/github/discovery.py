"""Discovery operations for GitHub repository releases and manifest assets."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

import httpx

from .models import DiscoveredAsset, DiscoveredRelease


def _release_endpoint(repository: str) -> str:
    return f"https://api.github.com/repos/{repository}/releases"


def _parse_release(repository: str, data: object) -> DiscoveredRelease:
    if not isinstance(data, dict):
        raise ValueError("GitHub release response contains a non-object release")
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise ValueError("GitHub release response has invalid assets")
    try:
        release_id = data["id"]
        tag = data["tag_name"]
        published_at = data["published_at"]
        draft = data["draft"]
    except KeyError as exc:
        raise ValueError(f"GitHub release response is missing {exc.args[0]}") from exc
    if (
        isinstance(release_id, bool)
        or not isinstance(release_id, int)
        or not isinstance(tag, str)
        or not isinstance(published_at, str)
        or not isinstance(draft, bool)
    ):
        raise ValueError("GitHub release response has invalid release fields")
    try:
        timestamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("GitHub release response has invalid published_at") from exc
    parsed_assets: list[DiscoveredAsset] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("GitHub release response contains a non-object asset")
        name = asset.get("name")
        api_url = asset.get("url")
        if not isinstance(name, str) or not isinstance(api_url, str):
            raise ValueError("GitHub release response has invalid asset fields")
        parsed_assets.append(DiscoveredAsset(name=name, api_url=api_url))
    return DiscoveredRelease(
        repository=repository,
        release_id=release_id,
        tag=tag,
        published_at=timestamp,
        draft=draft,
        assets=tuple(parsed_assets),
    )


def _valid_next_url(url: str, repository: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "api.github.com"
        and parsed.path == f"/repos/{repository}/releases"
    )


def list_releases(
    client: httpx.Client, repository: str
) -> tuple[DiscoveredRelease, ...]:
    """Return every GitHub release for one exact allowlisted repository."""
    url = f"{_release_endpoint(repository)}?per_page=100"
    releases: list[DiscoveredRelease] = []
    while True:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("GitHub releases response is not a list")
        releases.extend(_parse_release(repository, item) for item in payload)
        next_link = response.links.get("next")
        if next_link is None:
            break
        next_url = next_link.get("url")
        if not isinstance(next_url, str) or not _valid_next_url(next_url, repository):
            raise ValueError(
                "GitHub next release page is outside the repository endpoint"
            )
        url = next_url
    return tuple(sorted(releases, key=lambda release: release.release_id))


def fetch_manifest_asset(client: httpx.Client, release: DiscoveredRelease) -> bytes:
    """Fetch the unique gel-registry.json asset from one release."""
    assets = [asset for asset in release.assets if asset.name == "gel-registry.json"]
    if len(assets) != 1:
        raise ValueError("release does not have one gel-registry.json asset")
    response = client.get(
        assets[0].api_url,
        headers={"Accept": "application/octet-stream"},
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


__all__ = ["fetch_manifest_asset", "list_releases"]
