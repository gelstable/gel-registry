from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import copy_release, write_pointer

from gel_registry.render import RenderError, build_snapshot, select_snapshot


def test_selection_is_pointer_driven_and_rollback_preserves_pinned_trees(
    tmp_path: Path,
) -> None:
    copy_release(tmp_path, "1.0.0")
    old = build_snapshot(tmp_path)
    (tmp_path / "releases" / "gel-cli" / "1.0.0.json").unlink()
    copy_release(tmp_path, "2.0.0")
    new = build_snapshot(tmp_path)
    assert old != new

    pinned_before = {
        path.relative_to(tmp_path / "public" / "s"): path.read_bytes()
        for path in (tmp_path / "public" / "s").rglob("*")
        if path.is_file()
    }
    write_pointer(tmp_path, old)
    select_snapshot(tmp_path)
    first_root = (tmp_path / "public" / "registry.json").read_bytes()
    first_listing = (tmp_path / "public" / "v1" / "snapshots.json").read_bytes()
    assert all(
        entry["url"].startswith(f"s/{old}/index/")
        for entry in json.loads(first_root)["indexes"]
    )
    assert json.loads(first_listing)["latest"] == old
    assert set(json.loads(first_listing)["snapshots"]) == {old, new}

    write_pointer(tmp_path, new)
    select_snapshot(tmp_path)
    second_root = (tmp_path / "public" / "registry.json").read_bytes()
    second_listing = (tmp_path / "public" / "v1" / "snapshots.json").read_bytes()
    assert first_root != second_root
    assert first_listing != second_listing
    assert json.loads(second_listing)["latest"] == new
    pinned_after = {
        path.relative_to(tmp_path / "public" / "s"): path.read_bytes()
        for path in (tmp_path / "public" / "s").rglob("*")
        if path.is_file()
    }
    assert pinned_after == pinned_before


def test_selection_rejects_missing_selected_snapshot(tmp_path: Path) -> None:
    pointer = tmp_path / "pointers" / "latest.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_bytes(b'{"snapshot":"0000000000000000"}')

    with pytest.raises(RenderError):
        select_snapshot(tmp_path)
