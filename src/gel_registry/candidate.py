"""Build a deterministic, promotion-ready collection of release discoveries."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError

from .adapters import (
    Attestor,
    DeterministicVerificationError,
    VerificationError,
    adapter_for,
    stable_diagnostic,
    validate_record_policy,
)
from .contracts import BlockedCategory, BlockedRelease, ReleaseRecord, semver_key
from .digest import canonical_json
from .github import (
    DeterministicAssetError,
    GitHubError,
    GitHubRelease,
    discover_repository_releases,
    download_assets,
)
from .policy import PolicyError, ProductPolicy, SourcePolicy, load_source_policy


class CandidateError(ValueError):
    """Raised when committed candidate inputs are invalid."""


@dataclass(frozen=True)
class Candidate:
    """The complete result of one discovery and verification pass."""

    base_records: tuple[ReleaseRecord, ...]
    records: tuple[ReleaseRecord, ...]
    blocked: tuple[BlockedRelease, ...]


type _SemVerKey = tuple[
    int,
    int,
    int,
    tuple[tuple[int, int | str], ...],
    int,
]
type _RecordSortKey = tuple[str, _SemVerKey, int, str]


def build_candidate(
    repo: Path,
    client: httpx.Client,
    *,
    attestor: Attestor | None = None,
) -> Candidate:
    """Discover and verify every missing release without mutating ``repo``."""

    repo = Path(repo)
    policy = load_source_policy(repo)
    base_records = load_releases(repo)
    releases_by_repository = {
        repository: discover_repository_releases(client, repository)
        for repository in policy.repositories()
    }
    records, blocked = evaluate_products(
        policy,
        releases_by_repository,
        base_records,
        client,
        attestor,
    )
    return Candidate(base_records, records, blocked)


def load_releases(repo: Path) -> tuple[ReleaseRecord, ...]:
    """Load the immutable, canonical release records already in ``repo``."""

    root = Path(repo) / "releases"
    if not root.exists():
        if root.is_symlink():
            raise CandidateError(f"release root is a symlink: {root}")
        return ()
    if root.is_symlink() or not root.is_dir():
        raise CandidateError(f"release root is not a directory: {root}")

    records: list[ReleaseRecord] = []
    identities: set[tuple[str, str, int]] = set()
    paths = tuple(sorted(root.rglob("*"), key=lambda path: path.relative_to(root)))
    for path in paths:
        relative = path.relative_to(root)
        if path.is_symlink():
            raise CandidateError(f"release tree contains symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file() or path.suffix != ".json":
            raise CandidateError(f"unexpected release path: {relative}")
        if len(relative.parts) != 2:
            raise CandidateError(f"unexpected release path: {relative}")

        raw = path.read_bytes()
        try:
            record = ReleaseRecord.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise CandidateError(f"invalid release record {relative}: {exc}") from exc
        if raw != canonical_json(record):
            raise CandidateError(f"release record is not canonical: {relative}")
        if relative.parts[0] != record.product or relative.stem != record.version:
            raise CandidateError(
                f"release record path does not match record identity: {relative}"
            )

        identity = (
            record.product,
            record.source.repository,
            record.source.release_id,
        )
        if identity in identities:
            raise CandidateError(f"duplicate release identity: {identity!r}")
        identities.add(identity)
        records.append(record)
    return tuple(sorted(records, key=_record_sort_key))


def evaluate_products(
    policy: SourcePolicy,
    releases_by_repository: Mapping[str, tuple[GitHubRelease, ...]],
    base_records: tuple[ReleaseRecord, ...],
    client: httpx.Client,
    attestor: Attestor | None,
) -> tuple[tuple[ReleaseRecord, ...], tuple[BlockedRelease, ...]]:
    """Verify missing releases for every product in canonical policy order."""

    existing = {
        (record.product, record.source.repository, record.source.release_id)
        for record in base_records
    }
    records: list[ReleaseRecord] = []
    blocked: list[BlockedRelease] = []
    with tempfile.TemporaryDirectory(prefix="gel-registry-candidate-") as scratch:
        scratch_root = Path(scratch)
        for product in policy.products:
            adapter = adapter_for(product.adapter)
            canonical_names = adapter.canonical_asset_names(product)
            releases = releases_by_repository.get(product.repository, ())
            for release_index, release in enumerate(releases):
                if not adapter.eligible(product, release):
                    continue
                identity = (product.product, product.repository, release.id)
                if identity in existing:
                    continue
                release_names = {asset.name for asset in release.assets}
                missing = sorted(set(canonical_names) - release_names)
                if missing:
                    blocked.append(
                        _blocked_release(
                            product,
                            release,
                            DeterministicVerificationError(
                                BlockedCategory.MISSING_ARTIFACTS,
                                "release is missing canonical assets: "
                                + ", ".join(missing),
                                assets=missing,
                            ),
                            allowed_assets=canonical_names,
                        )
                    )
                    continue
                try:
                    assets = download_assets(
                        client,
                        release,
                        scratch_root
                        / f"release-{product.product}-{release.id}-{release_index}",
                        repository=product.repository,
                        names=canonical_names,
                    )
                except DeterministicAssetError as exc:
                    blocked.append(
                        _blocked_release(
                            product,
                            release,
                            DeterministicVerificationError(
                                BlockedCategory.INVALID_METADATA,
                                str(exc),
                                assets=exc.assets,
                            ),
                            allowed_assets=canonical_names,
                        )
                    )
                    continue
                try:
                    record = adapter.verify(product, release, assets, attestor)
                    validate_record_policy(record, product)
                    if record.source.release_id != release.id:
                        raise PolicyError(
                            "verified record release ID does not match discovery"
                        )
                except (PolicyError, ValidationError) as exc:
                    blocked.append(
                        _blocked_release(
                            product,
                            release,
                            DeterministicVerificationError(
                                BlockedCategory.INVALID_METADATA, str(exc)
                            ),
                        )
                    )
                except DeterministicVerificationError as exc:
                    blocked.append(_blocked_release(product, release, exc))
                except VerificationError as exc:
                    _raise_infrastructure_cause(exc)
                    raise
                else:
                    records.append(record)
                    existing.add(identity)

    return (
        tuple(sorted(records, key=_record_sort_key)),
        tuple(sorted(blocked, key=_blocked_sort_key)),
    )


def _record_sort_key(record: ReleaseRecord) -> _RecordSortKey:
    return (
        record.product,
        semver_key(record.version),
        record.source.release_id,
        record.version,
    )


def _blocked_sort_key(
    entry: BlockedRelease,
) -> tuple[str, str, int, str]:
    return entry.product, entry.repository, entry.release_id, entry.tag


def _blocked_release(
    policy: ProductPolicy,
    release: GitHubRelease,
    error: DeterministicVerificationError,
    *,
    allowed_assets: Iterable[str] = (),
) -> BlockedRelease:
    release_assets = {asset.name for asset in release.assets}
    allowed_asset_names = release_assets | set(allowed_assets)
    diagnostic_assets = tuple(
        sorted({asset for asset in error.assets if asset in allowed_asset_names})
    )
    return BlockedRelease(
        product=policy.product,
        repository=policy.repository,
        release_id=release.id,
        tag=release.tag_name,
        category=error.category,
        diagnostic=stable_diagnostic(error.category, diagnostic_assets),
    )


def _exception_chain(error: BaseException) -> Iterable[BaseException]:
    """Yield an exception and its explicit or implicit causes once each."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _raise_infrastructure_cause(error: BaseException) -> None:
    """Re-raise wrapped infrastructure errors instead of blocking them."""

    for current in _exception_chain(error):
        if isinstance(
            current,
            (httpx.HTTPError, GitHubError, OSError, subprocess.SubprocessError),
        ):
            raise current from error


__all__ = [
    "Candidate",
    "CandidateError",
    "build_candidate",
    "evaluate_products",
    "load_releases",
]
