"""Read package identities with the distribution's own inspection tools."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .metadata import deb_fields
from .models import PackageIdentity, format_evr


def _run(args: list[str]) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(
            f"native tool failed: {args[0]}: {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


def inspect_package(
    path: Path, fmt: str, patterns: list[str], key: Path | None
) -> PackageIdentity:
    """Validate ownership/architecture and return the standard-tool header identity.

    A supplied key also requires a trusted RPM signature. Only withdrawn records
    may pass None: their digest-verified bytes reserve identities, never indexes.
    """
    if fmt == "deb":
        # Querying Version alone normalizes an explicit zero epoch. The complete
        # control block preserves the spelling apt-ftparchive publishes.
        fields = deb_fields(_run(["dpkg-deb", "--field", str(path)]))
        name, full_version, arch = (
            fields[field] for field in ("Package", "Version", "Architecture")
        )
        allowed = ("amd64", "arm64")
    elif fmt == "rpm":
        name, epoch, version, release, arch = _run(
            [
                "rpm",
                "-qp",
                "--queryformat",
                "%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}",
                str(path),
            ]
        ).split("\t")
        full_version = format_evr(epoch, version, release)
        allowed = ("x86_64", "aarch64")
        if key is not None:
            with tempfile.TemporaryDirectory(prefix="native-rpm-keys-") as directory:
                database = str(Path(directory) / "db")
                _run(["rpm", "--dbpath", database, "--initdb"])
                _run(["rpmkeys", "--dbpath", database, "--import", str(key.resolve())])
                signature = _run(
                    [
                        "rpmkeys",
                        "--dbpath",
                        database,
                        "--verbose",
                        "--checksig",
                        str(path),
                    ]
                )
                if not re.search(
                    r"Signature[^\n]*: OK\s*$", signature, re.MULTILINE | re.IGNORECASE
                ):
                    raise ValueError(
                        f"native RPM has no valid trusted signature: {path}"
                    )
    else:
        raise ValueError(f"unsupported native format: {fmt}")
    if not any(re.fullmatch(pattern, name) for pattern in patterns):
        raise ValueError(f"native package name is not allowed: {name}")
    if arch not in allowed:
        raise ValueError(f"unsupported native architecture: {arch}")
    return PackageIdentity.model_validate(
        {"format": fmt, "name": name, "version": full_version, "arch": arch}
    )
