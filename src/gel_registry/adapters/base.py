"""Typed boundaries between product policy and release verification."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol

from ..contracts import BlockedCategory, ReleaseRecord
from ..github import GitHubRelease
from ..policy import PolicyError, ProductPolicy

type Attestor = Callable[[Path], None]


class VerificationError(RuntimeError):
    """Raised when a release cannot be safely classified as deterministic."""


class DeterministicVerificationError(VerificationError):
    """Raised for a deterministic upstream defect that should be blocked."""

    category: BlockedCategory
    assets: tuple[str, ...]

    def __init__(
        self,
        category: BlockedCategory,
        message: str = "",
        *,
        assets: Iterable[str] = (),
    ) -> None:
        self.category = BlockedCategory(category)
        self.assets = tuple(sorted(set(assets)))
        super().__init__(message or self.category.value)


def stable_diagnostic(category: BlockedCategory, assets: Iterable[str] = ()) -> str:
    """Return the stable diagnostic used in blocked-release manifests."""

    diagnostics = {
        BlockedCategory.INVALID_METADATA: "release metadata violates product policy",
        BlockedCategory.MISSING_ARTIFACTS: "release is missing required artifacts",
        BlockedCategory.UNSUPPORTED_PLATFORM: "release uses an unsupported platform",
        BlockedCategory.CHECKSUM_MISMATCH: "artifact checksum verification failed",
        BlockedCategory.ARTIFACT_MISMATCH: (
            "compressed artifact does not match identity"
        ),
        BlockedCategory.ATTESTATION_FAILED: "attestation failed",
    }
    allowed_assets = tuple(sorted(set(assets)))
    suffix = f" for asset(s): {', '.join(allowed_assets)}" if allowed_assets else ""
    return diagnostics[BlockedCategory(category)] + suffix


class ReleaseAdapter(Protocol):
    def canonical_asset_names(self, policy: ProductPolicy) -> tuple[str, ...]:
        """Return the adapter-owned asset names required for one policy."""

    def eligible(self, policy: ProductPolicy, release: GitHubRelease) -> bool:
        """Return whether a stable release is eligible under this product policy."""

    def verify(
        self,
        policy: ProductPolicy,
        release: GitHubRelease,
        assets: Mapping[str, Path],
        attestor: Attestor | None = None,
    ) -> ReleaseRecord:
        """Return the immutable record or raise a categorized verification error."""


def validate_record_policy(record: ReleaseRecord, policy: ProductPolicy) -> None:
    """Validate product-specific record fields declared by a source policy."""

    if record.product != policy.product:
        raise PolicyError(
            f"release record product {record.product!r} does not match policy"
        )
    if record.channel != policy.channel:
        raise PolicyError(
            f"release record channel {record.channel!r} does not match policy"
        )
    if record.source.repository != policy.repository:
        raise PolicyError(
            f"release record repository {record.source.repository!r} does not match "
            f"policy repository {policy.repository!r}"
        )
    if re.fullmatch(policy.tag_pattern, record.source.release_tag) is None:
        raise PolicyError(
            f"release record tag {record.source.release_tag!r} does not match policy"
        )

    expected = {
        (platform, encoding)
        for platform in policy.platforms
        for encoding in policy.encodings
    }
    actual = {(artifact.platform, artifact.encoding) for artifact in record.artifacts}
    if actual != expected or len(actual) != len(record.artifacts):
        raise PolicyError("release record artifacts do not match policy")


__all__ = [
    "Attestor",
    "DeterministicVerificationError",
    "ReleaseAdapter",
    "VerificationError",
    "stable_diagnostic",
    "validate_record_policy",
]
