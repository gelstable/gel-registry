"""Candidate-specific orchestration over the neutral publication primitives."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from .. import publication
from ..contracts import BlockedRelease, ReleaseRecord
from ..render import RenderError, build_snapshot, select_snapshot
from .result import PromotionError, PromotionResult
from .staging import (
    _SOURCE_ROOTS,
    _bootstrap_indexes,
    _canonical_pointer,
    _ensure_nonempty_snapshot,
    _prepare_blocked,
    _prepare_release,
    _stage_repository,
    _validate_repo,
    _write_pointer,
)

_MUTABLE_PATHS = publication._MUTABLE_PATHS | {"promotion/blocked.json"}
_MUTABLE_ORDER = ("promotion/blocked.json", *publication._MOVING_PATHS)
_REMOVABLE_PATHS = frozenset({"promotion/blocked.json"})


def _transaction(
    repo: Path,
    *,
    releases: Iterable[ReleaseRecord],
    blocked: tuple[BlockedRelease, ...],
    branch: str,
) -> PromotionResult:
    repo = Path(repo)
    releases = tuple(releases)
    _validate_repo(repo)
    _canonical_pointer(repo / "pointers" / "latest.json")
    _bootstrap_indexes(repo / "bootstrap")

    stage = _stage_repository(repo)
    release_paths = frozenset(
        f"releases/{release.product}/{release.version}.json" for release in releases
    )
    try:
        staged_bootstrap_indexes = _bootstrap_indexes(stage / "bootstrap")
        for release in releases:
            _prepare_release(stage, release)
        _prepare_blocked(stage, blocked)
        snapshot = build_snapshot(stage)
        _ensure_nonempty_snapshot(
            stage,
            snapshot,
            bootstrap_indexes=staged_bootstrap_indexes,
            require_bootstrap_packages=not releases,
        )
        _write_pointer(stage, snapshot)
        select_snapshot(stage)
        changed_paths = publication._compare_transaction(
            repo,
            stage,
            snapshot=snapshot,
            source_roots=_SOURCE_ROOTS,
            additional_paths=release_paths,
            removable_paths=_REMOVABLE_PATHS,
            mutable_paths=_MUTABLE_PATHS,
        )
        publication._install_transaction(
            repo,
            stage,
            changed_paths,
            mutable_paths=_MUTABLE_PATHS,
            mutable_order=_MUTABLE_ORDER,
        )
        return PromotionResult(
            snapshot=snapshot,
            branch=branch,
            changed_paths=changed_paths,
        )
    except PromotionError:
        raise
    except publication.PublicationError as exc:
        raise PromotionError(str(exc)) from exc
    except RenderError:
        raise
    except (OSError, ValidationError, ValueError) as exc:
        raise PromotionError(f"promotion transaction failed: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


__all__ = ["_transaction"]
