"""The public candidate-promotion operation."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..adapters import validate_record_policy
from ..contracts import BlockedRelease, ReleaseRecord, semver_key
from ..policy import PolicyError, load_source_policy
from ..publication import PublicationError
from ..publication import publish_bootstrap as _publish_bootstrap
from .result import PromotionError, PromotionResult
from .staging import _validated_record
from .transaction import _transaction


def publish_bootstrap(repo: Path) -> PromotionResult:
    """Compatibility entry point backed by the unchanged neutral operation."""

    try:
        result = _publish_bootstrap(Path(repo))
    except PublicationError as exc:
        raise PromotionError(str(exc)) from exc
    return PromotionResult(
        snapshot=result.snapshot,
        branch="publish/legacy-2026-08-bootstrap",
        changed_paths=result.changed_paths,
    )


def promote_candidate(
    repo: Path,
    records: Iterable[ReleaseRecord],
    blocked: tuple[BlockedRelease, ...],
) -> PromotionResult:
    """Install one complete, already verified promotion candidate."""

    try:
        policies = load_source_policy(Path(repo)).by_product()
    except PolicyError as exc:
        raise PromotionError(f"could not load product policy: {exc}") from exc

    validated: list[ReleaseRecord] = []
    for value in records:
        record = _validated_record(value)
        policy = policies.get(record.product)
        if policy is None:
            raise PromotionError(
                f"release record product {record.product!r} has no product policy"
            )
        try:
            validate_record_policy(record, policy)
        except PolicyError as exc:
            raise PromotionError(
                f"release record does not match product policy: {exc}"
            ) from exc
        validated.append(record)

    ordered = tuple(
        sorted(
            validated,
            key=lambda record: (
                record.product,
                semver_key(record.version),
                record.source.release_id,
                record.version,
            ),
        )
    )
    return _transaction(
        Path(repo),
        releases=ordered,
        blocked=tuple(blocked),
        branch="promote/registry",
    )


__all__ = ["promote_candidate", "publish_bootstrap"]
