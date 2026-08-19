"""Failure-safe transactions for bootstrap publication and release promotion.

The renderer intentionally exposes two independent phases: building an
immutable snapshot and selecting that snapshot through the internal pointer.
Promotion composes those phases in a temporary repository tree.  Nothing in
the caller's tree is changed until both phases, plus all immutable-byte checks,
have succeeded.
"""

from __future__ import annotations

from .operations import promote_release, publish_bootstrap
from .result import PromotionError, PromotionResult

__all__ = [
    "PromotionError",
    "PromotionResult",
    "promote_release",
    "publish_bootstrap",
]
