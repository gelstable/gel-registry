"""Narrow, deterministic contracts and operations for legacy artifact rescue."""

from .indexes import (
    RESCUE_ORIGIN,
    RescueCaptureManifest,
    capture_rescue_indexes,
    rescue_capture_root,
)
from .models import (
    RescueAbsentIndex,
    RescueAsset,
    RescueIndexCapture,
    RescuePlan,
    RescueRelease,
)
from .selection import (
    GitHubTagSource,
    GitTagSource,
    Product,
    TagSource,
    UpstreamTag,
    artifact_name,
    plan_rescue,
)
from .transfer import (
    RescueSourceIntegrityError,
    RescueTransferError,
    RescueUploadIntegrityError,
    transfer_asset,
    transfer_original_asset,
    verify_uploaded_asset,
)

__all__ = [
    "GitHubTagSource",
    "GitTagSource",
    "Product",
    "RESCUE_ORIGIN",
    "RescueAbsentIndex",
    "RescueAsset",
    "RescueCaptureManifest",
    "RescueIndexCapture",
    "RescuePlan",
    "RescueRelease",
    "RescueSourceIntegrityError",
    "RescueTransferError",
    "RescueUploadIntegrityError",
    "TagSource",
    "UpstreamTag",
    "artifact_name",
    "capture_rescue_indexes",
    "plan_rescue",
    "rescue_capture_root",
    "transfer_asset",
    "transfer_original_asset",
    "verify_uploaded_asset",
]
