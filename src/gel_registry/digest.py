"""Canonical JSON serialization and content identity helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Digests:
    """Byte count and canonical lowercase content digests."""

    size: int
    sha256: str
    blake2b: str


def canonical_json(value: BaseModel | Mapping[str, Any]) -> bytes:
    """Serialize a model or mapping using the registry's canonical JSON bytes."""

    serializable: Any = (
        value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    )
    text = json.dumps(
        serializable,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"{text}\n".encode()


def hash_bytes(data: bytes) -> Digests:
    """Return the size, SHA-256, and BLAKE2b-512 digests for bytes."""

    sha256 = hashlib.sha256(data)
    blake2b = hashlib.blake2b(data, digest_size=64)
    return Digests(
        size=len(data),
        sha256=sha256.hexdigest(),
        blake2b=blake2b.hexdigest(),
    )


def hash_file(path: Path) -> Digests:
    """Read a file in fixed-size binary chunks and return its digests."""

    sha256 = hashlib.sha256()
    blake2b = hashlib.blake2b(digest_size=64)
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_SIZE):
            size += len(chunk)
            sha256.update(chunk)
            blake2b.update(chunk)
    return Digests(
        size=size,
        sha256=sha256.hexdigest(),
        blake2b=blake2b.hexdigest(),
    )


def _validated_index_path(path: PurePosixPath) -> str:
    path_text = str(path)
    if "\n" in path_text or "\r" in path_text:
        raise ValueError("snapshot index paths must not contain newlines")
    if path.is_absolute() or len(path.parts) < 2 or path.parts[0] != "index":
        raise ValueError("snapshot index paths must be relative to index/")
    if ".." in path.parts:
        raise ValueError("snapshot index paths must not traverse parent directories")
    return path_text


def snapshot_id(indexes: Mapping[PurePosixPath, bytes]) -> str:
    """Return the short SHA-256 identity of a set of canonical index bytes."""

    lines = [
        f"{_validated_index_path(path)} {hashlib.sha256(data).hexdigest()}"
        for path, data in indexes.items()
    ]
    manifest = "\n".join(sorted(lines)).encode("utf-8")
    return hashlib.sha256(manifest).digest()[:8].hex()


__all__ = [
    "Digests",
    "canonical_json",
    "hash_bytes",
    "hash_file",
    "snapshot_id",
]
