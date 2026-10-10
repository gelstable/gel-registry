"""Write the native section from final package bytes and safe release names."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def native_manifest(directory: Path, repository: str, tag: str) -> dict[str, object]:
    component = r"[A-Za-z0-9][A-Za-z0-9._-]*"
    if not re.fullmatch(component + "/" + component, repository) or not re.fullmatch(
        component, tag
    ):
        raise ValueError("unsafe repository or release tag")
    packages = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in {".deb", ".rpm"}:
            continue
        if (
            path.is_symlink()
            or not path.is_file()
            or not re.fullmatch(component, path.name)
        ):
            raise ValueError("unsafe native package file")
        size = path.stat().st_size
        if not size:
            raise ValueError("empty native package")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        packages.append(
            {
                "url": f"https://github.com/{repository}/releases/download/{tag}/{path.name}",
                "sha256": digest,
                "size": size,
            }
        )
    if not packages:
        raise ValueError("no native packages")
    return {"packages": packages}


if __name__ == "__main__":
    print(
        json.dumps(
            native_manifest(Path(sys.argv[1]), sys.argv[2], sys.argv[3]), sort_keys=True
        )
    )
