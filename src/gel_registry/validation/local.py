"""Offline integrity gates over committed registry bytes.

The local validator is deliberately a consumer of committed bytes.  It does
not create an HTTP client and it never asks Git or a remote host for history.
"""

from __future__ import annotations

from pathlib import Path

from ..contracts import CaptureManifest
from ..normalize import NormalizationError, validate_normalization
from .drift import check_history, check_render_drift
from .inputs import (
    check_bootstrap,
    check_capture_bodies,
    check_releases,
    check_schemas,
    load_capture,
)
from .publication import check_pointer_and_snapshots
from .report import Collector, ValidationReport
from .support import CAPTURE_REL, capture_root, display_path


def validate_capture_local(repo: Path, base: Path | None = None) -> ValidationReport:
    """Validate a capture PR before any publication state exists.

    Capture pull requests intentionally do not build a snapshot or select a
    pointer.  This gate therefore checks only the immutable capture evidence,
    normalized bootstrap files, and their append-only history.
    """

    repository = Path(repo)
    collector = Collector()
    if repository.is_symlink() or not repository.is_dir():
        collector.add("repository", repository, "repository is not a directory")
        return collector.report()

    manifest = load_capture(repository, collector)
    check_capture_bodies(repository, manifest, collector)
    check_bootstrap(repository, manifest, collector)

    _check_normalization(repository, manifest, collector)

    if base is not None:
        check_history(
            repository,
            Path(base),
            collector,
            roots=(CAPTURE_REL, Path("bootstrap")),
        )
    return collector.report()


def validate_local(repo: Path, base: Path | None = None) -> ValidationReport:
    """Run all deterministic integrity, history, and render checks locally."""

    repository = Path(repo)
    collector = Collector()
    if repository.is_symlink() or not repository.is_dir():
        collector.add("repository", repository, "repository is not a directory")
        return collector.report()

    manifest = load_capture(repository, collector)
    check_capture_bodies(repository, manifest, collector)
    check_bootstrap(repository, manifest, collector)
    check_releases(repository, collector)
    check_schemas(repository, collector)
    check_pointer_and_snapshots(repository, collector)

    _check_normalization(repository, manifest, collector)

    check_render_drift(repository, collector)
    if base is not None:
        check_history(repository, Path(base), collector)
    return collector.report()


def _check_normalization(
    repo: Path,
    manifest: CaptureManifest | None,
    collector: Collector,
) -> None:
    """Compare committed bootstrap bytes against a fresh normalization."""

    check = "normalization.drift"
    collector.begin(check)
    bootstrap_root = repo / "bootstrap"
    if manifest is None or not bootstrap_root.exists():
        collector.add(
            check,
            display_path(repo, bootstrap_root),
            "normalization inputs are missing",
        )
        return
    try:
        validate_normalization(capture_root(repo), bootstrap_root)
    except (NormalizationError, OSError, ValueError) as exc:
        collector.add(check, display_path(repo, bootstrap_root), str(exc))
