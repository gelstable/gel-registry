"""Offline integrity gates and explicitly-scoped remote release checks.

The local validator is deliberately a consumer of committed bytes.  It does
not create an HTTP client and it never asks Git or a remote host for history.
The remote release entry point is separate so workflows can opt into network
checks only for release pull requests.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from .constants import capture_urls
from .digest import canonical_json, hash_bytes
from .github import GITHUB_REPOSITORY, GitHubAsset, GitHubRelease, download_assets
from .normalize import NormalizationError, validate_normalization
from .render import (
    RenderError,
    _load_pinned_snapshot,
    build_snapshot,
    render_schemas,
    select_snapshot,
)
from .schema import (
    CaptureManifest,
    PackageIndex,
    Pointer,
    ReleaseRecord,
    RootManifest,
    SnapshotListing,
)
from .verify import VerificationError, verify_cli_release

_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_CAPTURE_REL = Path("upstream/packages.geldata.com/legacy-2026-08-bootstrap")
_SCHEMA_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("capture.json", CaptureManifest),
    ("package-index.json", PackageIndex),
    ("release-record.json", ReleaseRecord),
    ("pointer.json", Pointer),
    ("root.json", RootManifest),
    ("snapshot-listing.json", SnapshotListing),
)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Stable check names and path-qualified validation failures.

    ``errors`` contains display-ready strings so callers can print a report
    without knowing an internal issue type.  ``checks`` records every layer
    that ran, including checks which produced no errors.  The aliases are
    intentionally small conveniences for command boundaries and tests.
    """

    errors: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether all executed checks passed."""

        return not self.errors

    @property
    def valid(self) -> bool:
        """Compatibility alias for :attr:`ok`."""

        return self.ok

    @property
    def passed(self) -> bool:
        """Compatibility alias for :attr:`ok`."""

        return self.ok

    @property
    def failures(self) -> tuple[str, ...]:
        """Compatibility alias for :attr:`errors`."""

        return self.errors

    @property
    def check_names(self) -> tuple[str, ...]:
        """Compatibility alias for :attr:`checks`."""

        return self.checks

    def __bool__(self) -> bool:
        return self.ok


class _Collector:
    def __init__(self) -> None:
        self.checks: list[str] = []
        self.errors: list[str] = []

    def begin(self, name: str) -> None:
        if name not in self.checks:
            self.checks.append(name)

    def add(self, check: str, path: Path | str | None, message: str) -> None:
        self.begin(check)
        path_text = str(path) if path is not None else ""
        if path_text:
            self.errors.append(f"{check}: {path_text}: {message}")
        else:
            self.errors.append(f"{check}: {message}")

    def run(
        self,
        check: str,
        path: Path | str | None,
        operation: Callable[[], object],
    ) -> object | None:
        self.begin(check)
        try:
            return operation()
        except Exception as exc:  # validation must aggregate independent layers
            self.add(check, path, str(exc))
            return None

    def report(self) -> ValidationReport:
        return ValidationReport(
            errors=tuple(self.errors),
            checks=tuple(self.checks),
        )


def _display_path(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()


def _read_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("is not a regular file")
    return path.read_bytes()


def _tree_files(root: Path) -> dict[str, bytes]:
    """Read a regular-file tree in deterministic relative-path order."""

    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{root} is not a regular directory")
    result: dict[str, bytes] = {}
    paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"contains symlink {relative}")
        if path.is_file():
            result[relative] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError(f"contains unsupported path {relative}")
    return result


def _canonical_error(raw: bytes, value: BaseModel) -> str | None:
    expected = canonical_json(value)
    if raw != expected:
        return "is not canonical JSON"
    return None


def _capture_root(repo: Path) -> Path:
    return repo / _CAPTURE_REL


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


def _load_capture(repo: Path, collector: _Collector) -> CaptureManifest | None:
    check = "capture.manifest"
    collector.begin(check)
    root = _capture_root(repo)
    path = root / "capture.json"
    if not root.exists():
        collector.add(check, _display_path(repo, root), "capture root is missing")
        return None
    if root.is_symlink() or not root.is_dir():
        collector.add(
            check, _display_path(repo, root), "capture root is not a directory"
        )
        return None
    try:
        raw = _read_file(path)
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, path), str(exc))
        return None
    entries, identities = _raw_capture_matrix(raw)
    expected_identities = {
        f"{channel}/{platform}" for channel, platform, _url in capture_urls()
    }
    if len(entries) != 24:
        collector.add(
            check,
            _display_path(repo, path),
            f"manifest must contain exactly 24 entries (observed {len(entries)})",
        )
    duplicates = sorted(
        identity for identity in set(identities) if identities.count(identity) > 1
    )
    for identity in duplicates:
        collector.add(
            check, _display_path(repo, path), f"duplicate matrix entry {identity}"
        )
    missing = sorted(expected_identities - set(identities))
    for identity in missing:
        collector.add(
            check, _display_path(repo, path), f"missing matrix entry {identity}"
        )
    try:
        manifest = CaptureManifest.model_validate_json(raw)
    except (OSError, PydanticValidationError, ValueError) as exc:
        collector.add(check, _display_path(repo, path), str(exc))
        return None
    canonical_error = _canonical_error(raw, manifest)
    if canonical_error is not None:
        collector.add(check, _display_path(repo, path), canonical_error)
    if manifest.entries and len(manifest.entries) != 24:
        collector.add(
            check, _display_path(repo, path), "manifest entry count is not 24"
        )
    return manifest


def _check_capture_bodies(
    repo: Path,
    manifest: CaptureManifest | None,
    collector: _Collector,
) -> None:
    check = "capture.bodies"
    collector.begin(check)
    root = _capture_root(repo)
    if manifest is None:
        collector.add(
            check, _display_path(repo, root), "capture manifest is unavailable"
        )
        return
    try:
        files = _tree_files(root)
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, root), str(exc))
        return
    expected = {"capture.json"}
    for entry in manifest.entries:
        if entry.status == 200:
            if entry.path is None:
                collector.add(
                    check,
                    _display_path(repo, root / "capture.json"),
                    f"{entry.channel}/{entry.platform} has no body path",
                )
            else:
                expected.add(entry.path)
    for relative in sorted(set(files) - expected):
        collector.add(
            check, _display_path(repo, root / relative), "unrecorded capture body"
        )
    for relative in sorted(expected - set(files)):
        collector.add(
            check, _display_path(repo, root / relative), "recorded body is missing"
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
            collector.add(check, _display_path(repo, path), str(exc))
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
                _display_path(repo, path),
                "capture hash mismatch "
                f"(expected size={expected_size} sha256={expected_sha} "
                f"blake2b={expected_blake}; observed size={observed.size} "
                f"sha256={observed.sha256} blake2b={observed.blake2b})",
            )
        try:
            index = PackageIndex.model_validate_json(body)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(
                check, _display_path(repo, path), f"invalid package index: {exc}"
            )
        else:
            # The exact capture is intentionally not canonicalized, but it must
            # still be accepted by the strict package-index contract.
            if not isinstance(index, PackageIndex):
                collector.add(check, _display_path(repo, path), "invalid package index")


def _check_bootstrap(
    repo: Path,
    manifest: CaptureManifest | None,
    collector: _Collector,
) -> None:
    check = "bootstrap.schema"
    collector.begin(check)
    root = repo / "bootstrap"
    if not root.exists():
        collector.add(check, _display_path(repo, root), "bootstrap root is missing")
        return
    try:
        files = _tree_files(root)
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, root), str(exc))
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
            collector.add(check, _display_path(repo, path), "unexpected bootstrap path")
            continue
        try:
            index = PackageIndex.model_validate_json(files[relative])
        except (PydanticValidationError, ValueError) as exc:
            collector.add(check, _display_path(repo, path), str(exc))
            continue
        canonical_error = _canonical_error(files[relative], index)
        if canonical_error is not None:
            collector.add(check, _display_path(repo, path), canonical_error)
        if expected_names and relative not in expected_names:
            collector.add(
                check,
                _display_path(repo, path),
                "no successful capture entry records this index",
            )


def _check_releases(repo: Path, collector: _Collector) -> None:
    check = "release.schema"
    collector.begin(check)
    root = repo / "releases"
    if not root.exists():
        return
    try:
        files = _tree_files(root)
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, root), str(exc))
        return
    for relative, raw in sorted(files.items()):
        path = root / relative
        pieces = Path(relative).parts
        if len(pieces) != 2 or pieces[0] != "gel-cli" or not relative.endswith(".json"):
            collector.add(
                check, _display_path(repo, path), "unexpected release record path"
            )
            continue
        try:
            record = ReleaseRecord.model_validate_json(raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(check, _display_path(repo, path), str(exc))
            continue
        canonical_error = _canonical_error(raw, record)
        if canonical_error is not None:
            collector.add(check, _display_path(repo, path), canonical_error)
        if Path(pieces[-1]).stem != record.version:
            collector.add(
                check,
                _display_path(repo, path),
                "record filename does not match its version",
            )
        for artifact in record.artifacts:
            name = _artifact_name(artifact.platform, artifact.encoding)
            parsed = urlsplit(artifact.url)
            expected_path = (
                f"/gelstable/gel-cli/releases/download/v{record.version}/{name}"
            )
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
                    _display_path(repo, path),
                    f"artifact URL is not the allowlisted release URL: {artifact.url}",
                )


def _check_schemas(repo: Path, collector: _Collector) -> None:
    check = "schema.validity"
    collector.begin(check)
    root = repo / "public"
    schema_root = root / "v1" / "schema"
    if not schema_root.exists():
        collector.add(check, _display_path(repo, schema_root), "schema root is missing")
        return
    try:
        files = _tree_files(schema_root)
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, schema_root), str(exc))
        return
    expected_names = {name for name, _model in _SCHEMA_MODELS}
    for relative in sorted(set(files) - expected_names):
        collector.add(
            check, _display_path(repo, schema_root / relative), "unexpected schema path"
        )
    for name, model in _SCHEMA_MODELS:
        path = schema_root / name
        raw = files.get(name)
        if raw is None:
            collector.add(
                check, _display_path(repo, path), "schema document is missing"
            )
            continue
        try:
            json.loads(raw)
        except (TypeError, ValueError) as exc:
            collector.add(
                check, _display_path(repo, path), f"schema is not JSON: {exc}"
            )
            continue
        expected = canonical_json(model.model_json_schema())
        if raw != expected:
            collector.add(
                check,
                _display_path(repo, path),
                "schema bytes drift from the model contract",
            )
    health = root / "healthz"
    try:
        health_raw = _read_file(health)
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, health), str(exc))
    else:
        if health_raw != b"ok\n":
            collector.add(
                check, _display_path(repo, health), "health body must be exactly ok\\n"
            )


def _check_pointer_and_snapshots(repo: Path, collector: _Collector) -> None:
    pointer_check = "pointer.integrity"
    listing_check = "snapshot.listing"
    internals_check = "snapshot.internals"
    moving_check = "moving.root"
    collector.begin(pointer_check)
    collector.begin(listing_check)
    collector.begin(internals_check)
    collector.begin(moving_check)
    pointer_path = repo / "pointers" / "latest.json"
    pointer: Pointer | None = None
    try:
        pointer_raw = _read_file(pointer_path)
    except (OSError, ValueError) as exc:
        collector.add(pointer_check, _display_path(repo, pointer_path), str(exc))
    else:
        try:
            pointer = Pointer.model_validate_json(pointer_raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(pointer_check, _display_path(repo, pointer_path), str(exc))
        else:
            canonical_error = _canonical_error(pointer_raw, pointer)
            if canonical_error is not None:
                collector.add(
                    pointer_check, _display_path(repo, pointer_path), canonical_error
                )

    public = repo / "public"
    snapshots_root = public / "s"
    snapshots: list[str] = []
    if not snapshots_root.exists():
        collector.add(
            internals_check,
            _display_path(repo, snapshots_root),
            "snapshot root is missing",
        )
    elif snapshots_root.is_symlink() or not snapshots_root.is_dir():
        collector.add(
            internals_check,
            _display_path(repo, snapshots_root),
            "snapshot root is not a directory",
        )
    else:
        try:
            children = sorted(snapshots_root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            collector.add(
                internals_check, _display_path(repo, snapshots_root), str(exc)
            )
            children = []
        for child in children:
            if child.is_symlink():
                collector.add(
                    internals_check,
                    _display_path(repo, child),
                    "snapshot path is a symlink",
                )
                continue
            if not child.is_dir():
                collector.add(
                    internals_check,
                    _display_path(repo, child),
                    "unexpected path beneath public/s",
                )
                continue
            if _SNAPSHOT_ID.fullmatch(child.name) is None:
                collector.add(
                    internals_check,
                    _display_path(repo, child),
                    "invalid snapshot directory name",
                )
                continue
            snapshots.append(child.name)

    manifests: dict[str, RootManifest] = {}
    for snapshot in snapshots:
        path = snapshots_root / snapshot
        try:
            manifest, _indexes = _load_pinned_snapshot(repo, snapshot)
        except (RenderError, OSError, ValueError) as exc:
            collector.add(internals_check, _display_path(repo, path), str(exc))
        else:
            manifests[snapshot] = manifest

    listing_path = public / "v1" / "snapshots.json"
    listing: SnapshotListing | None = None
    try:
        listing_raw = _read_file(listing_path)
    except (OSError, ValueError) as exc:
        collector.add(listing_check, _display_path(repo, listing_path), str(exc))
    else:
        try:
            listing = SnapshotListing.model_validate_json(listing_raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(listing_check, _display_path(repo, listing_path), str(exc))
        else:
            canonical_error = _canonical_error(listing_raw, listing)
            if canonical_error is not None:
                collector.add(
                    listing_check, _display_path(repo, listing_path), canonical_error
                )
            if set(listing.snapshots) != set(snapshots):
                collector.add(
                    listing_check,
                    _display_path(repo, listing_path),
                    f"snapshot listing does not account for committed snapshots "
                    f"(expected={snapshots} observed={list(listing.snapshots)})",
                )
            if pointer is not None and listing.latest != pointer.snapshot:
                collector.add(
                    listing_check,
                    _display_path(repo, listing_path),
                    "latest "
                    f"{listing.latest} does not match pointer {pointer.snapshot}",
                )

    moving_path = public / "registry.json"
    moving: RootManifest | None = None
    try:
        moving_raw = _read_file(moving_path)
    except (OSError, ValueError) as exc:
        collector.add(moving_check, _display_path(repo, moving_path), str(exc))
    else:
        try:
            moving = RootManifest.model_validate_json(moving_raw)
        except (PydanticValidationError, ValueError) as exc:
            collector.add(moving_check, _display_path(repo, moving_path), str(exc))
        else:
            canonical_error = _canonical_error(moving_raw, moving)
            if canonical_error is not None:
                collector.add(
                    moving_check, _display_path(repo, moving_path), canonical_error
                )

    if pointer is not None:
        selected = pointer.snapshot
        if selected not in snapshots:
            collector.add(
                pointer_check,
                _display_path(repo, pointer_path),
                f"selected snapshot is not committed: {selected}",
            )
        selected_manifest = manifests.get(selected)
        if selected_manifest is not None and moving is not None:
            expected = tuple(
                (item.channel, item.platform, f"s/{selected}/{item.url}")
                for item in selected_manifest.indexes
            )
            observed = tuple(
                (item.channel, item.platform, item.url) for item in moving.indexes
            )
            if observed != expected:
                collector.add(
                    moving_check,
                    _display_path(repo, moving_path),
                    "moving root references the wrong snapshot "
                    f"(expected={expected} observed={observed})",
                )


def _compare_tree_bytes(
    repo: Path,
    candidate: Path,
    committed: Path,
    check: str,
    collector: _Collector,
) -> None:
    try:
        candidate_files = _tree_files(candidate)
        committed_files = _tree_files(committed)
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, committed), str(exc))
        return
    for relative in sorted(set(candidate_files) - set(committed_files)):
        collector.add(
            check,
            _display_path(repo, committed / relative),
            "rendered path is missing from the committed tree",
        )
    for relative in sorted(set(committed_files) - set(candidate_files)):
        collector.add(
            check,
            _display_path(repo, committed / relative),
            "committed path is absent from a fresh render",
        )
    for relative in sorted(set(candidate_files) & set(committed_files)):
        if candidate_files[relative] != committed_files[relative]:
            collector.add(
                check, _display_path(repo, committed / relative), "rendered bytes drift"
            )


def _copy_render_inputs(repo: Path, stage: Path) -> None:
    stage.mkdir(parents=True)
    for name in ("upstream", "bootstrap", "releases", "pointers"):
        source = repo / name
        destination = stage / name
        if source.is_symlink():
            destination.symlink_to(
                source.readlink(), target_is_directory=source.is_dir()
            )
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        elif source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    # Pinned snapshots are immutable render inputs.  Moving documents,
    # schemas, healthz, and any other public path must be recreated by the
    # fresh render so the comparison covers the complete public path set.
    pinned = repo / "public" / "s"
    if pinned.is_symlink():
        (stage / "public" / "s").symlink_to(
            pinned.readlink(), target_is_directory=pinned.is_dir()
        )
    elif pinned.is_dir():
        shutil.copytree(pinned, stage / "public" / "s", symlinks=True)


def _check_render_drift(repo: Path, collector: _Collector) -> None:
    check = "render.drift"
    collector.begin(check)
    try:
        with tempfile.TemporaryDirectory(prefix=".gel-registry-validate-") as directory:
            stage = Path(directory) / "repo"
            _copy_render_inputs(repo, stage)
            try:
                build_snapshot(stage)
            except (RenderError, OSError, ValueError) as exc:
                collector.add(
                    check,
                    _display_path(repo, repo / "public"),
                    f"fresh snapshot build failed: {exc}",
                )
            pointer_path = stage / "pointers" / "latest.json"
            if pointer_path.exists() and not pointer_path.is_symlink():
                try:
                    select_snapshot(stage)
                except (RenderError, OSError, ValueError) as exc:
                    collector.add(
                        check,
                        _display_path(repo, repo / "public" / "registry.json"),
                        f"fresh snapshot selection failed: {exc}",
                    )
                    collector.add(
                        check,
                        _display_path(repo, repo / "public" / "v1" / "snapshots.json"),
                        "fresh snapshot selection did not produce the moving pair",
                    )
            try:
                render_schemas(stage)
            except (RenderError, OSError, ValueError) as exc:
                collector.add(
                    check,
                    _display_path(repo, repo / "public"),
                    f"fresh support render failed: {exc}",
                )
            _compare_tree_bytes(
                repo, stage / "public", repo / "public", check, collector
            )
    except (OSError, ValueError) as exc:
        collector.add(check, _display_path(repo, repo), str(exc))


def _check_history(
    repo: Path,
    base: Path,
    collector: _Collector,
    roots: tuple[Path, ...] | None = None,
) -> None:
    check = "history.append_only"
    collector.begin(check)
    if not base.exists() or base.is_symlink() or not base.is_dir():
        collector.add(check, base, "merge-base tree is not a directory")
        return
    immutable_roots = roots or (
        _CAPTURE_REL,
        Path("bootstrap"),
        Path("releases"),
        Path("public/s"),
    )
    for relative_root in immutable_roots:
        base_root = base / relative_root
        head_root = repo / relative_root
        try:
            base_files = _tree_files(base_root)
            head_files = _tree_files(head_root)
        except (OSError, ValueError) as exc:
            collector.add(check, relative_root, str(exc))
            continue
        for relative in sorted(base_files):
            path = relative_root / relative
            if relative not in head_files:
                collector.add(check, path, "immutable path was deleted")
            elif head_files[relative] != base_files[relative]:
                collector.add(check, path, "immutable path changed")


def validate_capture_local(repo: Path, base: Path | None = None) -> ValidationReport:
    """Validate a capture PR before any publication state exists.

    Capture pull requests intentionally do not build a snapshot or select a
    pointer.  This gate therefore checks only the immutable capture evidence,
    normalized bootstrap files, and their append-only history.
    """

    repository = Path(repo)
    collector = _Collector()
    if repository.is_symlink() or not repository.is_dir():
        collector.add("repository", repository, "repository is not a directory")
        return collector.report()

    manifest = _load_capture(repository, collector)
    _check_capture_bodies(repository, manifest, collector)
    _check_bootstrap(repository, manifest, collector)

    normalization_check = "normalization.drift"
    collector.begin(normalization_check)
    capture_root = _capture_root(repository)
    bootstrap_root = repository / "bootstrap"
    if manifest is not None and bootstrap_root.exists():
        try:
            validate_normalization(capture_root, bootstrap_root)
        except (NormalizationError, OSError, ValueError) as exc:
            collector.add(
                normalization_check, _display_path(repository, bootstrap_root), str(exc)
            )
    else:
        collector.add(
            normalization_check,
            _display_path(repository, bootstrap_root),
            "normalization inputs are missing",
        )

    if base is not None:
        _check_history(
            repository,
            Path(base),
            collector,
            roots=(_CAPTURE_REL, Path("bootstrap")),
        )
    return collector.report()


def validate_local(repo: Path, base: Path | None = None) -> ValidationReport:
    """Run all deterministic integrity, history, and render checks locally."""

    repository = Path(repo)
    collector = _Collector()
    if repository.is_symlink() or not repository.is_dir():
        collector.add("repository", repository, "repository is not a directory")
        return collector.report()

    manifest = _load_capture(repository, collector)
    _check_capture_bodies(repository, manifest, collector)
    _check_bootstrap(repository, manifest, collector)
    _check_releases(repository, collector)
    _check_schemas(repository, collector)
    _check_pointer_and_snapshots(repository, collector)

    normalization_check = "normalization.drift"
    collector.begin(normalization_check)
    capture_root = _capture_root(repository)
    bootstrap_root = repository / "bootstrap"
    if manifest is not None and bootstrap_root.exists():
        try:
            validate_normalization(capture_root, bootstrap_root)
        except (NormalizationError, OSError, ValueError) as exc:
            collector.add(
                normalization_check, _display_path(repository, bootstrap_root), str(exc)
            )
    else:
        collector.add(
            normalization_check,
            _display_path(repository, bootstrap_root),
            "normalization inputs are missing",
        )

    _check_render_drift(repository, collector)
    if base is not None:
        _check_history(repository, Path(base), collector)
    return collector.report()


def _artifact_name(platform: str, encoding: str) -> str:
    suffix = ".exe" if platform.endswith("-windows-msvc") else ""
    name = f"gel-cli-{platform}{suffix}"
    return f"{name}.zst" if encoding == "zstd" else name


def _synthetic_release(record: ReleaseRecord) -> GitHubRelease:
    assets: list[GitHubAsset] = []
    for index, artifact in enumerate(record.artifacts, 1):
        name = _artifact_name(artifact.platform, artifact.encoding)
        assets.append(
            GitHubAsset(
                name=name,
                url=f"https://api.github.com/repos/{GITHUB_REPOSITORY}/assets/{index}",
                browser_download_url=artifact.url,
                id=index,
                size=artifact.size,
            )
        )
    return GitHubRelease(
        id=record.source.release_id,
        tag_name=record.source.release_tag,
        assets=tuple(assets),
        repository=record.source.repository,
    )


def _load_release_path(repository: Path, value: object) -> tuple[ReleaseRecord, Path]:
    path = Path(value) if isinstance(value, (str, Path)) else None
    if path is None:
        raise ValueError(f"unsupported changed release value: {value!r}")
    if not path.is_absolute():
        path = repository / path
    raw = _read_file(path)
    record = ReleaseRecord.model_validate_json(raw)
    return record, path


def _changed_release_pairs(
    repository: Path,
    changed_records: Iterable[object] | Mapping[object, object],
) -> list[tuple[GitHubRelease, ReleaseRecord | None, Path]]:
    values = (
        list(changed_records.values())
        if isinstance(changed_records, Mapping)
        else list(changed_records)
    )
    result: list[tuple[GitHubRelease, ReleaseRecord | None, Path]] = []
    for value in values:
        release: GitHubRelease | None = None
        record: ReleaseRecord | None = None
        path: Path | None = None
        if isinstance(value, tuple) and len(value) == 2:
            left, right = value
            if isinstance(left, GitHubRelease):
                release = left
            elif isinstance(right, GitHubRelease):
                release = right
            if isinstance(left, ReleaseRecord):
                record = left
            elif isinstance(right, ReleaseRecord):
                record = right
            if isinstance(left, (str, Path)):
                path = Path(left)
            elif isinstance(right, (str, Path)):
                path = Path(right)
        elif isinstance(value, GitHubRelease):
            release = value
        elif isinstance(value, ReleaseRecord):
            record = value
        elif isinstance(value, (str, Path)):
            path = Path(value)
        else:
            raise ValueError(f"unsupported changed release value: {value!r}")

        if record is None and path is not None:
            record, path = _load_release_path(repository, path)
        if record is None and release is not None:
            candidate = repository / "releases" / "gel-cli" / f"{release.version}.json"
            if candidate.is_file():
                record, path = _load_release_path(repository, candidate)
        if release is None and record is not None:
            release = _synthetic_release(record)
        if release is None:
            raise ValueError("changed release has no release metadata")
        if path is None:
            path = repository / "releases" / "gel-cli" / f"{release.version}.json"
        result.append((release, record, path))
    return result


def _compare_remote_record(expected: ReleaseRecord, observed: ReleaseRecord) -> None:
    expected_artifacts = {
        (artifact.platform, artifact.encoding): artifact
        for artifact in expected.artifacts
    }
    observed_artifacts = {
        (artifact.platform, artifact.encoding): artifact
        for artifact in observed.artifacts
    }
    if set(expected_artifacts) != set(observed_artifacts):
        raise VerificationError(
            "remote release artifact target set differs from the record"
        )
    for key in sorted(expected_artifacts):
        left = expected_artifacts[key]
        right = observed_artifacts[key]
        if (
            left.url != right.url
            or left.size != right.size
            or left.sha256 != right.sha256
            or left.blake2b != right.blake2b
        ):
            raise VerificationError(
                f"remote artifact differs from release record for {key[0]}/{key[1]}"
            )


def validate_release_remotes(
    repo: Path,
    changed_records: Iterable[object] | Mapping[object, object],
) -> ValidationReport:
    """Verify only the caller-supplied newly-added release records remotely."""

    repository = Path(repo)
    collector = _Collector()
    collector.begin("remote.release.scope")
    try:
        pairs = _changed_release_pairs(repository, changed_records)
    except (OSError, PydanticValidationError, ValueError) as exc:
        collector.add("remote.release.scope", "releases", str(exc))
        return collector.report()
    if not pairs:
        return collector.report()

    collector.begin("remote.release.bytes")
    collector.begin("remote.release.attestations")
    try:
        client = httpx.Client()
    except Exception as exc:
        collector.add(
            "remote.release.scope", "releases", f"could not create HTTP client: {exc}"
        )
        return collector.report()
    try:
        for release, record, path in pairs:
            label = _display_path(repository, path)
            with tempfile.TemporaryDirectory(
                prefix=".gel-registry-release-"
            ) as directory:
                destination = Path(directory) / "assets"
                try:
                    assets = download_assets(client, release, destination)
                    observed = verify_cli_release(release, assets)
                    if record is not None:
                        _compare_remote_record(record, observed)
                except Exception as exc:
                    collector.add("remote.release.bytes", label, str(exc))
    finally:
        with suppress(Exception):
            client.close()
    return collector.report()


__all__ = [
    "ValidationReport",
    "validate_capture_local",
    "validate_local",
    "validate_release_remotes",
]
