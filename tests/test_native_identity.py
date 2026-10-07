"""Package identities retain distribution-specific version spelling."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gel_registry.contracts.release import NativePackage
from gel_registry.native.inspect import inspect_package


def test_native_package_rejects_empty_asset() -> None:
    with pytest.raises(ValidationError):
        NativePackage(
            url="https://github.com/gelstable/gel-cli/releases/download/pkg-1/cli.deb",
            sha256="a" * 64,
            size=0,
        )


@pytest.mark.parametrize("version", ["0:1.2-3", "1.2-3", "2:1.2-3", "1.2"])
def test_debian_identity_retains_raw_version(monkeypatch: Any, version: str) -> None:
    fields = {"Package": "gel-cli", "Version": version, "Architecture": "amd64"}
    monkeypatch.setattr(
        "gel_registry.native.inspect._run",
        lambda args: "\n".join(f"{key}: {value}" for key, value in fields.items()),
    )
    identity = inspect_package(Path("fixture.deb"), "deb", ["gel-cli"], None)
    assert identity.version == version


def test_retained_metadata_corruption_rejected_offline(tmp_path: Path) -> None:
    import hashlib
    import json

    from gel_registry.validation.native import validate_retained

    native = tmp_path / "native"
    native.mkdir()
    path = native / "package-metadata.json"
    path.write_text("{}\n")
    (native / "render-state.json").write_text(
        json.dumps({"packages_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    )
    path.write_text("{ }\n")
    with pytest.raises(ValueError, match="retained"):
        validate_retained(tmp_path, [])


@pytest.mark.parametrize("status", ["REVKEYSIG", "EXPKEYSIG", "EXPSIG"])
def test_unusable_signer_status_is_rejected(
    tmp_path: Path, monkeypatch: Any, status: str
) -> None:
    from gel_registry.native.sign import verify_signature

    fingerprint = "A" * 40
    monkeypatch.setattr(
        "gel_registry.native.sign.command",
        lambda args: (
            f"[GNUPG:] {status} A user\n[GNUPG:] VALIDSIG {fingerprint} 0\n".encode()
            if args[0] == "gpgv"
            else b"keyring"
        ),
    )
    with pytest.raises(ValueError):
        verify_signature(tmp_path / "key.asc", tmp_path / "sig", tmp_path / "data")


@pytest.mark.parametrize(
    "raw",
    [
        "not XML",
        '<package><version/><checksum type="sha256">a</checksum>'
        '<size package="1"/></package>',
    ],
)
def test_invalid_retained_rpm_is_a_validation_error(raw: str) -> None:
    from gel_registry.native.metadata import StoredPackage, check_stored
    from gel_registry.native.models import PackageIdentity

    stored = StoredPackage(
        identity=PackageIdentity(
            format="rpm", name="gel-7", version="1-1", arch="x86_64"
        ),
        size=1,
        rpm={"primary": raw, "filelists": raw, "other": raw},
    )
    with pytest.raises(ValueError):
        check_stored("a" * 64, stored, live=True)
