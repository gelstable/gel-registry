"""Read package identities with the distribution's own inspection tools."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path


def _run(args: list[str]) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(
            f"native tool failed: {args[0]}: {result.stderr or result.stdout}"
        )
    return result.stdout.strip()


def inspect_package(
    path: Path, fmt: str, patterns: list[str], key: Path | None
) -> tuple[str, str, str, str, str, str]:
    """Validate ownership/architecture and return the standard-tool header identity.

    A supplied key also requires a trusted RPM signature. Only withdrawn records
    may pass None: their digest-verified bytes reserve identities, never indexes.
    """
    if fmt == "deb":
        name, full_version, arch = (
            _run(["dpkg-deb", "--field", str(path), field])
            for field in ("Package", "Version", "Architecture")
        )
        epoch, separator, rest = full_version.partition(":")
        if not separator:
            epoch, rest = "0", epoch
        version, separator, release = rest.rpartition("-")
        if not separator:
            version, release = rest, ""
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
    return fmt, name, epoch, version, release, arch
