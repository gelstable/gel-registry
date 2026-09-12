"""Preflight, idempotency, and draft publication for a rescue release plan.

This module is the orchestrator over the narrow primitives:
``gel_registry.github`` performs single REST operations, and
``gel_registry.rescue.transfer`` streams one asset at a time. Nothing here
publishes a release, replaces an asset, or deletes an asset -- the only
mutations ever issued are "create a draft release" and "upload an asset
that is not present yet".

Safety model
------------
Preflight is all-or-nothing: every destination release and asset is
inspected, every problem is collected, and a single
:class:`RescuePreflightError` is raised before any mutation occurs. Only a
completely clean preflight proceeds to execution.

Execution is resumable purely from GitHub state: a rerun of the same plan
re-inspects every destination, skips assets that are already present, and
uploads only what is still absent. Missing binaries are uploaded first,
followed by the generated ``gel-registry.json`` manifest.

Reconciliation obligation
-------------------------
After any upload attempt that raises an exception, this module re-lists
the release's assets rather than retrying blindly. If the offending name
is now present, it stops and reports that asset's ID for explicit manual
cleanup. It never deletes or replaces it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from ..digest import canonical_json
from ..github import (
    GitHubAsset,
    GitHubError,
    GitHubRelease,
    create_draft_release,
    get_release_by_tag,
    list_release_assets,
    upload_release_asset,
)
from .manifests import (
    MANIFEST_NAME,
    release_manifest_bytes,
    release_manifest_digest,
)
from .models import RescueAsset, RescuePlan, RescueRelease
from .transfer import (
    transfer_original_asset,
    verify_uploaded_asset,
)

#: The only repositories a rescue plan may ever write drafts into.
DESTINATION_REPOSITORIES: Final = frozenset(
    {"gelstable/gel", "gelstable/gel-cli", "gelstable/gel-postgis"}
)

#: GitHub's asset upload states. Anything else means an incomplete upload.
_COMPLETE_ASSET_STATE: Final = "uploaded"

_SOURCE_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0)

type AssetStatus = Literal["absent", "matching", "unverified", "conflicting"]


class RescuePublishError(RuntimeError):
    """Raised when a rescue plan cannot be published safely."""


class RescuePreflightError(RescuePublishError):
    """Raised with every preflight problem found, before any mutation."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__(
            "rescue preflight found "
            f"{len(self.problems)} problem(s); no releases or assets were "
            "created: " + "; ".join(self.problems)
        )


class RescueManualCleanupError(RescuePublishError):
    """Raised when a leftover asset must be removed by an operator by hand.

    The rescue tooling never deletes or replaces an asset, so an asset that
    a failed upload left behind has to be reviewed and removed explicitly.
    """

    def __init__(self, message: str, *, asset_id: int | None) -> None:
        self.asset_id = asset_id
        super().__init__(message)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AssetOutcome(_Frozen):
    """How one planned binary asset compares with the destination release."""

    asset: RescueAsset
    status: AssetStatus
    existing: GitHubAsset | None = None
    detail: str | None = None


class ManifestOutcome(_Frozen):
    """How the generated gel-registry.json compares with destination state."""

    status: AssetStatus
    expected_size: int
    expected_sha256: str
    existing: GitHubAsset | None = None
    detail: str | None = None


class ReleaseOutcome(_Frozen):
    """The inspected state of one destination release and its assets."""

    release: RescueRelease
    existing: GitHubRelease | None = None
    assets: tuple[AssetOutcome, ...] = ()
    manifest: ManifestOutcome | None = None

    @property
    def uploads(self) -> tuple[AssetOutcome, ...]:
        return tuple(outcome for outcome in self.assets if outcome.status == "absent")

    @property
    def upload_manifest(self) -> bool:
        return self.manifest is not None and self.manifest.status == "absent"


class PreflightReport(_Frozen):
    """The complete, mutation-free inspection of one rescue plan."""

    plan: RescuePlan
    releases: tuple[ReleaseOutcome, ...]

    @property
    def upload_count(self) -> int:
        return sum(
            len(r.uploads) + (1 if r.upload_manifest else 0) for r in self.releases
        )


