from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from gel_registry.normalize import normalize_capture
from gel_registry.publication import publish_bootstrap
from gel_registry.render import render_schemas
from gel_registry.storage import read_tree

REPOSITORY_ROOT = Path(__file__).parents[1]


def _committed_source() -> Path:
    if not (REPOSITORY_ROOT / "upstream").is_dir():
        pytest.skip("Task 5 committed registry inputs are not present yet")
    for name in ("bootstrap", "pointers", "public"):
        if not (REPOSITORY_ROOT / name).is_dir():
            pytest.fail(f"committed registry output is incomplete: {name}/")
    return REPOSITORY_ROOT


def test_committed_legacy_inputs_reproduce_every_generated_byte() -> None:
    with tempfile.TemporaryDirectory(prefix="gel-registry-reproduction-") as directory:
        root = Path(directory)
        repo = _committed_source()
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

        assert reproduced_snapshot == "0f776b71381237c8"
        assert read_tree(reproduced / "bootstrap") == read_tree(repo / "bootstrap")
        assert read_tree(reproduced / "pointers") == read_tree(repo / "pointers")
        assert read_tree(reproduced / "public") == read_tree(repo / "public")
