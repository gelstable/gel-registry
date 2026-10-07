"""Read committed native inputs without following filesystem links."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def input_path(repo: Path, name: str) -> Path:
    """Check an optional native input before reading or replacing it."""
    path = repo / "native" / name
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise ValueError(f"symlink in native input: {parent}")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"native input parent is not a directory: {parent}")
    if path.is_symlink():
        raise ValueError(f"symlink in native input: {path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"native input is not a regular file: {path}")
    return path


def load_yanked(repo: Path) -> set[str]:
    """Load strictly formed artifact digests from the optional yank list."""
    path = input_path(repo, "yanked.json")
    entries = json.loads(path.read_bytes()) if path.exists() else []
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict)
        or set(entry) != {"sha256", "reason"}
        or not isinstance(entry["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
        or not isinstance(entry["reason"], str)
        for entry in entries
    ):
        raise ValueError("invalid native yank list")
    return {entry["sha256"] for entry in entries}


def metadata_inventory(out: Path) -> dict[str, str]:
    files = {}
    for fmt in ("apt", "rpm"):
        root = out / fmt
        if root.is_symlink():
            raise ValueError("symlink in native output")
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"symlink in native output: {path}")
            if path.is_file() and path.name not in {
                "InRelease",
                "Release.gpg",
                "repomd.xml.asc",
            }:
                files[str(path.relative_to(out))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return files
