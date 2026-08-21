"""Explicitly-scoped remote verification of newly added release records.

This is the only validation layer that reaches the network, so workflows can
opt into it for release pull requests alone.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path

import httpx
from pydantic import ValidationError as PydanticValidationError

from ..adapters import DeterministicVerificationError, adapter_for, stable_diagnostic
from ..contracts import BlockedCategory, BlockedRelease, ReleaseRecord
from ..github import (
    GITHUB_API,
    GITHUB_REPOSITORY,
    DeterministicAssetError,
    GitHubAsset,
    GitHubRelease,
    asset_name,
    canonical_asset_names,
    download_assets,
)
from ..github.transport import API_HEADERS, REQUEST_TIMEOUT
from ..policy import PolicyError, SourcePolicy, load_source_policy
from ..verify import VerificationError, verify_cli_release
from .report import Collector, ValidationReport
from .support import display_path, read_file

type _BlockedObservation = tuple[BlockedCategory, tuple[str, ...]]


def validate_release_remotes(
    repo: Path,
    changed_records: Iterable[object] | Mapping[object, object],
    *,
    policy: SourcePolicy | None = None,
) -> ValidationReport:
    """Verify only the caller-supplied newly-added release records remotely."""

    repository = Path(repo)
    collector = Collector()
    collector.begin("remote.release.scope")
    try:
        pairs = _changed_release_pairs(repository, changed_records)
    except (OSError, PydanticValidationError, ValueError) as exc:
        collector.add("remote.release.scope", "releases", str(exc))
        return collector.report()
    if not pairs:
        return collector.report()

    collector.begin("remote.release.bytes")
    collector.begin("remote.release.attestations")
    try:
        client = httpx.Client()
    except Exception as exc:
        collector.add(
            "remote.release.scope", "releases", f"could not create HTTP client: {exc}"
        )
        return collector.report()
    try:
        for release, record, path in pairs:
            label = display_path(repository, path)
            with tempfile.TemporaryDirectory(
                prefix=".gel-registry-release-"
            ) as directory:
                destination = Path(directory) / "assets"
                try:
                    if record is None:
                        raise ValueError("remote release record is unavailable")
                    if policy is None:
                        assets = download_assets(
                            client,
                            release,
                            destination,
                            names=canonical_asset_names(),
                        )
                        observed = verify_cli_release(release, assets)
                    else:
                        release = _fetch_release_by_id(client, record)
                        product_policy = policy.by_product().get(record.product)
                        if product_policy is None:
                            raise PolicyError(
                                f"release product {record.product!r} has no policy"
                            )
                        adapter = adapter_for(product_policy.adapter)
                        canonical_names = adapter.canonical_asset_names(product_policy)
                        assets = download_assets(
                            client,
                            release,
                            destination,
                            repository=record.source.repository,
                            names=canonical_names,
                        )
                        observed = adapter.verify(product_policy, release, assets)
                    _compare_remote_record(record, observed)
                except Exception as exc:
                    collector.add("remote.release.bytes", label, str(exc))
    finally:
        with suppress(Exception):
            client.close()
    return collector.report()


def validate_blocked_remotes(
    repo: Path,
    entries: Iterable[BlockedRelease],
    *,
    policy: SourcePolicy | None = None,
) -> ValidationReport:
    """Verify that each committed blocked entry still has its stated cause."""

    repository = Path(repo)
    collector = Collector()
    collector.begin("remote.blocked.scope")
    values = tuple(entries)
    if not values:
        return collector.report()

    selected_policy = policy
    if selected_policy is None:
        try:
            selected_policy = load_source_policy(repository)
        except (OSError, PolicyError, ValueError) as exc:
            collector.add("remote.blocked.scope", "sources/products.json", str(exc))
            return collector.report()
    assert selected_policy is not None

    collector.begin("remote.blocked.category")
    try:
        client = httpx.Client()
    except Exception as exc:
        collector.add(
            "remote.blocked.scope",
            "promotion/blocked.json",
            f"could not create HTTP client: {exc}",
        )
        return collector.report()
    try:
        for entry in values:
            label = f"releases/{entry.product}/{entry.tag}.json"
            try:
                observed = _verify_blocked_entry(client, entry, selected_policy)
                if observed is None:
                    raise VerificationError("blocked release verified successfully")
                if isinstance(observed, BlockedCategory):
                    observed_category = observed
                    observed_assets = ()
                else:
                    observed_category, observed_assets = observed
                expected = entry.category
                if observed_category != expected:
                    raise VerificationError(
                        "blocked category mismatch: "
                        f"expected {expected}, observed {observed_category}"
                    )
                expected_diagnostic = stable_diagnostic(
                    observed_category, observed_assets
                )
                if entry.diagnostic != expected_diagnostic:
                    raise VerificationError(
                        f"blocked diagnostic mismatch: expected {expected_diagnostic!r}"
                    )
            except Exception as exc:
                collector.add("remote.blocked.category", label, str(exc))
    finally:
        with suppress(Exception):
            client.close()
    return collector.report()


def _fetch_release_by_id(client: httpx.Client, record: ReleaseRecord) -> GitHubRelease:
    return _fetch_release_identity(
        client,
        record.source.repository,
        record.source.release_id,
        record.source.release_tag,
    )


def _fetch_release_identity(
    client: httpx.Client,
    repository: str,
    release_id: int,
    release_tag: str,
) -> GitHubRelease:
    endpoint = f"{GITHUB_API}/repos/{repository}/releases/{release_id}"
    try:
        response = client.get(
            endpoint,
            headers=API_HEADERS,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise VerificationError(f"GitHub release request failed: {exc}") from exc
    if response.status_code != 200:
        raise VerificationError(
            f"GitHub release request returned HTTP {response.status_code}"
        )
    try:
        payload = response.json()
        release = GitHubRelease.model_validate(
            payload, context={"repository": repository}
        )
    except (PydanticValidationError, ValueError) as exc:
        raise VerificationError(f"invalid GitHub release response: {exc}") from exc
    if release.id != release_id:
        raise VerificationError("remote release ID differs from release record")
    if release.tag_name != release_tag:
        raise VerificationError("remote release tag differs from release record")
    if release.repository != repository:
        raise VerificationError("remote release repository differs from release record")
    return release


def _verify_blocked_entry(
    client: httpx.Client,
    entry: BlockedRelease,
    policy: SourcePolicy,
) -> _BlockedObservation | None:
    product = entry.product
    product_policy = policy.by_product().get(product)
    if product_policy is None:
        raise PolicyError(f"blocked product {product!r} has no policy")
    release = _fetch_release_identity(
        client, entry.repository, entry.release_id, entry.tag
    )
    adapter = adapter_for(product_policy.adapter)
    release_assets = {asset.name for asset in release.assets}
    canonical_names = adapter.canonical_asset_names(product_policy)
    missing = sorted(set(canonical_names) - release_assets)
    if missing:
        return (BlockedCategory.MISSING_ARTIFACTS, tuple(missing))
    with tempfile.TemporaryDirectory(prefix=".gel-registry-blocked-") as directory:
        try:
            assets = download_assets(
                client,
                release,
                Path(directory) / "assets",
                repository=entry.repository,
                names=canonical_names,
            )
        except DeterministicAssetError as exc:
            return (
                BlockedCategory.INVALID_METADATA,
                tuple(asset for asset in exc.assets if asset in release_assets),
            )
        try:
            adapter.verify(product_policy, release, assets)
        except DeterministicVerificationError as exc:
            return (
                exc.category,
                tuple(asset for asset in exc.assets if asset in release_assets),
            )
    return None


def _synthetic_release(record: ReleaseRecord) -> GitHubRelease:
    assets: list[GitHubAsset] = []
    for index, artifact in enumerate(record.artifacts, 1):
        name = asset_name(artifact.platform, artifact.encoding)
        assets.append(
            GitHubAsset(
                name=name,
                url=f"https://api.github.com/repos/{GITHUB_REPOSITORY}/assets/{index}",
                browser_download_url=artifact.url,
                id=index,
                size=artifact.size,
            )
        )
    return GitHubRelease(
        id=record.source.release_id,
        tag_name=record.source.release_tag,
        assets=tuple(assets),
        repository=record.source.repository,
    )


def _load_release_path(repository: Path, value: object) -> tuple[ReleaseRecord, Path]:
    path = Path(value) if isinstance(value, (str, Path)) else None
    if path is None:
        raise ValueError(f"unsupported changed release value: {value!r}")
    if not path.is_absolute():
        path = repository / path
    raw = read_file(path)
    record = ReleaseRecord.model_validate_json(raw)
    return record, path


def _changed_release_pairs(
    repository: Path,
    changed_records: Iterable[object] | Mapping[object, object],
) -> list[tuple[GitHubRelease, ReleaseRecord | None, Path]]:
    values = (
        list(changed_records.values())
        if isinstance(changed_records, Mapping)
        else list(changed_records)
    )
    result: list[tuple[GitHubRelease, ReleaseRecord | None, Path]] = []
    for value in values:
        release: GitHubRelease | None = None
        record: ReleaseRecord | None = None
        path: Path | None = None
        if isinstance(value, tuple) and len(value) == 2:
            left, right = value
            if isinstance(left, GitHubRelease):
                release = left
            elif isinstance(right, GitHubRelease):
                release = right
            if isinstance(left, ReleaseRecord):
                record = left
            elif isinstance(right, ReleaseRecord):
                record = right
            if isinstance(left, (str, Path)):
                path = Path(left)
            elif isinstance(right, (str, Path)):
                path = Path(right)
        elif isinstance(value, GitHubRelease):
            release = value
        elif isinstance(value, ReleaseRecord):
            record = value
        elif isinstance(value, (str, Path)):
            path = Path(value)
        else:
            raise ValueError(f"unsupported changed release value: {value!r}")

        if record is None and path is not None:
            record, path = _load_release_path(repository, path)
        if record is None and release is not None:
            candidate = repository / "releases" / "gel-cli" / f"{release.version}.json"
            if candidate.is_file():
                record, path = _load_release_path(repository, candidate)
        if release is None and record is not None:
            release = _synthetic_release(record)
        if release is None:
            raise ValueError("changed release has no release metadata")
        if path is None:
            path = repository / "releases" / "gel-cli" / f"{release.version}.json"
        result.append((release, record, path))
    return result


def _compare_remote_record(expected: ReleaseRecord, observed: ReleaseRecord) -> None:
    if (
        expected.product != observed.product
        or expected.channel != observed.channel
        or expected.version != observed.version
        or expected.source != observed.source
    ):
        raise VerificationError("remote release provenance differs from release record")
    expected_artifacts = {
        (artifact.platform, artifact.encoding): artifact
        for artifact in expected.artifacts
    }
    observed_artifacts = {
        (artifact.platform, artifact.encoding): artifact
        for artifact in observed.artifacts
    }
    if set(expected_artifacts) != set(observed_artifacts):
        raise VerificationError(
            "remote release artifact target set differs from the record"
        )
    for key in sorted(expected_artifacts):
        left = expected_artifacts[key]
        right = observed_artifacts[key]
        if (
            left.url != right.url
            or left.media_type != right.media_type
            or left.size != right.size
            or left.sha256 != right.sha256
            or left.blake2b != right.blake2b
        ):
            raise VerificationError(
                f"remote artifact differs from release record for {key[0]}/{key[1]}"
            )


__all__ = ["validate_blocked_remotes", "validate_release_remotes"]
