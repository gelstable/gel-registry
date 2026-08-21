"""Trusted access to the allowlisted GitHub release repository."""

from __future__ import annotations

from .assets import DeterministicAssetError, download_assets
from .models import (
    GITHUB_REPOSITORY,
    GitHubAsset,
    GitHubError,
    GitHubRelease,
    asset_name,
    canonical_asset_names,
)
from .releases import discover_cli_releases, discover_repository_releases
from .transport import GITHUB_API

__all__ = [
    "GITHUB_API",
    "GITHUB_REPOSITORY",
    "DeterministicAssetError",
    "GitHubAsset",
    "GitHubError",
    "GitHubRelease",
    "asset_name",
    "canonical_asset_names",
    "discover_cli_releases",
    "discover_repository_releases",
    "download_assets",
]
