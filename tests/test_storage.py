from __future__ import annotations

from pathlib import Path

import pytest

from gel_registry import storage


def test_rename_noreplace_refuses_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(OSError):
        storage.rename_noreplace(source, destination)

    assert source.is_dir()


def test_replace_atomic_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "document.json"
    storage.replace_atomic(path, b"first")
    storage.replace_atomic(path, b"second")

    assert path.read_bytes() == b"second"
    assert [item.name for item in tmp_path.iterdir()] == ["document.json"]


def test_create_atomic_reports_identical_bytes_as_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "immutable.json"

    assert storage.create_atomic(path, b"bytes") is True
    assert storage.create_atomic(path, b"bytes") is False

    with pytest.raises(ValueError, match="immutable path collision"):
        storage.create_atomic(path, b"different")


def test_ensure_directory_chain_reports_only_created_directories(
    tmp_path: Path,
) -> None:
    created = storage.ensure_directory_chain(tmp_path / "public" / "s" / "abc")

    assert created == (
        tmp_path / "public",
        tmp_path / "public" / "s",
        tmp_path / "public" / "s" / "abc",
    )
    assert storage.ensure_directory_chain(tmp_path / "public") == ()


def test_ensure_directory_chain_rejects_a_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "public").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked ancestor"):
        storage.ensure_directory_chain(tmp_path / "public" / "s")

    assert not tuple(outside.iterdir())


def test_read_tree_records_directories_and_rejects_symlinks(tmp_path: Path) -> None:
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "stable.json").write_bytes(b"{}")

    assert storage.read_tree(tmp_path) == {
        "index": None,
        "index/stable.json": b"{}",
    }
    assert storage.read_files(tmp_path) == {"index/stable.json": b"{}"}

    (tmp_path / "index" / "alias.json").symlink_to(tmp_path / "index" / "stable.json")

    with pytest.raises(ValueError, match="contains symlink"):
        storage.read_tree(tmp_path)


def test_read_tree_rejects_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_bytes(b"{}")

    with pytest.raises(ValueError, match="is not a directory"):
        storage.read_tree(path)