def load_plan(path: Path) -> RescuePlan:
    """Read a plan file and require its exact canonical byte representation."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RescuePublishError(f"cannot read rescue plan: {exc}") from exc
    try:
        plan = RescuePlan.model_validate_json(data)
        if canonical_json(plan) != data:
            raise ValueError("rescue plan is not canonical JSON")
        return plan
    except ValueError as exc:
        raise RescuePublishError(f"invalid rescue plan: {exc}") from exc


def _classify_asset(asset: RescueAsset, existing: GitHubAsset | None) -> AssetOutcome:
    """Classify one planned asset against what the release already holds.

    ``absent`` assets are the only ones ever uploaded. ``matching`` means
    GitHub's own metadata positively agrees with the plan. ``unverified``
    means an asset with that name already exists and nothing contradicts
    the plan, but GitHub supplied no evidence that could confirm it --
    reporting neither size nor digest. ``conflicting`` means the existing
    asset positively disagrees with the plan or is an incomplete upload.
    """

    if existing is None:
        return AssetOutcome(asset=asset, status="absent")
    if existing.state is not None and existing.state != _COMPLETE_ASSET_STATE:
        return AssetOutcome(
            asset=asset,
            status="conflicting",
            existing=existing,
            detail=(
                f"incomplete upload (state {existing.state!r}); remove asset id "
                f"{existing.id} explicitly before retrying"
            ),
        )
    if existing.size is not None and existing.size != asset.expected_size:
        return AssetOutcome(
            asset=asset,
            status="conflicting",
            existing=existing,
            detail=(
                f"existing asset id {existing.id} has size {existing.size}, "
                f"plan expects {asset.expected_size}"
            ),
        )
    if existing.sha256 is not None and existing.sha256 != asset.expected_sha256:
        return AssetOutcome(
            asset=asset,
            status="conflicting",
            existing=existing,
            detail=(
                f"existing asset id {existing.id} has a different sha256 than "
                "the plan expects"
            ),
        )
    if (
        existing.state == _COMPLETE_ASSET_STATE
        and existing.size == asset.expected_size
        and existing.sha256 == asset.expected_sha256
    ):
        return AssetOutcome(asset=asset, status="matching", existing=existing)

    missing: list[str] = []
    if existing.state != _COMPLETE_ASSET_STATE:
        missing.append(f"state {_COMPLETE_ASSET_STATE!r}")
    if existing.size is None:
        missing.append("exact size")
    if existing.sha256 is None:
        missing.append("exact sha256")
    detail = (
        f"existing asset id {existing.id} is missing required verification "
        f"metadata ({', '.join(missing)}); operator reconciliation required"
    )
    return AssetOutcome(
        asset=asset,
        status="unverified",
        existing=existing,
        detail=detail,
    )


def _classify_manifest(
    release: RescueRelease,
    existing: GitHubAsset | None,
    binary_outcomes: tuple[AssetOutcome, ...],
) -> tuple[ManifestOutcome, str | None]:
    """Classify the release's generated gel-registry.json manifest asset."""

    manifest_bytes = release_manifest_bytes(release)
    expected_size = len(manifest_bytes)
    expected_sha256 = release_manifest_digest(manifest_bytes)

    if existing is None:
        return ManifestOutcome(
            status="absent",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        ), None

    # An existing manifest cannot claim absent or conflicting binaries.
    non_matching = [o for o in binary_outcomes if o.status != "matching"]
    if non_matching:
        detail = (
            f"existing {MANIFEST_NAME} is present (id {existing.id}) but one or more "
            "planned binary assets are absent or not yet matched; remove the "
            "manifest asset before retrying"
        )
        return ManifestOutcome(
            status="conflicting",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            existing=existing,
            detail=detail,
        ), detail

    if existing.state is not None and existing.state != _COMPLETE_ASSET_STATE:
        detail = (
            f"incomplete upload (state {existing.state!r}); remove asset id "
            f"{existing.id} explicitly before retrying"
        )
        return ManifestOutcome(
            status="conflicting",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            existing=existing,
            detail=detail,
        ), detail

    if existing.size is not None and existing.size != expected_size:
        detail = (
            f"existing manifest id {existing.id} has size {existing.size}, "
            f"expected {expected_size}"
        )
        return ManifestOutcome(
            status="conflicting",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            existing=existing,
            detail=detail,
        ), detail

    if existing.sha256 is not None and existing.sha256 != expected_sha256:
        detail = (
            f"existing manifest id {existing.id} has a different sha256 than expected"
        )
        return ManifestOutcome(
            status="conflicting",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            existing=existing,
            detail=detail,
        ), detail

    if (
        existing.state == _COMPLETE_ASSET_STATE
        and existing.size == expected_size
        and existing.sha256 == expected_sha256
    ):
        return ManifestOutcome(
            status="matching",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            existing=existing,
        ), None

    missing = []
    if existing.state != _COMPLETE_ASSET_STATE:
        missing.append(f"state {_COMPLETE_ASSET_STATE!r}")
    if existing.size is None:
        missing.append("exact size")
    if existing.sha256 is None:
        missing.append("exact sha256")
    detail = (
        f"existing manifest id {existing.id} is missing required verification "
        f"metadata ({', '.join(missing)}); operator reconciliation required"
    )
    return ManifestOutcome(
        status="unverified",
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        existing=existing,
        detail=detail,
    ), detail


