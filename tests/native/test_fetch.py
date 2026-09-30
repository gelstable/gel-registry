from pathlib import Path

import pytest


def test_download_verifies_and_reuses_cache(tmp_path: Path) -> None:
    from gel_registry.native.fetch import fetch_package

    source = tmp_path / "source"
    source.write_bytes(b"hello")
    digest = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    cache = tmp_path / "cache"
    result = fetch_package(source.as_uri(), digest, 5, cache)
    source.unlink()
    assert fetch_package(source.as_uri(), digest, 5, cache) == result
    assert result.read_bytes() == b"hello"


@pytest.mark.parametrize(
    "digest,size",
    [
        ("0" * 64, 5),
        ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", 6),
    ],
)
def test_bad_download_never_enters_cache(
    tmp_path: Path, digest: str, size: int
) -> None:
    from gel_registry.native.fetch import fetch_package

    source = tmp_path / "source"
    source.write_bytes(b"hello")
    cache = tmp_path / "cache"
    with pytest.raises(ValueError):
        fetch_package(source.as_uri(), digest, size, cache)
    assert list(cache.iterdir()) == []


def test_network_failure_does_not_create_cache_entry(tmp_path: Path) -> None:
    from gel_registry.native.fetch import fetch_package

    cache = tmp_path / "cache"
    with pytest.raises(OSError):
        fetch_package("http://127.0.0.1:1/missing", "0" * 64, 5, cache)
    assert list(cache.iterdir()) == []
