"""Download and verify immutable native package blobs."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.request import urlopen


def _verified(path: Path, sha256: str, size: int) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == sha256


def fetch_package(url: str, sha256: str, size: int, cache: Path) -> Path:
    """Return a verified cache blob; failed transfers never become cache entries."""
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / sha256
    if _verified(destination, sha256, size):
        return destination
    fd, name = tempfile.mkstemp(prefix=".download-", dir=cache)
    temporary = Path(name)
    try:
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(fd, "wb") as output, urlopen(url, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > size:
                    raise ValueError(f"native package size mismatch: {url}")
                digest.update(chunk)
                output.write(chunk)
        if total != size or digest.hexdigest() != sha256:
            raise ValueError(f"native package SHA-256 or size mismatch: {url}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
