"""Fresh-render comparison and append-only history checks."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..render import RenderError, build_snapshot, render_schemas, select_snapshot
from .report import Collector
from .support import CAPTURE_REL, display_path, tree_files


def check_render_drift(repo: Path, collector: Collector) -> None:
    check = "render.drift"
    collector.begin(check)
    try:
        with tempfile.TemporaryDirectory(prefix=".gel-registry-validate-") as directory:
            stage = Path(directory) / "repo"
            _copy_render_inputs(repo, stage)
            try:
                build_snapshot(stage)
            except (RenderError, OSError, ValueError) as exc:
                collector.add(
                    check,
                    display_path(repo, repo / "public"),
                    f"fresh snapshot build failed: {exc}",
                )
            pointer_path = stage / "pointers" / "latest.json"
            if pointer_path.exists() and not pointer_path.is_symlink():
                try:
                    select_snapshot(stage)
                except (RenderError, OSError, ValueError) as exc:
                    collector.add(
                        check,
                        display_path(repo, repo / "public" / "registry.json"),
                        f"fresh snapshot selection failed: {exc}",
                    )
                    collector.add(
                        check,
                        display_path(repo, repo / "public" / "v1" / "snapshots.json"),
                        "fresh snapshot selection did not produce the moving pair",
                    )
            try:
                render_schemas(stage)
            except (RenderError, OSError, ValueError) as exc:
                collector.add(
                    check,
                    display_path(repo, repo / "public"),
                    f"fresh support render failed: {exc}",
                )
            _compare_tree_bytes(
                repo, stage / "public", repo / "public", check, collector
            )
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, repo), str(exc))


def check_history(
    repo: Path,
    base: Path,
    collector: Collector,
    roots: tuple[Path, ...] | None = None,
) -> None:
    check = "history.append_only"
    collector.begin(check)
    if not base.exists() or base.is_symlink() or not base.is_dir():
        collector.add(check, base, "merge-base tree is not a directory")
        return
    immutable_roots = roots or (
        CAPTURE_REL,
        Path("bootstrap"),
        Path("releases"),
        Path("public/s"),
    )
    for relative_root in immutable_roots:
        base_root = base / relative_root
        head_root = repo / relative_root
        try:
            base_files = tree_files(base_root)
            head_files = tree_files(head_root)
        except (OSError, ValueError) as exc:
            collector.add(check, relative_root, str(exc))
            continue
        for relative in sorted(base_files):
            path = relative_root / relative
            if relative not in head_files:
                collector.add(check, path, "immutable path was deleted")
            elif head_files[relative] != base_files[relative]:
                collector.add(check, path, "immutable path changed")


def _compare_tree_bytes(
    repo: Path,
    candidate: Path,
    committed: Path,
    check: str,
    collector: Collector,
) -> None:
    try:
        candidate_files = tree_files(candidate)
        committed_files = tree_files(committed)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, committed), str(exc))
        return
    for relative in sorted(set(candidate_files) - set(committed_files)):
        collector.add(
            check,
            display_path(repo, committed / relative),
            "rendered path is missing from the committed tree",
        )
    for relative in sorted(set(committed_files) - set(candidate_files)):
        collector.add(
            check,
            display_path(repo, committed / relative),
            "committed path is absent from a fresh render",
        )
    for relative in sorted(set(candidate_files) & set(committed_files)):
        if candidate_files[relative] != committed_files[relative]:
            collector.add(
                check, display_path(repo, committed / relative), "rendered bytes drift"
            )


def _copy_render_inputs(repo: Path, stage: Path) -> None:
    stage.mkdir(parents=True)
    for name in ("upstream", "bootstrap", "releases", "pointers"):
        source = repo / name
        destination = stage / name
        if source.is_symlink():
            destination.symlink_to(
                source.readlink(), target_is_directory=source.is_dir()
            )
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        elif source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    # Pinned snapshots are immutable render inputs.  Moving documents,
    # schemas, healthz, and any other public path must be recreated by the
    # fresh render so the comparison covers the complete public path set.
    pinned = repo / "public" / "s"
    if pinned.is_symlink():
        (stage / "public" / "s").symlink_to(
            pinned.readlink(), target_is_directory=pinned.is_dir()
        )
    elif pinned.is_dir():
        shutil.copytree(pinned, stage / "public" / "s", symlinks=True)
