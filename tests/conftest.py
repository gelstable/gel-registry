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
        "name": "gel-cli",
        "version": "1.2.3",
        "version_details": {
            "major": 1,
            "minor": 2,
            "patch": 3,
            "prerelease": [],
            "metadata": {"build_hash": "abc123"},
        },
        "version_key": "1.2.3",
        "revision": "1",
        "build_date": "2026-08-15T00:00:00+00:00",
        "architecture": "x86_64",
        "slot": "",
        "tags": {},
        "installref": installref_data["ref"],
        "installrefs": [installref_data],
    }


@pytest.fixture
def package_index_data(package_data: dict[str, object]) -> dict[str, object]:
    return {"packages": [package_data]}


@pytest.fixture
def iter_channels() -> Iterator[str]:
    yield from ("stable", "testing", "nightly")
