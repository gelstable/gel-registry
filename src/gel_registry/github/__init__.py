"""Narrowly scoped, extensible GitHub release transport and discovery."""

from .discovery import fetch_manifest_asset, list_releases
from .models import (
    DiscoveredAsset,
    DiscoveredRelease,
    GitHubAsset,
    GitHubError,
    GitHubRelease,
)
from .transport import (
    GITHUB_API,
    GITHUB_HOSTS,
    GITHUB_UPLOADS,
    GitHubTokenAuth,
    create_github_client,
    resolve_github_token,
)
from .uploads import (
    create_draft_release,
    get_release_by_tag,
    list_release_assets,
    upload_release_asset,
)

__all__ = [
    "DiscoveredAsset",
    "DiscoveredRelease",
    "GITHUB_API",
    "GITHUB_HOSTS",
    "GITHUB_UPLOADS",
    "GitHubAsset",
    "GitHubError",
    "GitHubRelease",
    "GitHubTokenAuth",
    "create_draft_release",
    "create_github_client",
    "fetch_manifest_asset",
    "get_release_by_tag",
    "list_release_assets",
    "list_releases",
    "resolve_github_token",
    "upload_release_asset",
]
