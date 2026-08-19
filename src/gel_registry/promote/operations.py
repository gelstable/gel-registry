"""The two public publication operations."""

from __future__ import annotations

from pathlib import Path

from ..contracts import ReleaseRecord
from .result import PromotionResult
from .staging import _validated_record
from .transaction import _transaction


def publish_bootstrap(repo: Path) -> PromotionResult:
    """Build, select, and stage the first nonempty bootstrap-backed snapshot."""

    return _transaction(
        Path(repo),
        release=None,
        branch="publish/legacy-2026-08-bootstrap",
    )


def promote_release(repo: Path, record: ReleaseRecord) -> PromotionResult:
    """Publish one verified stable CLI release over the full bootstrap."""

    validated = _validated_record(record)
    return _transaction(
        Path(repo),
        release=validated,
        branch=f"promote/gel-cli-{validated.version}",
    )
