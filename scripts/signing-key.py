#!/usr/bin/env python3
"""Local GnuPG signing-key ceremony. Never installs or publishes keys."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class CeremonyError(Exception):
    """An incomplete or unsafe ceremony."""


def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path.expanduser()))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise CeremonyError("symlink paths are not accepted")
    return path


def private_file(path: Path) -> None:
    safe_path(path)
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise CeremonyError(f"missing file or insecure permissions: {path.name}")


def destination(path: Path) -> Path:
    path = safe_path(path)
    if any((p / ".git").exists() for p in (path, *path.parents)):
        raise CeremonyError("output must be outside a Git checkout")
    if path.exists():
        raise CeremonyError("output already exists; refusing overwrite")
    path.mkdir(mode=0o700)
    (path / "INCOMPLETE").write_text(
        "Incomplete ceremony; retain any primary backup.\n"
    )
    return path


@contextmanager
def key_home(pinentry: str | None = None) -> Iterator[Path]:
    # /tmp keeps private homes outside the checkout even when TMPDIR is overridden.
    with tempfile.TemporaryDirectory(prefix="gel-key-", dir="/tmp") as name:
        home = Path(name)
        if pinentry:
            if any(c in pinentry for c in "\n\r"):
                raise CeremonyError("invalid pinentry program")
            program = Path(pinentry).expanduser().resolve(strict=True)
            if (
                any(c in str(program) for c in "\n\r")
                or not program.is_file()
                or not os.access(program, os.X_OK)
            ):
                raise CeremonyError("invalid pinentry program")
            (home / "gpg-agent.conf").write_text(f"pinentry-program {program}\n")
        try:
            yield home
        finally:
            subprocess.run(
                ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def gpg(home: Path, *args: str, batch: bool = False) -> bytes:
    env = os.environ.copy()
    env["GNUPGHOME"] = str(home)
    env.pop("GPG_AGENT_INFO", None)
    command = ["gpg", "--no-options", "--homedir", str(home)]
    if batch:
        command += ["--batch", "--pinentry-mode", "error"]
    result = subprocess.run(
        [*command, *args], stdout=subprocess.PIPE, env=env, check=False
    )
    if result.returncode:
        raise CeremonyError("GnuPG command failed; ceremony is incomplete")
    return result.stdout


def records(home: Path, secret: bool = False) -> list[dict[str, Any]]:
    raw = gpg(
        home,
        "--with-colons",
        "--with-keygrip",
        "--with-subkey-fingerprint",
        "--list-secret-keys" if secret else "--list-keys",
        batch=True,
    )
    keys: list[dict[str, Any]] = []
    for line in raw.decode().splitlines():
        fields = line.split(":")
        if fields[0] in {"pub", "sub", "sec", "ssb"}:
            keys.append(
                {
                    "kind": fields[0],
                    "bits": fields[2],
                    "algo": fields[3],
                    "expires": fields[6],
                    "validity": fields[1],
                    "usage": fields[11],
                    "secret": fields[14] if secret else "",
                }
            )
        elif fields[0] == "fpr" and keys:
            keys[-1]["fpr"] = fields[9]
        elif fields[0] == "grp" and keys:
            keys[-1]["grip"] = fields[9]
    return keys


def policy(
    keys: list[dict[str, Any]], primary: str, signing: str | None = None
) -> None:
    primaries = [k for k in keys if k["kind"] in {"pub", "sec"}]
    if len(primaries) != 1 or primaries[0]["fpr"] != primary:
        raise CeremonyError("expected exactly the specified primary fingerprint")
    selected = [primaries[0]]
    if signing:
        subs = [k for k in keys if k["kind"] in {"sub", "ssb"} and k["fpr"] == signing]
        if len(subs) != 1:
            raise CeremonyError("selected signing subkey missing")
        selected += subs
    for index, key in enumerate(selected):
        # Uppercase capabilities aggregate the entire certificate; lowercase is own use.
        own_usage = "".join(c for c in key["usage"] if c.islower())
        if (
            key["bits"] != "4096"
            or key["algo"] != "1"
            or key["expires"]
            or key["validity"] in {"r", "e", "d"}
            or own_usage != ("c" if index == 0 else "s")
        ):
            raise CeremonyError(
                "requires non-expiring RSA4096 certify-only primary and signing subkey"
            )


def protected_primary(home: Path, primary: str) -> None:
    keys = records(home, secret=True)
    key = next((k for k in keys if k["fpr"] == primary), None)
    if not key or key["secret"] != "+":
        raise CeremonyError("actual primary secret missing from custody backup")
    # GPG agent's documented KEYINFO protection field: P protected, C clear.
    result = subprocess.run(
        ["gpg-connect-agent", "--homedir", str(home), f"KEYINFO {key['grip']}", "/bye"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    info = [
        line.split()
        for line in result.stdout.decode().splitlines()
        if line.startswith("S KEYINFO ")
    ]
    if len(info) != 1 or info[0][7] != "P":
        raise CeremonyError("primary secret must be passphrase protected")


def export(home: Path, target: Path, operation: str, fingerprint: str) -> None:
    gpg(home, "--armor", "--output", str(target), operation, fingerprint)
    private_file(target)


def fingerprint(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise CeremonyError("a full 40-character fingerprint is required")
    return value.upper()


def validate_revocation(certificate: bytes, revocation: bytes, primary: str) -> None:
    marker = b":-----BEGIN PGP PUBLIC KEY BLOCK-----"
    if revocation.count(marker) != 1:
        raise CeremonyError("expected an import-protected GPG primary revocation")
    # Authenticate only in a disposable public-only copy. Original bytes stay intact.
    with key_home() as scratch:
        public_file = scratch / "public.asc"
        public_file.write_bytes(certificate)
        gpg(scratch, "--import", str(public_file), batch=True)
        before = records(scratch)
        policy(before, primary)
        if records(scratch, secret=True):
            raise CeremonyError("revocation validation requires public-only material")
        if before[0]["validity"] == "r":
            raise CeremonyError("revocation validation requires an unrevoked primary")
        activated = scratch / "validation-revocation.asc"
        activated.write_bytes(revocation.replace(marker, marker[1:], 1))
        try:
            gpg(scratch, "--import", str(activated), batch=True)
        except CeremonyError as exc:
            raise CeremonyError("invalid primary revocation certificate") from exc
        after = records(scratch)
        if (
            len(after) != len(before)
            or [k["fpr"] for k in after] != [k["fpr"] for k in before]
            or after[0]["fpr"] != primary
            or after[0]["validity"] != "r"
            or records(scratch, secret=True)
        ):
            raise CeremonyError("revocation does not authenticate the expected primary")


def verify_bundle(
    bundle: Path, primary: str, signing: str, *, allow_incomplete: bool = False
) -> None:
    bundle = safe_path(bundle)
    if (bundle / "INCOMPLETE").exists() and not allow_incomplete:
        raise CeremonyError("bundle ceremony is marked INCOMPLETE")
    if not bundle.is_dir() or bundle.stat().st_mode & 0o077:
        raise CeremonyError("bundle must be a private directory (0700)")
    for name in (
        "primary-backup.asc",
        "automation-subkey.asc",
        "public.asc",
        "primary-revocation.rev",
        "fingerprints.json",
        "README.txt",
    ):
        private_file(bundle / name)
    inventory = json.loads((bundle / "fingerprints.json").read_text())
    if not isinstance(inventory, dict):
        raise CeremonyError("inventory must be a JSON object")
    if inventory["primary"] != primary or inventory["signing"] != signing:
        raise CeremonyError("inventory does not match expected fingerprints")
    hashes = inventory["sha256"]
    expected = {
        "primary-backup.asc",
        "automation-subkey.asc",
        "public.asc",
        "primary-revocation.rev",
    }
    if not isinstance(hashes, dict) or set(hashes) != expected:
        raise CeremonyError("inventory requires exactly all four artifact hashes")
    for name, digest in hashes.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CeremonyError("inventory requires lowercase SHA256 hex digests")
        if hashlib.sha256((bundle / name).read_bytes()).hexdigest() != digest:
            raise CeremonyError(f"bundle artifact changed: {name}")
    with key_home() as public, key_home() as custody, key_home() as automation:
        for home, name in (
            (public, "public.asc"),
            (custody, "primary-backup.asc"),
            (automation, "automation-subkey.asc"),
        ):
            gpg(home, "--import", str(bundle / name), batch=True)
            policy(records(home), primary, signing)
        if records(public, secret=True):
            raise CeremonyError("public certificate contains secret material")
        canonical = gpg(public, "--export", primary, batch=True)
        validate_revocation(
            canonical, (bundle / "primary-revocation.rev").read_bytes(), primary
        )
        if gpg(custody, "--export", primary, batch=True) != canonical:
            raise CeremonyError("custody/public certificates differ")
        if inventory["subkeys"] != [k["fpr"] for k in records(public)[1:]]:
            raise CeremonyError("public subkey inventory differs")
        protected_primary(custody, primary)
        actual = [
            k["fpr"] for k in records(automation, secret=True) if k["secret"] == "+"
        ]
        if actual != [signing]:
            raise CeremonyError("CI export must contain only selected subkey secret")
        if any(k["secret"] not in {"+", "#"} for k in records(automation, secret=True)):
            raise CeremonyError("CI export contains unavailable/external secret keys")
        # Fresh imported homes prevent cached unlocks from masking protection.
        payload = automation / "challenge"
        payload.write_bytes(b"Gelstable local custody restore test\n")
        signature = automation / "challenge.sig"
        gpg(
            automation,
            "--local-user",
            signing + "!",
            "--output",
            str(signature),
            "--detach-sign",
            str(payload),
            batch=True,
        )
        status = gpg(
            public,
            "--status-fd",
            "1",
            "--verify",
            str(signature),
            str(payload),
            batch=True,
        ).decode()
        valid = [
            line.split()
            for line in status.splitlines()
            if line.startswith("[GNUPG:] VALIDSIG ")
        ]
        if len(valid) != 1 or valid[0][2] != signing or valid[0][-1] != primary:
            raise CeremonyError("test signature used an unexpected key")


def build(args: argparse.Namespace) -> tuple[str, str]:
    out = destination(args.out)
    with key_home(args.pinentry_program) as custody:
        if args.command == "create":
            print(
                "GPG will prompt for a strong primary passphrase. Keep it in custody.",
                file=sys.stderr,
            )
            gpg(
                custody,
                "--batch",
                "--yes",
                "--quick-generate-key",
                args.uid,
                "rsa4096",
                "cert",
                "0",
            )
            primary = records(custody)[0]["fpr"]
        else:
            primary = fingerprint(args.primary_fingerprint)
            private_file(args.backup)
            private_file(args.revocation_certificate)
            gpg(custody, "--import", str(args.backup), batch=True)
        policy(records(custody), primary)
        protected_primary(custody, primary)
        source = (
            custody / "openpgp-revocs.d" / f"{primary}.rev"
            if args.command == "create"
            else args.revocation_certificate
        )
        revocation = source.read_bytes()
        validate_revocation(
            gpg(custody, "--export", primary, batch=True), revocation, primary
        )
        old = {k["fpr"] for k in records(custody)[1:]}
        gpg(custody, "--batch", "--quick-add-key", primary, "rsa4096", "sign", "0")
        added = [k["fpr"] for k in records(custody)[1:] if k["fpr"] not in old]
        if len(added) != 1:
            raise CeremonyError("expected one new signing subkey")
        signing = added[0]
        policy(records(custody), primary, signing)
        export(custody, out / "primary-backup.asc", "--export-secret-keys", primary)
        export(custody, out / "public.asc", "--export", primary)
        (out / "primary-revocation.rev").write_bytes(revocation)
        with key_home(args.pinentry_program) as automation:
            intermediate = automation / "protected-subkey.asc"
            export(custody, intermediate, "--export-secret-subkeys", signing + "!")
            gpg(automation, "--import", str(intermediate), batch=True)
            secrets = records(automation, secret=True)
            if [k["fpr"] for k in secrets if k["secret"] == "+"] != [signing]:
                raise CeremonyError(
                    "refusing passphrase change: unexpected private material"
                )
            print(
                "In the isolated CI copy: enter the existing passphrase, then leave "
                "the NEW passphrase empty and confirm. "
                "Custody primary stays protected.",
                file=sys.stderr,
            )
            grip = next(k["grip"] for k in secrets if k["fpr"] == signing)
            changed = subprocess.run(
                [
                    "gpg-connect-agent",
                    "--homedir",
                    str(automation),
                    f"PASSWD {grip}",
                    "/bye",
                ],
                stdout=subprocess.PIPE,
                check=True,
            )
            if any(line.startswith(b"ERR ") for line in changed.stdout.splitlines()):
                raise CeremonyError("GPG agent passphrase change failed")
            export(
                automation,
                out / "automation-subkey.asc",
                "--export-secret-subkeys",
                signing + "!",
            )
        inventory = {
            "primary": primary,
            "signing": signing,
            "subkeys": [k["fpr"] for k in records(custody)[1:]],
            "sha256": {
                name: hashlib.sha256((out / name).read_bytes()).hexdigest()
                for name in (
                    "primary-backup.asc",
                    "public.asc",
                    "automation-subkey.asc",
                    "primary-revocation.rev",
                )
            },
        }
        (out / "fingerprints.json").write_text(json.dumps(inventory, indent=2) + "\n")
        (out / "README.txt").write_text(
            "LOCAL CUSTODY BUNDLE — PRIVATE\n"
            f"Primary: {primary}\nAutomation signing subkey: {signing}\n"
            "primary-backup.asc: protected primary and all historical subkeys.\n"
            "automation-subkey.asc: UNPROTECTED selected secret subkey, "
            "no primary secret.\n"
            "public.asc: public certificate; fingerprints.json: "
            "inventory and integrity hashes.\n"
            "primary-revocation.rev: GPG import-protection colon "
            "retained. Never activate/import\n"
            "or publish it without following the manual compromise procedure.\n"
            "Upload separate custody Documents/attachments and store "
            "primary passphrase in\n"
            "1Password as described in docs/operations.md. Test "
            "downloaded copies with verify.\n"
            "Confirm primary unlock manually in a disposable home with "
            "its saved passphrase.\n"
            "No secrets, public trust, published keys, metadata, or old "
            "keys were updated.\n"
        )
    verify_bundle(out, primary, signing, allow_incomplete=True)
    (out / "INCOMPLETE").unlink()
    return primary, signing


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "rotate-subkey"):
        sub = commands.add_parser(command)
        sub.add_argument("--out", type=Path, required=True)
        sub.add_argument(
            "--pinentry-program", help="optional absolute pinentry executable"
        )
        if command == "create":
            sub.add_argument("--uid", required=True)
        else:
            sub.add_argument("--backup", type=Path, required=True)
            sub.add_argument("--primary-fingerprint", required=True)
            sub.add_argument("--revocation-certificate", type=Path, required=True)
    sub = commands.add_parser("verify")
    sub.add_argument("--bundle", type=Path, required=True)
    sub.add_argument("--primary-fingerprint", required=True)
    sub.add_argument("--signing-fingerprint", required=True)
    args = parser.parse_args()
    try:
        if not all(shutil.which(p) for p in ("gpg", "gpgconf", "gpg-connect-agent")):
            raise CeremonyError("GnuPG 2.4 tools are required")
        if args.command == "verify":
            verify_bundle(
                args.bundle,
                fingerprint(args.primary_fingerprint),
                fingerprint(args.signing_fingerprint),
            )
            print(
                "Bundle verified; manually confirm protected primary unlock "
                "before custody acceptance."
            )
        else:
            primary, signing = build(args)
            print(
                f"Verified local bundle: {args.out}\nPrimary: {primary}\n"
                f"Signing subkey: {signing}"
            )
        return 0
    except (
        CeremonyError,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted; retain incomplete output and any primary backup.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
