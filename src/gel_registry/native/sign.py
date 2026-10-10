"""Sign metadata with the selected subkey, then verify using public bytes only."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


def command(args: list[str]) -> bytes:
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"native tool failed: {args[0]}: {result.stderr.decode()}")
    return result.stdout


def verify_signature(
    key: Path,
    signature: Path,
    data: Path | None = None,
    fingerprint: str | None = None,
) -> bytes:
    """Use an isolated public keyring; return verified cleartext for InRelease."""
    with tempfile.TemporaryDirectory(prefix="native-gpgv-") as directory:
        home = Path(directory)
        ring = home / "trusted.gpg"
        ring.write_bytes(
            command(
                [
                    "gpg",
                    "--batch",
                    "--no-options",
                    "--homedir",
                    str(home),
                    "--output",
                    "-",
                    "--dearmor",
                    str(key.resolve()),
                ]
            )
        )
        output = home / "cleartext"
        args = [
            "gpgv",
            "--homedir",
            str(home),
            "--keyring",
            str(ring),
            "--status-fd",
            "1",
        ]
        if data is None:
            args.extend(["--output", str(output)])
        args.append(str(signature))
        if data is not None:
            args.append(str(data))
        status = command(args).decode()
        unusable = {"REVKEYSIG", "EXPKEYSIG", "EXPSIG", "KEYREVOKED", "KEYEXPIRED"}
        if any(
            len(fields := line.split()) > 1 and fields[1] in unusable
            for line in status.splitlines()
            if line.startswith("[GNUPG:] ")
        ):
            raise ValueError(f"revoked or expired signing key: {signature}")
        signers = [
            line.split()[2]
            for line in status.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ")
        ]
        if not signers or (fingerprint and any(s != fingerprint for s in signers)):
            raise ValueError(f"unexpected signing key: {signature}")
        from ..validation.native import signing_subkeys

        trusted = signing_subkeys(key)
        if any(not trusted.get(signer, False) for signer in signers):
            raise ValueError(
                f"metadata signer is not a current signing subkey: {signature}"
            )
        return output.read_bytes() if data is None else b""


def cleartext_matches_release(cleartext: bytes, release: bytes) -> bool:
    # GnuPG versions differ in whether they return the final cleartext framing
    # LF. Permit only that one byte; Release.gpg still authenticates exact bytes.
    return (
        cleartext == release
        or cleartext == release + b"\n"
        or cleartext + b"\n" == release
    )


def sign_native(repo: Path) -> None:
    """Sign only APT Release and RPM repomd.xml using the operator environment."""
    from ..validation.native import validate_native_structure

    validate_native_structure(repo)
    fingerprint = os.environ.get("GELSTABLE_SIGNING_FPR", "").upper()
    if not re.fullmatch(r"(?:[0-9A-F]{40}|[0-9A-F]{64})", fingerprint):
        raise ValueError("GELSTABLE_SIGNING_FPR must be a full signing fingerprint")
    if not os.environ.get("GNUPGHOME"):
        raise ValueError("GNUPGHOME must select the signing key home")
    public = repo / "public"
    key = public / "keys/gelstable.asc"
    targets = [
        *sorted((public / "apt/dists").glob("*/Release")),
        *sorted((public / "rpm").glob("*/*/repodata/repomd.xml")),
    ]
    if not targets:
        raise ValueError("native metadata is missing")
    for target in targets:
        signatures = (
            [
                (target.with_name("InRelease"), ["--clearsign"]),
                (target.with_name("Release.gpg"), ["--detach-sign"]),
            ]
            if target.name == "Release"
            else [(target.with_name("repomd.xml.asc"), ["--detach-sign", "--armor"])]
        )
        for signature, flags in signatures:
            try:
                cleartext = verify_signature(
                    key, signature, None if "--clearsign" in flags else target
                )
                if "--clearsign" not in flags or cleartext_matches_release(
                    cleartext, target.read_bytes()
                ):
                    continue
            except (ValueError, OSError):
                pass
            command(
                [
                    "gpg",
                    "--batch",
                    "--yes",
                    "--no-options",
                    "--digest-algo",
                    "SHA512",
                    "--local-user",
                    fingerprint + "!",
                    "--output",
                    str(signature),
                    *flags,
                    str(target),
                ]
            )
            cleartext = verify_signature(
                key, signature, None if "--clearsign" in flags else target, fingerprint
            )
            if "--clearsign" in flags and not cleartext_matches_release(
                cleartext, target.read_bytes()
            ):
                raise ValueError(f"signed Release differs: {signature}")
