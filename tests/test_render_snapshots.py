from __future__ import annotations

from pathlib import Path

import pytest
from support import copy_release

from gel_registry.render import RenderError, build_snapshot


def test_build_refuses_to_overwrite_mutated_existing_snapshot(tmp_path: Path) -> None:
    copy_release(tmp_path)
    snapshot = build_snapshot(tmp_path)
    target = next((tmp_path / "public" / "s" / snapshot).rglob("*.json"))
    target.write_bytes(target.read_bytes() + b"\n")

    with pytest.raises(RenderError, match="immutable|drift|mismatch"):
        build_snapshot(tmp_path)


def test_build_rejects_symlinked_snapshot_parent_before_installation(
    tmp_path: Path,
) -> None:
    copy_release(tmp_path)
    public = tmp_path / "public"
    public.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (public / "s").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RenderError, match="snapshot"):
        build_snapshot(tmp_path)

    assert not tuple(outside.iterdir())
