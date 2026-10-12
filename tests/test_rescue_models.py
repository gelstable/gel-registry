"""Canonical contracts for safe, reproducible rescue operations."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gel_registry.rescue.models import (
    RescueAsset,
    RescuePlan,
    RescueRelease,
)


def _asset(*, name: str = "gel-cli", sha256: str = "a" * 64) -> RescueAsset:
    return RescueAsset(
        source_url="https://packages.edgedb.com/archive/gel-cli",
        destination_name=name,
        content_type="application/octet-stream",
        expected_size=12,
        expected_sha256=sha256,
    )


def _release(
    *,
    repository: str = "gelstable/gel",
    tag: str = "v1.0.0",
    assets: tuple[RescueAsset, ...] | None = None,
) -> RescueRelease:
    return RescueRelease(
        repository=repository,
        tag=tag,
        target_commitish=None,
        assets=assets if assets is not None else (_asset(),),
    )


def test_asset_rejects_source_urls_outside_the_safe_package_origins() -> None:
    """A changed source-host check must fail before unsafe bytes are transferred."""

    for url in (
        "http://packages.edgedb.com/archive/gel-cli",
        "https://example.com/archive/gel-cli",
        "https://user@packages.geldata.com/archive/gel-cli",
        "https://packages.geldata.com:444/archive/gel-cli",
        "https://packages.geldata.com/archive/../secret",
        "https://packages.geldata.com/archive//secret",
    ):
        with pytest.raises(ValidationError):
            RescueAsset(
                source_url=url,
                destination_name="gel-cli",
                content_type="application/octet-stream",
                expected_size=12,
                expected_sha256="a" * 64,
            )


def test_asset_requires_non_null_sha256() -> None:
    """Every selected artifact must have the exact SHA-256 for promotion replacement."""

    with pytest.raises(ValidationError):
        RescueAsset.model_validate(
            {
                "source_url": "https://packages.edgedb.com/archive/gel-cli",
                "destination_name": "gel-cli",
                "content_type": "application/octet-stream",
                "expected_size": 12,
                "expected_sha256": None,
            }
        )


def test_plan_rejects_duplicate_destination_release_pairs() -> None:
    """Removing release uniqueness must not permit ambiguous draft targets."""

    with pytest.raises(ValidationError, match="duplicate.*destination"):
        RescuePlan(
            schema_version=1,
            capture="legacy-2026-08-31",
            releases=(
                _release(tag="v1.0.0", assets=(_asset(sha256="a" * 64),)),
                _release(tag="v1.0.0", assets=(_asset(sha256="b" * 64),)),
            ),
        )


def test_release_rejects_duplicate_destination_asset_names() -> None:
    """Removing asset uniqueness must not permit an upload overwrite ambiguity."""

    with pytest.raises(ValidationError, match="duplicate"):
        RescueRelease(
            repository="gelstable/gel",
            tag="v1.0.0",
            target_commitish=None,
            assets=(_asset(name="a"), _asset(name="a")),
        )


def test_plan_rejects_duplicate_expected_sha256_across_releases() -> None:
    """Duplicate replacement digests across releases must fail at plan boundary."""

    with pytest.raises(ValidationError, match="duplicate.*sha256"):
        RescuePlan(
            schema_version=1,
            capture="legacy-2026-08-31",
            releases=(
                _release(
                    repository="gelstable/gel",
                    tag="v1.0.0",
                    assets=(_asset(sha256="a" * 64),),
                ),
                _release(
                    repository="gelstable/gel",
                    tag="v2.0.0",
                    assets=(_asset(sha256="a" * 64),),
                ),
            ),
        )


def test_plan_rejects_noncanonical_ordering_and_json() -> None:
    """Relaxing canonical ordering or JSON checks must fail at the contract edge."""

    with pytest.raises(ValidationError, match="canonical order"):
        RescuePlan(
            schema_version=1,
            capture="legacy-2026-08-31",
            releases=(
                _release(
                    repository="gelstable/gel",
                    tag="v2.0.0",
                    assets=(_asset(sha256="2" * 64),),
                ),
                _release(
                    repository="gelstable/gel",
                    tag="v1.0.0",
                    assets=(_asset(sha256="1" * 64),),
                ),
            ),
        )

    valid_plan = RescuePlan(
        schema_version=1,
        capture="legacy-2026-08-31",
        releases=(_release(),),
    )
    from gel_registry.digest import canonical_json

    noncanonical = canonical_json(valid_plan) + b" "
    with pytest.raises(ValueError, match="canonical JSON"):
        RescuePlan.model_validate_json(noncanonical)


def test_contracts_require_sizes_and_safe_asset_names() -> None:
    """Dropping required byte metadata or filename checks must fail validation."""

    with pytest.raises(ValidationError):
        RescueAsset.model_validate(
            {
                "source_url": "https://packages.edgedb.com/archive/gel-cli",
                "destination_name": "gel-cli",
                "content_type": "application/octet-stream",
                "expected_sha256": "a" * 64,
            }
        )
    with pytest.raises(ValidationError, match="safe filename"):
        _asset(name="../gel-cli")
    for unsafe_name in ("gel\tcli", "gel\x1fcli"):
        with pytest.raises(ValidationError, match="safe filename"):
            _asset(name=unsafe_name)
