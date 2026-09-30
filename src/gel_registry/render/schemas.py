"""Publication of the immutable public JSON Schemas and health response."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel

from ..contracts import (
    CaptureManifest,
    PackageIndex,
    Pointer,
    ReleaseManifest,
    ReleaseRecord,
    RootManifest,
    SnapshotListing,
)
from ..digest import Digests, canonical_json, hash_bytes
from . import files
from .errors import RenderError

_APPROVED_RELEASE_RECORD_PREDECESSORS = {
    Digests(
        size=2916,
        sha256="a5742bee35c43441079fd73c9956876a00f26bdde8358c57e85d90eb9391b56b",
        blake2b=(
            "0128cee6be47f624579f3f456fdcf7643f44bae8104ba319c6df86f4f04953d0b206b4ba091ebd52583a861eb1e0f0d4bcd80b076647de885e91a094d1124426"
        ),
    ),
    Digests(
        size=2906,
        sha256="e6be48808d6a5cf5aa6355a8372973ebebacf504805c7742e81dbc5fed065531",
        blake2b=(
            "fecb435bdf34c7187c114befa39974cf3eae0d5b9356cb6ef09cf2154befa7932"
            "c9f4c16bee5da10471a1fbae978c9b0eb08be702d92e38eec89013a11cbb52e"
        ),
    ),
}


_APPROVED_V1_RELEASE_SCHEMAS = {
    "release-manifest.json": (
        "f8cd2bf071b472558c275cb4cbeb9041bd4d0b903f125d3392864325b307307b"
    ),
    "release-record.json": (
        "da2442427988f6cc3928d844968395f5db3a82656c9d68333ace352cc46053a3"
    ),
}


def is_approved_release_record_predecessor(data: bytes) -> bool:
    """Return whether bytes match an approved legacy release-record schema."""

    return (
        hash_bytes(data) in _APPROVED_RELEASE_RECORD_PREDECESSORS
        or hash_bytes(data).sha256
        == _APPROVED_V1_RELEASE_SCHEMAS["release-record.json"]
    )


def is_approved_release_schema_predecessor(name: str, data: bytes) -> bool:
    """Recognize approved predecessor bytes only for their exact schema name."""
    if name == "release-record.json":
        return is_approved_release_record_predecessor(data)
    return (
        name == "release-manifest.json"
        and hash_bytes(data).sha256 == _APPROVED_V1_RELEASE_SCHEMAS[name]
    )


def _support_documents() -> tuple[tuple[str, bytes], ...]:
    models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("capture.json", CaptureManifest),
        ("package-index.json", PackageIndex),
        ("release-manifest.json", ReleaseManifest),
        ("release-record.json", ReleaseRecord),
        ("pointer.json", Pointer),
        ("root.json", RootManifest),
        ("snapshot-listing.json", SnapshotListing),
    )
    return tuple(
        (name, canonical_json(model.model_json_schema())) for name, model in models
    )


def _client_documents(public: Path) -> list[tuple[Path, bytes]]:
    documents: list[tuple[Path, bytes]] = []
    for channel in ("stable", "testing"):
        stem = "gelstable" + ("-testing" if channel == "testing" else "")
        apt = (
            "Types: deb\nURIs: https://registry.gelstable.com/apt\n"
            f"Suites: {channel}\nComponents: main\n"
            "Signed-By: /etc/apt/keyrings/gelstable.asc\n"
        )
        rpm = (
            f"[{stem}]\nname=Gelstable"
            + (" Testing" if channel == "testing" else "")
            + f"\nbaseurl=https://registry.gelstable.com/rpm/{channel}/$basearch\n"
            + f"enabled={0 if channel == 'testing' else 1}\n"
            + "gpgcheck=1\nrepo_gpgcheck=1\n"
            + "gpgkey=https://registry.gelstable.com/keys/gelstable.asc\n"
        )
        documents.extend(
            (
                (public / f"{stem}.sources", apt.encode()),
                (public / f"{stem}.repo", rpm.encode()),
            )
        )
    key = public / "keys/gelstable.asc"
    if key.exists() or key.is_symlink():
        if key.is_symlink() or not key.is_file():
            raise RenderError(f"public key is not a regular file: {key}")
        with tempfile.TemporaryDirectory(prefix="registry-key-") as home:
            result = subprocess.run(
                [
                    "gpg",
                    "--batch",
                    "--no-options",
                    "--homedir",
                    home,
                    "--with-colons",
                    "--show-keys",
                    str(key.resolve()),
                ],
                capture_output=True,
                check=False,
            )
        if result.returncode:
            raise RenderError("could not inspect public signing key")
        fingerprints = [
            line.split(":")[9]
            for line in result.stdout.decode().splitlines()
            if line.startswith("fpr:")
        ]
        if not fingerprints:
            raise RenderError("public signing key has no fingerprint")
        documents.append(
            (public / "keys/gelstable.fingerprint", (fingerprints[0] + "\n").encode())
        )
    return documents


def render_schemas(repo: Path, *, allow_release_record_migration: bool = False) -> None:
    """Install canonical public JSON Schemas and the static health response.

    The existing migration flag also permits the hash-pinned v1 release-manifest
    and release-record schemas to evolve to v2. Other support documents and
    unrecognized predecessor bytes remain immutable.
    """

    repo = Path(repo)
    public = repo / "public"
    files.ensure_directory_chain(public, "public root")
    files.directory(public, "public root")
    schema_dir = public / "v1" / "schema"
    files.ensure_directory_chain(schema_dir, "schema root")
    files.directory(schema_dir, "schema root")

    documents = list(_support_documents())
    documents.append(("../healthz", b"ok\n"))
    targets = tuple(
        (schema_dir / name if name != "../healthz" else public / "healthz", data)
        for name, data in documents
    ) + tuple(_client_documents(public))
    replacements: set[Path] = set()
    for path, data in targets:
        if path.is_symlink():
            raise RenderError(f"support document is a symlink: {path}")
        if path.exists() and not path.is_file():
            raise RenderError(f"immutable support document mismatch: {path}")
        if path.exists():
            existing = path.read_bytes()
            if existing == data:
                continue
            if not (
                allow_release_record_migration
                and is_approved_release_schema_predecessor(path.name, existing)
            ):
                raise RenderError(f"immutable support document mismatch: {path}")
            replacements.add(path)
    for path, data in targets:
        if not path.exists() or path in replacements:
            files.atomic_replace(path, data, "support document")
