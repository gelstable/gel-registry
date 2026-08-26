"""Shared filesystem primitives for reading and installing registry trees.

Every operation here is deliberately free of domain vocabulary and raises only
``OSError`` or ``ValueError``.  Callers wrap those into ``RenderError``,
``PromotionError``, ``NormalizationError`` or a validation report entry so one
atomicity implementation can serve normalization, rendering, promotion and
validation without any layer importing another layer's private helpers.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
import tempfile
from pathlib import Path

_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_AT_FDCWD = -100


def rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a path without replacing an existing destination."""

    source_bytes = _encoded_path(source)
    destination_bytes = _encoded_path(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            renamex_np = libc.renamex_np
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "renamex_np is unavailable") from exc
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, destination_bytes, _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable") from exc
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def write_temp(directory: Path, prefix: str, data: bytes) -> Path:
    """Write bytes to a new durable temporary file beside their destination."""

    fd, name = tempfile.mkstemp(prefix=prefix, dir=str(directory))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def replace_atomic(path: Path, data: bytes) -> None:
    """Replace one regular file with new bytes atomically."""

    temporary = write_temp(path.parent, f".{path.name}.", data)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _holds_exactly(path: Path, data: bytes) -> bool:
    """Report whether ``path`` is a regular file holding exactly ``data``.

    ``Path.is_file()`` follows symlinks, so a symlink pointing at matching
    bytes would otherwise be accepted as an unchanged immutable file and left
    in a tree the rest of this module guarantees is symlink-free.  The check is
    made with ``lstat`` so the link itself, not its target, decides.
    """

    try:
        status = os.lstat(path)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(status.st_mode):
        return False
    try:
        return path.read_bytes() == data
    except OSError:
        return False


def create_atomic(path: Path, data: bytes) -> bool:
    """Create a file without replacing a raced or preexisting destination.

    Returns whether the file was created.  A destination that already holds
    exactly these bytes is reported as unchanged rather than as a collision, so
    an interrupted publication can be replayed.  A symlink is never unchanged,
    however closely its target matches.
    """

    if path.is_symlink() or path.exists():
        if _holds_exactly(path, data):
            return False
        raise ValueError(f"immutable path collision: {path}")
    temporary = write_temp(path.parent, f".{path.name}.", data)
    try:
        rename_noreplace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        if exc.errno == errno.EEXIST and _holds_exactly(path, data):
            return False
        raise
    return True


def ensure_directory_chain(path: Path) -> tuple[Path, ...]:
    """Create a directory and reject symlinked or non-directory ancestors.

    ``Path.mkdir(parents=True)`` follows an existing symlink in the chain, so
    every ancestor is checked before a write is attempted.  The directories
    that were actually created are returned in creation order, which lets a
    failed transaction roll its own empty directories back.
    """

    missing: list[Path] = []
    current = path
    while True:
        if current.is_symlink():
            raise ValueError(f"contains symlinked ancestor: {current}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"ancestor is not a directory: {current}")
            break
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    created: list[Path] = []
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(
                    f"ancestor is not a regular directory: {directory}"
                ) from None
        else:
            created.append(directory)
    return tuple(created)


def read_tree(root: Path) -> dict[str, bytes | None]:
    """Read a regular-file tree in relative-path order, rejecting symlinks.

    Directories are recorded with a ``None`` value so a comparison covers the
    complete path set rather than only the files.
    """

    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"is not a directory: {root}")
    entries: dict[str, bytes | None] = {}
    paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"contains symlink: {relative}")
        if path.is_dir():
            entries[relative] = None
        elif path.is_file():
            entries[relative] = path.read_bytes()
        else:
            raise ValueError(f"contains unsupported path: {relative}")
    return entries


def read_files(root: Path) -> dict[str, bytes]:
    """Read only the regular files of a tree, rejecting symlinks."""

    return {
        relative: data for relative, data in read_tree(root).items() if data is not None
    }


def _encoded_path(path: Path) -> bytes:
    encoded = os.fsencode(str(path))
    if b"\x00" in encoded:
        raise ValueError("path contains an embedded NUL byte")
    return encoded


__all__ = [
    "create_atomic",
    "ensure_directory_chain",
    "read_files",
    "read_tree",
    "rename_noreplace",
    "replace_atomic",
    "write_temp",
]
