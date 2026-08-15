from __future__ import annotations

from collections.abc import Iterator

import pytest

from gel_registry.constants import ORIGIN


@pytest.fixture
def artifact_url() -> str:
    return f"{ORIGIN}/archive/x86_64-unknown-linux-musl/example.tar.zst"


@pytest.fixture
def verification_data() -> dict[str, object]:
    return {
        "size": 3,
        "sha256": "a" * 64,
        "blake2b": "b" * 128,
    }


@pytest.fixture
def installref_data(
    artifact_url: str, verification_data: dict[str, object]
) -> dict[str, object]:
    return {
        "ref": artifact_url,
        "type": "application/x-pie-executable",
        "encoding": "zstd",
        "verification": verification_data,
    }


@pytest.fixture
def package_data(installref_data: dict[str, object]) -> dict[str, object]:
    return {
        "basename": "gel-cli",
        "version": "1.2.3",
        "slot": "",
        "tags": {},
        "installrefs": [installref_data],
    }


@pytest.fixture
def package_index_data(package_data: dict[str, object]) -> dict[str, object]:
    return {"packages": [package_data]}


@pytest.fixture
def iter_channels() -> Iterator[str]:
    yield from ("stable", "testing", "nightly")
