"""Publication of the immutable public JSON Schemas and health response."""

from __future__ import annotations

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


def is_approved_release_record_predecessor(data: bytes) -> bool:
    """Return whether bytes match the inherited Task 3 release-record schema."""

    return hash_bytes(data) in _APPROVED_RELEASE_RECORD_PREDECESSORS


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


def render_schemas(repo: Path, *, allow_release_record_migration: bool = False) -> None:
    """Install canonical public JSON Schemas and the static health response.

    Bootstrap publication may explicitly opt into the approved release-record
    schema migration; every other existing support document remains immutable.
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
    )
    release_record_schema = schema_dir / "release-record.json"
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
                and path == release_record_schema
                and is_approved_release_record_predecessor(existing)
            ):
                raise RenderError(f"immutable support document mismatch: {path}")
            replacements.add(path)
    for path, data in targets:
        if not path.exists() or path in replacements:
            files.atomic_replace(path, data, "support document")
