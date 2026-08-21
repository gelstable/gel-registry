"""Reading and formatting helpers shared by every validation layer."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from .. import storage
from ..digest import canonical_json

CAPTURE_REL = Path("upstream/packages.geldata.com/legacy-2026-08-bootstrap")


def capture_root(repo: Path) -> Path:
    return repo / CAPTURE_REL


def display_path(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()


def read_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("is not a regular file")
    return path.read_bytes()


def tree_files(root: Path) -> dict[str, bytes]:
    """Read a regular-file tree in deterministic relative-path order."""

    if not root.exists():
        return {}
    return storage.read_files(root)


def canonical_error(raw: bytes, value: BaseModel) -> str | None:
    expected = canonical_json(value)
    if raw != expected:
        return "is not canonical JSON"
    return None
