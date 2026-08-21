"""Compatibility entry points for release verification."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .adapters import Attestor, VerificationError, adapter_for
from .adapters.cli import gh_attestor
from .constants import CLI_PLATFORMS
from .contracts import ReleaseRecord
from .github import GITHUB_REPOSITORY, GitHubRelease
from .policy import ProductPolicy


def _cli_policy() -> ProductPolicy:
    return ProductPolicy(
        product="gel-cli",
        repository=GITHUB_REPOSITORY,
        adapter="gel-cli",
        channel="stable",
        tag_pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+$",
        platforms=CLI_PLATFORMS,
        encodings=("identity", "zstd"),
    )


def verify_cli_release(
    release: GitHubRelease,
    assets: Mapping[str, Path],
    attestor: Attestor | None = None,
) -> ReleaseRecord:
    """Verify a CLI release through the product adapter compatibility boundary."""

    selected_attestor = attestor if attestor is not None else gh_attestor
    return adapter_for("gel-cli").verify(
        _cli_policy(), release, assets, selected_attestor
    )


__all__ = [
    "Attestor",
    "VerificationError",
    "gh_attestor",
    "verify_cli_release",
]