def _inspect_release(
    client: httpx.Client, release: RescueRelease
) -> tuple[ReleaseOutcome, list[str]]:
    problems: list[str] = []
    destination = f"{release.repository}@{release.tag}"
    existing = get_release_by_tag(client, release.repository, release.tag)
    if existing is not None and not existing.draft:
        problems.append(f"{destination}: an existing release is not a draft")
        return ReleaseOutcome(release=release, existing=existing), problems

    present: dict[str, GitHubAsset] = {}
    if existing is not None:
        seen_names: set[str] = set()
        for gh_asset in list_release_assets(client, release.repository, existing):
            if gh_asset.name in seen_names:
                problems.append(
                    f"{destination}: duplicate asset name on GitHub: {gh_asset.name}"
                )
            seen_names.add(gh_asset.name)
            present[gh_asset.name] = gh_asset

    binary_outcomes: list[AssetOutcome] = []
    for asset in release.assets:
        outcome = _classify_asset(asset, present.get(asset.destination_name))
        binary_outcomes.append(outcome)
        if outcome.status in {"conflicting", "unverified"}:
            problems.append(f"{destination}/{asset.destination_name}: {outcome.detail}")

    manifest_outcome, manifest_problem = _classify_manifest(
        release, present.get(MANIFEST_NAME), tuple(binary_outcomes)
    )
    if manifest_problem is not None:
        problems.append(f"{destination}/{MANIFEST_NAME}: {manifest_problem}")

    return (
        ReleaseOutcome(
            release=release,
            existing=existing,
            assets=tuple(binary_outcomes),
            manifest=manifest_outcome,
        ),
        problems,
    )


