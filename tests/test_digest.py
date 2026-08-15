from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import pytest
from pydantic import BaseModel

from gel_registry.digest import (
    Digests,
    canonical_json,
    hash_bytes,
    hash_file,
    snapshot_id,
)


def test_canonical_json_sorts_keys_and_uses_one_trailing_lf() -> None:
    assert canonical_json({"z": 1, "a": [2]}) == (
        b'{\n  "a": [\n    2\n  ],\n  "z": 1\n}\n'
    )


def test_canonical_json_serializes_pydantic_models_in_json_mode() -> None:
    class Payload(BaseModel):
        z: int
        a: list[int]

    assert canonical_json(Payload(z=1, a=[2])) == (
        b'{\n  "a": [\n    2\n  ],\n  "z": 1\n}\n'
    )


def test_hash_bytes_returns_standard_sha256_and_blake2b_vectors() -> None:
    assert hash_bytes(b"abc") == Digests(
        size=3,
        sha256="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        blake2b=(
            "ba80a53f981c4d0d6a2797b69f12f6e94c212f14685ac4b74b12bb6fdbffa2d17d87c5392aab792dc252d5de4533cc9518d38aa8dbf1925ab92386edd4009923"
        ),
    )


def test_hash_file_streams_bytes_and_returns_size_and_digests(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"abc")

    assert hash_file(path) == hash_bytes(b"abc")


def test_snapshot_id_hashes_sorted_index_manifest_lines() -> None:
    indexes = {
        PurePosixPath("index/z.json"): b"z",
        PurePosixPath("index/a.json"): b"a",
    }
    lines = sorted(
        f"{path} {hashlib.sha256(data).hexdigest()}" for path, data in indexes.items()
    )
    manifest = "\n".join(lines).encode("utf-8")

    assert snapshot_id(indexes) == hashlib.sha256(manifest).digest()[:8].hex()
    assert snapshot_id(dict(reversed(list(indexes.items())))) == snapshot_id(indexes)


def test_snapshot_id_hashes_empty_manifest() -> None:
    assert snapshot_id({}) == hashlib.sha256(b"").digest()[:8].hex()


@pytest.mark.parametrize(
    "path",
    [
        PurePosixPath("packages.json"),
        PurePosixPath("index"),
        PurePosixPath("/index/packages.json"),
        PurePosixPath("index/../packages.json"),
        PurePosixPath("index/packages\n.json"),
    ],
)
def test_snapshot_id_rejects_unsafe_index_paths(path: PurePosixPath) -> None:
    with pytest.raises(ValueError):
        snapshot_id({path: b"payload"})
