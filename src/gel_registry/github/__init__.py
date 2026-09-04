"""Narrowly scoped, extensible GitHub release transport and discovery."""

from .discovery import fetch_manifest_asset, list_releases
from .models import DiscoveredAsset, DiscoveredRelease
from .transport import (
    GITHUB_API,
    GITHUB_HOSTS,
    GITHUB_UPLOADS,
    GitHubTokenAuth,
    create_github_client,
    resolve_github_token,
)

__all__ = [
    "DiscoveredAsset",
    "DiscoveredRelease",
    "GITHUB_API",
    "GITHUB_HOSTS",
    "GITHUB_UPLOADS",
    "GitHubTokenAuth",
    "create_github_client",
    "fetch_manifest_asset",
    "list_releases",
    "resolve_github_token",
]
