"""Filesystem access for the renderer, reported in ``RenderError`` terms.

Every function here delegates its mechanics to :mod:`gel_registry.storage` and
adds the label a renderer message needs, so the renderer never carries a second
copy of the atomicity rules.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from .. import storage
from .errors import RenderError


def regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RenderError(f"{label} is not a regular file: {path}")


def directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RenderError(f"{label} is not a directory: {path}")


def ensure_directory_chain(path: Path, label: str) -> None:
    """Create a directory, rejecting symlinked or non-directory ancestors."""

    try:
        storage.ensure_directory_chain(path)
    except ValueError as exc:
        raise RenderError(f"{label} {exc}") from exc
    except OSError as exc:
        raise RenderError(f"could not create {label} directory {path}: {exc}") from exc


def read_model[ModelT: BaseModel](
    path: Path, model: type[ModelT], label: str
) -> ModelT:
    regular_file(path, label)
    try:
        return model.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise RenderError(f"invalid {label} {path}: {exc}") from exc


def read_bytes(path: Path, label: str) -> bytes:
    regular_file(path, label)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RenderError(f"could not read {label} {path}: {exc}") from exc


def json_files(root: Path, label: str) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    directory(root, label)
    files: list[Path] = []
    try:
        paths = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
        for path in paths:
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                raise RenderError(f"{label} contains symlink: {relative}")
            if path.is_file() and path.suffix == ".json":
                files.append(path)
    except OSError as exc:
        raise RenderError(f"could not inspect {label} {root}: {exc}") from exc
    return tuple(files)


def read_tree(root: Path, label: str = "tree") -> dict[str, bytes | None]:
    try:
        return storage.read_tree(root)
    except ValueError as exc:
        raise RenderError(f"{label} {exc}") from exc
    except OSError as exc:
        raise RenderError(f"could not inspect {label} {root}: {exc}") from exc


def atomic_replace(path: Path, data: bytes, label: str) -> None:
    """Replace one regular file atomically, creating missing parents."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RenderError(f"{label} is not a regular file: {path}")
    ensure_directory_chain(path.parent, f"{label} parent")
    try:
        storage.replace_atomic(path, data)
    except OSError as exc:
        raise RenderError(f"could not atomically write {label} {path}: {exc}") from exc


__all__ = [
    "atomic_replace",
    "directory",
    "ensure_directory_chain",
    "json_files",
    "read_bytes",
    "read_model",
    "read_tree",
    "regular_file",
]
