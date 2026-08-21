"""Fresh-render comparison and append-only history checks."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..contracts import Pointer
from ..digest import canonical_json
from ..render import RenderError, build_snapshot, render_schemas, select_snapshot
from .report import Collector
from .support import CAPTURE_REL, display_path, read_file, tree_files


def check_render_drift(repo: Path, collector: Collector) -> None:
    """Re-render committed inputs and compare the complete public tree."""

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
                else:
                    _compare_file_bytes(
                        repo,
                        stage / "vercel.json",
                        repo / "vercel.json",
                        check,
                        collector,
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


def check_candidate_reproduction(
    repo: Path,
    collector: Collector,
    *,
    base: Path | None = None,
) -> None:
    """Rebuild generated candidate output from committed immutable inputs."""

    check = "candidate.reproduction"
    collector.begin(check)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".gel-registry-candidate-"
        ) as directory:
            stage = Path(directory) / "repo"
            pinned_source = None if base is None else Path(base) / "public"
            _copy_render_inputs(repo, stage, pinned_source=pinned_source)
            _remove_candidate_outputs(stage)
            snapshot = build_snapshot(stage)
            pointer_path = stage / "pointers" / "latest.json"
            pointer_path.parent.mkdir(parents=True, exist_ok=True)
            pointer_path.write_bytes(canonical_json(Pointer(snapshot=snapshot)))
            select_snapshot(stage)
            render_schemas(stage)
            _compare_tree_bytes(
                repo, stage / "public", repo / "public", check, collector
            )
            _compare_file_bytes(
                repo,
                pointer_path,
                repo / "pointers" / "latest.json",
                check,
                collector,
            )
            _compare_file_bytes(
                repo,
                stage / "vercel.json",
                repo / "vercel.json",
                check,
                collector,
            )
    except (OSError, RenderError, ValueError) as exc:
        collector.add(check, display_path(repo, repo), str(exc))


def check_history(
    repo: Path,
    base: Path,
    collector: Collector,
    roots: tuple[Path, ...] | None = None,
) -> None:
    """Reject changes to immutable capture, input, or pinned output bytes."""

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
        # The shared blob store is immutable history too: without this, a
        # rewritten index blob would pass the append-only gate.
        Path("public/i"),
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
    fresh: Path,
    committed: Path,
    check: str,
    collector: Collector,
) -> None:
    try:
        fresh_files = tree_files(fresh)
        committed_files = tree_files(committed)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, committed), str(exc))
        return
    for relative in sorted(set(fresh_files) - set(committed_files)):
        collector.add(
            check,
            display_path(repo, committed / relative),
            "rendered path is missing from the committed tree",
        )
    for relative in sorted(set(committed_files) - set(fresh_files)):
        collector.add(
            check,
            display_path(repo, committed / relative),
            "committed path is absent from a fresh render",
        )
    for relative in sorted(set(fresh_files) & set(committed_files)):
        if fresh_files[relative] != committed_files[relative]:
            collector.add(
                check, display_path(repo, committed / relative), "rendered bytes drift"
            )


def _compare_file_bytes(
    repo: Path,
    candidate: Path,
    committed: Path,
    check: str,
    collector: Collector,
) -> None:
    try:
        candidate_bytes = read_file(candidate)
        committed_bytes = read_file(committed)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, committed), str(exc))
        return
    if candidate_bytes != committed_bytes:
        collector.add(check, display_path(repo, committed), "reproduced bytes drift")


def _copy_render_inputs(
    repo: Path, stage: Path, *, pinned_source: Path | None = None
) -> None:
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

    # Pinned snapshots and the blob store they point into are immutable render
    # inputs: a fresh render reproduces only the selected snapshot's blobs, so
    # every earlier snapshot's would otherwise be missing. Moving documents,
    # schemas, healthz, and other public paths are recreated by the render.
    pinned_root = repo / "public" if pinned_source is None else Path(pinned_source)
    for name in ("s", "i"):
        pinned = pinned_root / name
        if pinned.is_symlink():
            (stage / "public").mkdir(parents=True, exist_ok=True)
            (stage / "public" / name).symlink_to(
                pinned.readlink(), target_is_directory=pinned.is_dir()
            )
        elif pinned.is_dir():
            shutil.copytree(pinned, stage / "public" / name, symlinks=True)


def _remove_candidate_outputs(stage: Path) -> None:
    """Remove only documents recreated by the candidate render pipeline."""

    for relative in (
        Path("pointers/latest.json"),
        Path("public/registry.json"),
        Path("public/v1/snapshots.json"),
        Path("public/v1/schema"),
        Path("public/healthz"),
    ):
        path = stage / relative
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