def _declared_size(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def _ranged_probe(client: httpx.Client, asset: RescueAsset) -> int | None:
    """Probe one source object with ``Range: bytes=0-0``, reading no body.

    Returns the object's total size when it can be determined, ``-1`` when
    the object is available but its size is not stated, and ``None`` when
    the object is not available at all.
    """

    try:
        request = client.build_request(
            "GET",
            asset.source_url,
            headers={"Range": "bytes=0-0"},
            timeout=_SOURCE_TIMEOUT,
        )
        response = client.send(request, stream=True, follow_redirects=False)
    except httpx.HTTPError:
        return None
    try:
        if response.status_code not in {200, 206}:
            return None
        content_range = response.headers.get("Content-Range")
        if response.status_code == 206 and content_range is not None:
            total = content_range.rsplit("/", 1)[-1].strip()
            return int(total) if total.isdigit() else -1
        if response.status_code == 200:
            declared = _declared_size(response.headers.get("Content-Length"))
            return declared if declared is not None else -1
        return -1
    finally:
        response.close()


def _check_source_available(client: httpx.Client, asset: RescueAsset) -> str | None:
    """Confirm one source object still exists, without fetching its body."""

    try:
        response = client.request(
            "HEAD",
            asset.source_url,
            follow_redirects=False,
            timeout=_SOURCE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return f"{asset.destination_name}: source request failed: {exc}"
    if response.status_code == 200:
        declared = _declared_size(response.headers.get("Content-Length"))
    else:
        head_status = response.status_code
        ranged = _ranged_probe(client, asset)
        if ranged is None:
            return (
                f"{asset.destination_name}: source returned HTTP "
                f"{head_status} to HEAD and no usable ranged GET: "
                f"{asset.source_url}"
            )
        declared = None if ranged < 0 else ranged
    if declared is not None and declared != asset.expected_size:
        return (
            f"{asset.destination_name}: source reports {declared} bytes, "
            f"plan expects {asset.expected_size}"
        )
    return None


def preflight(
    client: httpx.Client,
    plan: RescuePlan,
    *,
    allowed_repositories: frozenset[str] = DESTINATION_REPOSITORIES,
) -> PreflightReport:
    """Inspect every destination and prove the plan is safe to execute.

    Raises :class:`RescuePreflightError` listing *every* problem found. No
    release or asset is created by this function under any circumstance.
    """

    problems: list[str] = []
    disallowed = sorted(
        {
            release.repository
            for release in plan.releases
            if release.repository not in allowed_repositories
        }
    )
    problems.extend(
        f"{repository}: destination repository is not allowlisted"
        for repository in disallowed
    )
    if problems:
        raise RescuePreflightError(problems)

    releases: list[ReleaseOutcome] = []
    try:
        for release in plan.releases:
            outcome, release_problems = _inspect_release(client, release)
            releases.append(outcome)
            problems.extend(release_problems)
    except GitHubError as exc:
        raise RescuePublishError(f"destination inspection failed: {exc}") from exc

    for outcome in releases:
        for upload in outcome.uploads:
            problem = _check_source_available(client, upload.asset)
            if problem is not None:
                problems.append(
                    f"{outcome.release.repository}@{outcome.release.tag}/{problem}"
                )
    if problems:
        raise RescuePreflightError(problems)
    return PreflightReport(plan=plan, releases=tuple(releases))


def describe_operations(report: PreflightReport) -> tuple[str, ...]:
    """Render the exact operations a non-dry run would perform, in order."""

    lines: list[str] = []
    for outcome in report.releases:
        destination = f"{outcome.release.repository}@{outcome.release.tag}"
        has_uploads = bool(outcome.uploads or outcome.upload_manifest)
        if outcome.existing is None:
            lines.append(
                f"create draft release {destination}"
                if has_uploads
                else f"skip {destination} (nothing to upload; no draft is created)"
            )
        else:
            lines.append(
                f"reuse draft release {destination} (id {outcome.existing.id})"
            )
        for asset in outcome.assets:
            name = asset.asset.destination_name
            if asset.status == "absent":
                lines.append(f"  upload {name} ({asset.asset.expected_size} bytes)")
            elif asset.status == "matching":
                lines.append(f"  skip {name} (already matches)")
            else:
                lines.append(f"  skip {name} (unverified: {asset.detail})")
        if outcome.manifest is not None:
            if outcome.manifest.status == "absent":
                lines.append(
                    f"  upload {MANIFEST_NAME} ({outcome.manifest.expected_size} bytes)"
                )
            elif outcome.manifest.status == "matching":
                lines.append(f"  skip {MANIFEST_NAME} (already matches)")
            else:
                lines.append(
                    f"  skip {MANIFEST_NAME} (unverified: {outcome.manifest.detail})"
                )
    lines.append(f"total uploads: {report.upload_count}")
    return tuple(lines)


def _reconcile_failed_upload(
    client: httpx.Client,
    repository: str,
    release: GitHubRelease,
    asset_name: str,
    error: Exception,
) -> RescuePublishError:
    """Re-inspect a release after an upload that may have left an asset.

    Never retries and never removes anything: it only turns the failure
    into a precise instruction for an operator.
    """

    destination = f"{repository}@{release.tag}/{asset_name}"
    try:
        assets = list_release_assets(client, repository, release)
    except GitHubError as exc:
        return RescuePublishError(
            f"{destination}: upload failed ({error}) and the release could not "
            f"be re-inspected ({exc}); inspect it manually before retrying"
        )
    leftover = next((item for item in assets if item.name == asset_name), None)
    if leftover is None:
        return RescuePublishError(
            f"{destination}: upload failed ({error}); no asset was left on the "
            "release, so this plan can be rerun once the cause is understood"
        )
    return RescueManualCleanupError(
        f"{destination}: upload failed ({error}) and left asset id "
        f"{leftover.id} on the draft release; remove it explicitly before "
        "rerunning this plan -- nothing is deleted automatically",
        asset_id=leftover.id,
    )


def _upload_binary(
    client: httpx.Client,
    planned: RescueRelease,
    release: GitHubRelease,
    asset: RescueAsset,
) -> None:
    try:
        transfer_original_asset(client, planned.repository, release, asset)
    except Exception as exc:
        raise _reconcile_failed_upload(
            client, planned.repository, release, asset.destination_name, exc
        ) from exc


def _upload_manifest(
    client: httpx.Client,
    planned: RescueRelease,
    release: GitHubRelease,
) -> None:
    manifest_bytes = release_manifest_bytes(planned)
    expected_size = len(manifest_bytes)
    expected_sha256 = release_manifest_digest(manifest_bytes)
    try:
        github_asset = upload_release_asset(
            client,
            planned.repository,
            release,
            MANIFEST_NAME,
            "application/json",
            expected_size,
            iter((manifest_bytes,)),
        )
        verify_uploaded_asset(
            MANIFEST_NAME, github_asset, expected_size, expected_sha256
        )
    except Exception as exc:
        raise _reconcile_failed_upload(
            client, planned.repository, release, MANIFEST_NAME, exc
        ) from exc


def execute(
    client: httpx.Client,
    report: PreflightReport,
) -> int:
    """Create missing drafts and upload missing assets and manifests in order.

    Missing binaries are uploaded first; the manifest is uploaded last.
    Returns the total number of uploaded assets.
    """

    uploaded_count = 0
    for outcome in report.releases:
        pending = outcome.uploads
        upload_manifest = outcome.upload_manifest
        if not pending and not upload_manifest:
            continue

        release = outcome.existing
        if release is None:
            try:
                release = create_draft_release(
                    client,
                    outcome.release.repository,
                    outcome.release.tag,
                    outcome.release.target_commitish,
                )
            except GitHubError as exc:
                raise RescuePublishError(
                    f"{outcome.release.repository}@{outcome.release.tag}: "
                    f"draft release creation failed: {exc}"
                ) from exc

        # Missing binaries first
        for upload in pending:
            _upload_binary(client, outcome.release, release, upload.asset)
            uploaded_count += 1

        # Manifest uploaded last
        if upload_manifest:
            _upload_manifest(client, outcome.release, release)
            uploaded_count += 1

    return uploaded_count


def publish_plan(
    client: httpx.Client,
    plan: RescuePlan,
    *,
    dry_run: bool = False,
    allowed_repositories: frozenset[str] = DESTINATION_REPOSITORIES,
) -> tuple[str, ...]:
    """Run the whole preflight, then either describe or perform the plan."""

    report = preflight(client, plan, allowed_repositories=allowed_repositories)
    operations = describe_operations(report)
    if dry_run:
        return ("dry run: no releases or assets were created", *operations)
    uploaded = execute(client, report)
    return (
        *operations,
        f"uploaded {uploaded} asset(s)",
    )


__all__ = [
    "DESTINATION_REPOSITORIES",
    "AssetOutcome",
    "ManifestOutcome",
    "PreflightReport",
    "ReleaseOutcome",
    "RescueManualCleanupError",
    "RescuePreflightError",
    "RescuePublishError",
    "describe_operations",
    "execute",
    "load_plan",
    "preflight",
    "publish_plan",
]
