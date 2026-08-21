"""Atomic promotion of verified release candidates."""

from __future__ import annotations

from .operations import promote_candidate, publish_bootstrap
from .result import PromotionError, PromotionResult

__all__ = [
    "PromotionError",
    "PromotionResult",
    "promote_candidate",
    "publish_bootstrap",
]
