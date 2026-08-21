"""Checks over the committed capture, bootstrap, release and schema bytes."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ..constants import capture_urls
from ..contracts import (
    CaptureManifest,
    PackageIndex,
    Pointer,
    ReleaseRecord,
    RootManifest,
    SnapshotListing,
)
from ..digest import canonical_json, hash_bytes
from .report import Collector
from .support import canonical_error, capture_root, display_path, read_file, tree_files

_SCHEMA_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("capture.json", CaptureManifest),
    ("package-index.json", PackageIndex),
    ("release-record.json", ReleaseRecord),
    ("pointer.json", Pointer),
    ("root.json", RootManifest),
    ("snapshot-listing.json", SnapshotListing),
)


def _artifact_name(product: str, platform: str, encoding: str) -> str:
    """Return the canonical filename derived from neutral release fields."""

    suffix = ".exe" if platform.endswith("-windows-msvc") else ""
    identity = f"{product}-{platform}{suffix}"
    return f"{identity}.zst" if encoding == "zstd" else identity


def load_capture(repo: Path, collector: Collector) -> CaptureManifest | None:
    check = "capture.manifest"
    collector.begin(check)
    root = capture_root(repo)
    path = root / "capture.json"
    if not root.exists():
        collector.add(check, display_path(repo, root), "capture root is missing")
        return None
    if root.is_symlink() or not root.is_dir():
        collector.add(
            check, display_path(repo, root), "capture root is not a directory"
        )
        return None
    try:
        raw = read_file(path)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, path), str(exc))
        return None
    entries, identities = _raw_capture_matrix(raw)
    expected_identities = {
        f"{channel}/{platform}" for channel, platform, _url in capture_urls()
    }
    if len(entries) != 24:
        collector.add(
            check,
            display_path(repo, path),
            f"manifest must contain exactly 24 entries (observed {len(entries)})",
        )
    duplicates = sorted(
        identity for identity in set(identities) if identities.count(identity) > 1
    )
    for identity in duplicates:
        collector.add(
            check, display_path(repo, path), f"duplicate matrix entry {identity}"
        )
    missing = sorted(expected_identities - set(identities))
    for identity in missing:
        collector.add(
            check, display_path(repo, path), f"missing matrix entry {identity}"
        )
    try:
        manifest = CaptureManifest.model_validate_json(raw)
    except (OSError, PydanticValidationError, ValueError) as exc:
        collector.add(check, display_path(repo, path), str(exc))
        return None
    canonical_issue = canonical_error(raw, manifest)
    if canonical_issue is not None:
        collector.add(check, display_path(repo, path), canonical_issue)
    if manifest.entries and len(manifest.entries) != 24:
        collector.add(check, display_path(repo, path), "manifest entry count is not 24")
    return manifest


def check_capture_bodies(
    repo: Path,
    manifest: CaptureManifest | None,
    collector: Collector,
) -> None:
    check = "capture.bodies"
    collector.begin(check)
    root = capture_root(repo)
    if manifest is None:
        collector.add(
            check, display_path(repo, root), "capture manifest is unavailable"
        )
        return
    try:
        files = tree_files(root)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, root), str(exc))
        return
    expected = {"capture.json"}
    for entry in manifest.entries:
        if entry.status == 200:
            if entry.path is None:
                collector.add(
                    check,
                    display_path(repo, root / "capture.json"),
                    f"{entry.channel}/{entry.platform} has no body path",
                )
            else:
                expected.add(entry.path)
    for relative in sorted(set(files) - expected):
        collector.add(
            check, display_path(repo, root / relative), "unrecorded capture body"
        )
    for relative in sorted(expected - set(files)):
        collector.add(
            check, display_path(repo, root / relative), "recorded body is missing"
        )

    for entry in manifest.entries:
        if entry.status != 200 or entry.path is None:
            continue
        path = root / entry.path
        if not path.is_file() or path.is_symlink():
            continue
        try:
            body = path.read_bytes()
            observed = hash_bytes(body)
        except OSError as exc:
            collector.add(check, display_path(repo, path), str(exc))
            continue
        expected_size = entry.size
        expected_sha = entry.sha256
        expected_blake = entry.blake2b
        if (
            observed.size != expected_size
            or observed.sha256 != expected_sha
            or observed.blake2b != expected_blake
        ):
            collector.add(
                check,
                display_path(repo, path),
                "capture hash mismatch "
                f"(expected size={expected_size} sha256={expected_sha} "
                f"blake2b={expected_blake}; observed size={observed.size} "
                f"sha256={observed.sha256} blake2b={observed.blake2b})",
            )
        try:
            index = PackageIndex.model_validate_json(body)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(
                check, display_path(repo, path), f"invalid package index: {exc}"
            )
        else:
            # The exact capture is intentionally not canonicalized, but it must
            # still be accepted by the strict package-index contract.
            if not isinstance(index, PackageIndex):
                collector.add(check, display_path(repo, path), "invalid package index")


def check_bootstrap(
    repo: Path,
    manifest: CaptureManifest | None,
    collector: Collector,
) -> None:
    check = "bootstrap.schema"
    collector.begin(check)
    root = repo / "bootstrap"
    if not root.exists():
        collector.add(check, display_path(repo, root), "bootstrap root is missing")
        return
    try:
        files = tree_files(root)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, root), str(exc))
        return
    expected_names: set[str] = set()
    if manifest is not None:
        expected_names = {
            f"{entry.channel}-{entry.platform}.json"
            for entry in manifest.entries
            if entry.status == 200
        }
    for relative in sorted(files):
        path = root / relative
        if "/" in relative or not relative.endswith(".json"):
            collector.add(check, display_path(repo, path), "unexpected bootstrap path")
            continue
        try:
            index = PackageIndex.model_validate_json(files[relative])
        except (PydanticValidationError, ValueError) as exc:
            collector.add(check, display_path(repo, path), str(exc))
            continue
        canonical_issue = canonical_error(files[relative], index)
        if canonical_issue is not None:
            collector.add(check, display_path(repo, path), canonical_issue)
        if expected_names and relative not in expected_names:
            collector.add(
                check,
                display_path(repo, path),
                "no successful capture entry records this index",
            )


def check_releases(repo: Path, collector: Collector) -> None:
    check = "release.schema"
    collector.begin(check)
    root = repo / "releases"
    if not root.exists():
        return
    try:
        files = tree_files(root)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, root), str(exc))
        return
    for relative, raw in sorted(files.items()):
        path = root / relative
        pieces = Path(relative).parts
        if len(pieces) != 2 or not relative.endswith(".json"):
            collector.add(
                check, display_path(repo, path), "unexpected release record path"
            )
            continue
        try:
            record = ReleaseRecord.model_validate_json(raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(check, display_path(repo, path), str(exc))
            continue
        canonical_issue = canonical_error(raw, record)
        if canonical_issue is not None:
            collector.add(check, display_path(repo, path), canonical_issue)
        if pieces[0] != record.product:
            collector.add(
                check,
                display_path(repo, path),
                "release record path does not match its product",
            )
        if Path(pieces[-1]).stem != record.version:
            collector.add(
                check,
                display_path(repo, path),
                "record filename does not match its version",
            )
        for artifact in record.artifacts:
            name = _artifact_name(record.product, artifact.platform, artifact.encoding)
            parsed = urlsplit(artifact.url)
            expected_path = f"/{record.source.repository}/releases/download/"
            expected_path += f"{record.source.release_tag}/{name}"
            if (
                parsed.scheme != "https"
                or parsed.hostname != "github.com"
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or parsed.query
                or parsed.path != expected_path
            ):
                collector.add(
                    check,
                    display_path(repo, path),
                    f"artifact URL is not the allowlisted release URL: {artifact.url}",
                )


def check_schemas(repo: Path, collector: Collector) -> None:
    check = "schema.validity"
    collector.begin(check)
    root = repo / "public"
    schema_root = root / "v1" / "schema"
    if not schema_root.exists():
        collector.add(check, display_path(repo, schema_root), "schema root is missing")
        return
    try:
        files = tree_files(schema_root)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, schema_root), str(exc))
        return
    expected_names = {name for name, _model in _SCHEMA_MODELS}
    for relative in sorted(set(files) - expected_names):
        collector.add(
            check, display_path(repo, schema_root / relative), "unexpected schema path"
        )
    for name, model in _SCHEMA_MODELS:
        path = schema_root / name
        raw = files.get(name)
        if raw is None:
            collector.add(check, display_path(repo, path), "schema document is missing")
            continue
        try:
            json.loads(raw)
        except (TypeError, ValueError) as exc:
            collector.add(check, display_path(repo, path), f"schema is not JSON: {exc}")
            continue
        expected = canonical_json(model.model_json_schema())
        if raw != expected:
            collector.add(
                check,
                display_path(repo, path),
                "schema bytes drift from the model contract",
            )
    health = root / "healthz"
    try:
        health_raw = read_file(health)
    except (OSError, ValueError) as exc:
        collector.add(check, display_path(repo, health), str(exc))
    else:
        if health_raw != b"ok\n":
            collector.add(
                check, display_path(repo, health), "health body must be exactly ok\\n"
            )


def _raw_capture_matrix(raw: bytes) -> tuple[list[object], list[str]]:
    """Extract matrix diagnostics before Pydantic aggregates them."""

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return [], []
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return [], []
    entries = payload["entries"]
    identities: list[str] = []
    for item in entries:
        if isinstance(item, dict):
            channel = item.get("channel")
            platform = item.get("platform")
            if isinstance(channel, str) and isinstance(platform, str):
                identities.append(f"{channel}/{platform}")
    return entries, identities
