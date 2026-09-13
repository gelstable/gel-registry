from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from support import copy_release

from gel_registry.contracts import RootManifest
from gel_registry.normalize import normalize_capture
from gel_registry.publication import publish_bootstrap
from gel_registry.render import render_schemas
from gel_registry.storage import read_tree

REPOSITORY_ROOT = Path(__file__).parents[1]
BOOTSTRAP_SNAPSHOT = "0f776b71381237c8"


def _committed_source() -> Path:
    if not (REPOSITORY_ROOT / "upstream").is_dir():
        pytest.skip("Task 5 committed registry inputs are not present yet")
    for name in ("bootstrap", "pointers", "public"):
        if not (REPOSITORY_ROOT / name).is_dir():
            pytest.fail(f"committed registry output is incomplete: {name}/")
    return REPOSITORY_ROOT


def _snapshot_artifacts(repo: Path, snapshot: str) -> dict[str, bytes]:
    """Return the pinned manifest and blobs that form one snapshot."""
    root = repo / "public" / "s" / snapshot / "registry.json"
    manifest = RootManifest.model_validate_json(root.read_bytes())
    artifacts = {f"s/{snapshot}/registry.json": root.read_bytes()}
    for index in manifest.indexes:
        blob = repo / "public" / "i" / Path(index.ref).name
        artifacts[f"i/{blob.name}"] = blob.read_bytes()
    return artifacts


def _manifest_blob_mapping(root: Path) -> set[tuple[str, str, str]]:
    """Map document-relative index URLs to their shared blob identity."""
    manifest = RootManifest.model_validate_json(root.read_bytes())
    return {
        (item.channel, item.platform, Path(item.ref).name) for item in manifest.indexes
    }


def _assert_bootstrap_reproduction(repo: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="gel-registry-reproduction-") as directory:
        root = Path(directory)
        reproduced = root / "reproduced"
        reproduced.mkdir()
        shutil.copytree(repo / "upstream", reproduced / "upstream", symlinks=True)

        capture_root = (
            reproduced
            / "upstream"
            / "packages.geldata.com"
            / "legacy-2026-08-bootstrap"
        )
        normalize_capture(capture_root, reproduced / "bootstrap")
        render_schemas(reproduced, allow_release_record_migration=True)
        result = publish_bootstrap(reproduced)
        reproduced_snapshot = result.snapshot

        assert reproduced_snapshot == BOOTSTRAP_SNAPSHOT
        assert read_tree(reproduced / "bootstrap") == read_tree(repo / "bootstrap")
        assert _snapshot_artifacts(
            reproduced, BOOTSTRAP_SNAPSHOT
        ) == _snapshot_artifacts(repo, BOOTSTRAP_SNAPSHOT)


def test_committed_legacy_inputs_reproduce_immutable_bootstrap_outputs() -> None:
    _assert_bootstrap_reproduction(_committed_source())


def test_bootstrap_reproduction_survives_a_valid_later_promotion() -> None:
    """Initial inputs reproduce their snapshot after the selected one advances."""
    with tempfile.TemporaryDirectory(prefix="gel-registry-promoted-") as directory:
        promoted = Path(directory) / "repo"
        repo = _committed_source()
        for name in ("upstream", "bootstrap", "pointers", "public"):
            shutil.copytree(repo / name, promoted / name, symlinks=True)

        copy_release(promoted)
        promoted_snapshot = publish_bootstrap(promoted).snapshot

        assert promoted_snapshot != BOOTSTRAP_SNAPSHOT
        assert json.loads((promoted / "pointers" / "latest.json").read_bytes()) == {
            "snapshot": promoted_snapshot
        }
        assert (
            json.loads((promoted / "public" / "v1" / "snapshots.json").read_bytes())[
                "latest"
            ]
            == promoted_snapshot
        )
        assert _manifest_blob_mapping(
            promoted / "public" / "registry.json"
        ) == _manifest_blob_mapping(
            promoted / "public" / "s" / promoted_snapshot / "registry.json"
        )
        _assert_bootstrap_reproduction(promoted)


@pytest.mark.parametrize(
    "target",
    ("bootstrap", "snapshot", "blob"),
)
def test_bootstrap_reproduction_rejects_immutable_history_drift(
    target: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="gel-registry-drift-") as directory:
        repo = Path(directory) / "repo"
        source = _committed_source()
        for name in ("upstream", "bootstrap", "public"):
            shutil.copytree(source / name, repo / name, symlinks=True)

        if target == "bootstrap":
            path = repo / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
        elif target == "snapshot":
            path = repo / "public" / "s" / BOOTSTRAP_SNAPSHOT / "registry.json"
        else:
            manifest = RootManifest.model_validate_json(
                (
                    repo / "public" / "s" / BOOTSTRAP_SNAPSHOT / "registry.json"
                ).read_bytes()
            )
            path = repo / "public" / "i" / Path(manifest.indexes[0].ref).name
        path.write_bytes(path.read_bytes() + b"\n")

        with pytest.raises(AssertionError):
            _assert_bootstrap_reproduction(repo)
