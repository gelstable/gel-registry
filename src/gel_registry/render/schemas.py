"""Publication of the immutable public JSON Schemas and health response."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ..contracts import (
    CaptureManifest,
    PackageIndex,
    Pointer,
    ReleaseRecord,
    RootManifest,
    SnapshotListing,
)
from ..digest import canonical_json
from . import files
from .errors import RenderError


def _support_documents() -> tuple[tuple[str, bytes], ...]:
    models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("capture.json", CaptureManifest),
        ("package-index.json", PackageIndex),
        ("release-record.json", ReleaseRecord),
        ("pointer.json", Pointer),
        ("root.json", RootManifest),
        ("snapshot-listing.json", SnapshotListing),
    )
    return tuple(
        (name, canonical_json(model.model_json_schema())) for name, model in models
    )


def render_schemas(repo: Path) -> None:
    """Install canonical public JSON Schemas and the static health response."""

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
    for path, data in targets:
        if path.is_symlink():
            raise RenderError(f"support document is a symlink: {path}")
        if path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise RenderError(f"immutable support document mismatch: {path}")
    for path, data in targets:
        if not path.exists():
            files.atomic_replace(path, data, "support document")
