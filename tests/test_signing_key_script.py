"""Disposable real-GPG ceremony tests; no operator keys or passphrases."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/signing-key.py"
pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="requires GnuPG")


@pytest.fixture
def pinentry(tmp_path: Path) -> Path:
    # Only disposable test credentials. A private helper exercises real agent prompts.
    helper = tmp_path / "pinentry"
    helper.write_text("""#!/usr/bin/env python3
import sys
new = False
print("OK", flush=True)
for line in sys.stdin:
    cmd = line.strip()
    if cmd.startswith("SETDESC"):
        new = "new passphrase" in cmd.lower()
    if cmd.startswith("GETPIN"):
        print("D " + ("" if new else "disposable-test-passphrase"), flush=True)
    print("OK", flush=True)
    if cmd == "BYE":
        break
""")
    helper.chmod(0o700)
    return helper


def run(*args: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if home:
        env["GNUPGHOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


def verify(bundle: Path, home: Path | None = None) -> subprocess.CompletedProcess[str]:
    inventory = json.loads((bundle / "fingerprints.json").read_text())
    return run(
        "verify",
        "--bundle",
        str(bundle),
        "--primary-fingerprint",
        inventory["primary"],
        "--signing-fingerprint",
        inventory["signing"],
        home=home,
    )


def test_create_rotate_and_restore_are_isolated(tmp_path: Path, pinentry: Path) -> None:
    # Missing primary protection, exporting old secrets, or touching caller home fails.
    caller = tmp_path / "caller"
    caller.mkdir(mode=0o700)
    (caller / "sentinel").write_text("keep")
    bundle = tmp_path / "created"
    linked_pinentry = tmp_path / "pinentry-link"
    linked_pinentry.symlink_to(pinentry)
    result = run(
        "create",
        "--uid",
        "Disposable Ceremony <test@example.invalid>",
        "--out",
        str(bundle),
        "--pinentry-program",
        str(linked_pinentry),
        home=caller,
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in caller.iterdir()) == ["sentinel"]
    assert verify(bundle).returncode == 0
    (bundle / "INCOMPLETE").write_text("unfinished")
    assert verify(bundle).returncode != 0
    (bundle / "INCOMPLETE").unlink()
    old = json.loads((bundle / "fingerprints.json").read_text())
    assert len(old["subkeys"]) == 1
    assert bundle.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in bundle.iterdir())
    assert (
        b":-----BEGIN PGP PUBLIC KEY BLOCK-----"
        in (bundle / "primary-revocation.rev").read_bytes()
    )
    repeated = run("create", "--uid", "Never create", "--out", str(bundle))
    assert repeated.returncode != 0
    assert verify(bundle).returncode == 0
    rotated = tmp_path / "rotated"
    result = run(
        "rotate-subkey",
        "--backup",
        str(bundle / "primary-backup.asc"),
        "--primary-fingerprint",
        old["primary"],
        "--revocation-certificate",
        str(bundle / "primary-revocation.rev"),
        "--out",
        str(rotated),
        "--pinentry-program",
        str(pinentry),
    )
    assert result.returncode == 0, result.stderr
    assert verify(rotated).returncode == 0
    new = json.loads((rotated / "fingerprints.json").read_text())
    assert new["primary"] == old["primary"]
    assert new["signing"] != old["signing"]
    assert set(new["subkeys"]) == {old["signing"], new["signing"]}
    wrong = run(
        "rotate-subkey",
        "--backup",
        str(bundle / "primary-backup.asc"),
        "--primary-fingerprint",
        "0" * 40,
        "--revocation-certificate",
        str(bundle / "primary-revocation.rev"),
        "--out",
        str(tmp_path / "wrong"),
    )
    assert wrong.returncode != 0
    # Public permissions and altered private material must not be silently accepted.
    (rotated / "automation-subkey.asc").chmod(0o644)
    assert verify(rotated).returncode != 0
    (rotated / "automation-subkey.asc").chmod(0o600)
    # Update integrity hash too, to exercise secret policy directly.
    shutil.copyfile(rotated / "primary-backup.asc", rotated / "automation-subkey.asc")
    new["sha256"]["automation-subkey.asc"] = hashlib.sha256(
        (rotated / "automation-subkey.asc").read_bytes()
    ).hexdigest()
    (rotated / "fingerprints.json").write_text(json.dumps(new))
    result = verify(rotated)
    assert result.returncode != 0
    assert "only selected subkey secret" in result.stderr
    protected = tmp_path / "protected-export"
    protected.mkdir(mode=0o700)
    (protected / "gpg-agent.conf").write_text(f"pinentry-program {pinentry}\n")
    try:
        subprocess.run(
            [
                "gpg",
                "--homedir",
                str(protected),
                "--batch",
                "--import",
                str(bundle / "primary-backup.asc"),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "gpg",
                "--homedir",
                str(protected),
                "--batch",
                "--armor",
                "--output",
                str(protected / "ci.asc"),
                "--export-secret-subkeys",
                old["signing"] + "!",
            ],
            check=True,
            capture_output=True,
        )
        shutil.copyfile(protected / "ci.asc", bundle / "automation-subkey.asc")
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", str(protected), "--kill", "gpg-agent"], check=True
        )
    old["sha256"]["automation-subkey.asc"] = hashlib.sha256(
        (bundle / "automation-subkey.asc").read_bytes()
    ).hexdigest()
    (bundle / "fingerprints.json").write_text(json.dumps(old))
    result = verify(bundle)
    assert result.returncode != 0
    assert "GnuPG command failed" in result.stderr


def test_refuses_checkout_and_symlink_destinations(tmp_path: Path) -> None:
    # Removing path validation would put private artifacts in a checkout or alias.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    inside = run("create", "--uid", "No", "--out", str(checkout / "bundle"))
    assert inside.returncode != 0
    assert "checkout" in inside.stderr
    assert not (checkout / "bundle").exists()
    alias = tmp_path / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    linked = run("create", "--uid", "No", "--out", str(alias / "bundle"))
    assert linked.returncode != 0
    assert "symlink" in linked.stderr


def test_recovery_and_inventory_fail_closed(tmp_path: Path, pinentry: Path) -> None:
    # Marker/issuer-only checks, missing hash enforcement, or validation after
    # key generation would accept a broken emergency-recovery bundle.
    bundle = tmp_path / "bundle"
    result = run(
        "create",
        "--uid",
        "Recovery Test <test@example.invalid>",
        "--out",
        str(bundle),
        "--pinentry-program",
        str(pinentry),
    )
    assert result.returncode == 0, result.stderr
    # Any pinentry invocation during invalid rotations would mean key work began.
    pinentry.write_text(
        pinentry.read_text().replace(
            "import sys",
            "import sys\nfrom pathlib import Path\n"
            "Path(__file__).with_name('pinentry-called').touch()",
        )
    )
    inventory = json.loads((bundle / "fingerprints.json").read_text())
    original_inventory = (bundle / "fingerprints.json").read_bytes()
    original_revocation = (bundle / "primary-revocation.rev").read_bytes()
    caller = tmp_path / "caller"
    caller.mkdir(mode=0o700)
    (caller / "sentinel").write_text("preserved")
    other = tmp_path / "other-primary"
    other.mkdir(mode=0o700)
    try:
        subprocess.run(
            ["gpg", "--homedir", str(other), "--batch", "--generate-key"],
            input="Key-Type: RSA\nKey-Length: 2048\nKey-Usage: cert\n"
            "Name-Real: Other Disposable Primary\nExpire-Date: 0\n"
            "%no-protection\n%commit\n",
            text=True,
            capture_output=True,
            check=True,
        )
        foreign_rev = next((other / "openpgp-revocs.d").glob("*.rev")).read_bytes()
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", str(other), "--kill", "gpg-agent"], check=True
        )
    armor = original_revocation.split(b":-----BEGIN PGP PUBLIC KEY BLOCK-----", 1)[1]
    encoded = b"".join(
        line
        for line in armor.splitlines()
        if line and b":" not in line and not line.startswith((b"=", b"-----"))
    )
    packet = bytearray(base64.b64decode(encoded))
    packet[-1] ^= 1
    corrupt = (
        b":-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"
        + base64.b64encode(packet)
        + b"\n-----END PGP PUBLIC KEY BLOCK-----\n"
    )
    bad = {
        "foreign": foreign_rev,
        "marker-only": b":-----BEGIN PGP PUBLIC KEY BLOCK-----\ngarbage\n",
        "corrupt": corrupt,
    }
    accepted = []
    for name, material in bad.items():
        rev = tmp_path / f"{name}.rev"
        rev.write_bytes(material)
        rev.chmod(0o600)
        output = tmp_path / f"rotate-{name}"
        called = tmp_path / "pinentry-called"
        called.unlink(missing_ok=True)
        result = run(
            "rotate-subkey",
            "--backup",
            str(bundle / "primary-backup.asc"),
            "--primary-fingerprint",
            inventory["primary"],
            "--revocation-certificate",
            str(rev),
            "--out",
            str(output),
            "--pinentry-program",
            str(pinentry),
            home=caller,
        )
        if result.returncode == 0 or (output / "primary-backup.asc").exists():
            accepted.append(f"rotation-{name}")
        assert not called.exists(), "rotation reached key-generation pinentry"
        assert rev.read_bytes() == material
        (bundle / "primary-revocation.rev").write_bytes(material)
        edited = json.loads(original_inventory)
        edited["sha256"]["primary-revocation.rev"] = hashlib.sha256(
            material
        ).hexdigest()
        (bundle / "fingerprints.json").write_text(json.dumps(edited))
        if verify(bundle, home=caller).returncode == 0:
            accepted.append(f"verify-{name}")
    (bundle / "primary-revocation.rev").write_bytes(original_revocation)
    for name, mapping in (
        ("empty", {}),
        (
            "missing",
            {
                k: v
                for k, v in inventory["sha256"].items()
                if k != "primary-revocation.rev"
            },
        ),
        ("wrong-type", []),
        ("bad-digest", {**inventory["sha256"], "public.asc": 0}),
    ):
        edited = json.loads(original_inventory)
        edited["sha256"] = mapping
        (bundle / "fingerprints.json").write_text(json.dumps(edited))
        checked = verify(bundle, home=caller)
        if checked.returncode == 0 or "Traceback" in checked.stderr:
            accepted.append(f"inventory-{name}")
    (bundle / "fingerprints.json").write_bytes(original_inventory)
    assert verify(bundle).returncode == 0
    assert (bundle / "primary-revocation.rev").read_bytes() == original_revocation
    assert sorted(p.name for p in caller.iterdir()) == ["sentinel"]
    alias = tmp_path / "backup-alias.asc"
    alias.symlink_to(bundle / "primary-backup.asc")
    result = run(
        "rotate-subkey",
        "--backup",
        str(alias),
        "--primary-fingerprint",
        inventory["primary"],
        "--revocation-certificate",
        str(bundle / "primary-revocation.rev"),
        "--out",
        str(tmp_path / "alias-output"),
        home=caller,
    )
    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert not accepted, f"fail-open cases: {accepted}"
