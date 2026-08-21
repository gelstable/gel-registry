"""Algebraic invariants of the registry's byte-level primitives.

Everything the registry publishes is a pure function of committed immutable
inputs. That guarantee rests on a canonical encoding, a content digest, a
content-addressed snapshot identity, and a set of storage operations that
either install exact bytes or leave nothing behind. These are the properties
those primitives must hold for arbitrary inputs.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from gel_registry import storage
from gel_registry.contracts import InstallRef
from gel_registry.digest import (
    Digests,
    blob_id,
    canonical_json,
    hash_bytes,
    snapshot_id,
)

_EXAMPLE = itertools.count()

_FILESYSTEM = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.text(max_size=32),
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=8,
)
json_objects = st.dictionaries(st.text(max_size=8), json_values, max_size=6)
index_paths = st.builds(
    lambda name: PurePosixPath("index") / f"{name}.json",
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12),
)


def _shuffled(value: dict[str, Any], seed: int) -> dict[str, Any]:
    """Return an equal mapping whose insertion order differs deterministically."""

    items = sorted(value.items(), key=lambda item: hash((seed, item[0])))
    return dict(items)


@given(value=json_objects, seed=st.integers())
def test_canonical_json_is_an_idempotent_key_ordered_encoding(
    value: dict[str, Any], seed: int
) -> None:
    encoded = canonical_json(value)

    assert encoded.endswith(b"\n")
    assert not encoded.endswith(b"\n\n")
    assert canonical_json(_shuffled(value, seed)) == encoded
    assert canonical_json(json.loads(encoded)) == encoded
    assert json.loads(encoded) == value


@given(data=st.binary(max_size=512))
def test_hash_bytes_agrees_with_hashlib_and_reports_length(data: bytes) -> None:
    digests = hash_bytes(data)

    assert digests == Digests(
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        blake2b=hashlib.blake2b(data, digest_size=64).hexdigest(),
    )
    assert len(digests.sha256) == 64
    assert len(digests.blake2b) == 128


@given(
    indexes=st.dictionaries(
        index_paths, st.binary(max_size=64), min_size=1, max_size=6
    ),
    seed=st.integers(),
)
def test_snapshot_id_depends_only_on_the_set_of_index_bytes(
    indexes: dict[PurePosixPath, bytes], seed: int
) -> None:
    identity = snapshot_id(indexes)
    reordered = dict(
        sorted(indexes.items(), key=lambda item: hash((seed, str(item[0]))))
    )

    assert snapshot_id(reordered) == identity
    assert len(identity) == 16
    assert all(character in "0123456789abcdef" for character in identity)

    lines = sorted(
        f"{path} {hashlib.sha256(data).hexdigest()}" for path, data in indexes.items()
    )
    manifest = "\n".join(lines).encode("utf-8")
    assert identity == hashlib.sha256(manifest).digest()[:8].hex()

    for unsafe in (
        "index/../packages.json",
        "/index/packages.json",
        "packages.json",
        "index/pack\nages.json",
    ):
        with pytest.raises(ValueError):
            snapshot_id({PurePosixPath(unsafe): b"payload"})


@given(data=st.binary(max_size=64), other=st.binary(max_size=64))
def test_a_blob_identity_is_a_pure_function_of_its_bytes(
    data: bytes, other: bytes
) -> None:
    """A blob's name is derived from nothing but the blob.

    It is 128 bits rather than the snapshot identity's 64 because the blob
    namespace accumulates entries indefinitely, while snapshot IDs are one per
    promotion.
    """

    identity = blob_id(data)

    assert identity == hashlib.sha256(data).hexdigest()[:32]
    assert len(identity) == 32
    assert all(character in "0123456789abcdef" for character in identity)
    assert blob_id(data) == identity
    assert (blob_id(other) == identity) == (other == data)


@given(first=st.binary(max_size=32), second=st.binary(max_size=32))
@_FILESYSTEM
def test_atomic_installation_writes_exact_bytes_or_nothing(
    tmp_path: Path, first: bytes, second: bytes
) -> None:
    root = tmp_path / f"install-{next(_EXAMPLE)}"
    created = storage.ensure_directory_chain(root / "s" / "abc")
    assert created == (root, root / "s", root / "s" / "abc")
    assert storage.ensure_directory_chain(root) == ()

    immutable = root / "immutable.json"
    assert storage.create_atomic(immutable, first) is True
    assert storage.create_atomic(immutable, first) is False
    assert immutable.read_bytes() == first
    if second != first:
        with pytest.raises(ValueError, match="immutable path collision"):
            storage.create_atomic(immutable, second)
        assert immutable.read_bytes() == first

    mutable = root / "mutable.json"
    storage.replace_atomic(mutable, first)
    storage.replace_atomic(mutable, second)
    assert mutable.read_bytes() == second
    assert sorted(path.name for path in root.iterdir()) == [
        "immutable.json",
        "mutable.json",
        "s",
    ]

    source = root / "source"
    source.mkdir()
    with pytest.raises(OSError):
        storage.rename_noreplace(source, root / "s")
    assert source.is_dir()
    storage.rename_noreplace(source, root / "moved")
    assert (root / "moved").is_dir()


def test_reading_a_tree_rejects_symlinks_and_non_directories(tmp_path: Path) -> None:
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

    document = tmp_path / "registry.json"
    document.write_bytes(b"{}")
    with pytest.raises(ValueError, match="is not a directory"):
        storage.read_tree(document)

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "public").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked ancestor"):
        storage.ensure_directory_chain(tmp_path / "public" / "s")
    assert not tuple(outside.iterdir())


@given(
    ref=st.sampled_from(
        [
            "http://packages.geldata.com/archive/example",
            "https://packages.geldata.com/archive/example#fragment",
            "https://user:password@packages.geldata.com/archive/example",
            "ftp://packages.geldata.com/archive/example",
            "https://packages.geldata.com:8443/archive/example",
        ]
    )
)
def test_installref_rejects_every_unsafe_reference(ref: str) -> None:
    with pytest.raises(ValidationError):
        InstallRef(
            ref=ref,
            type="application/x-pie-executable",
            encoding="identity",
            verification={"size": 3, "sha256": "a" * 64, "blake2b": "b" * 128},
        )
