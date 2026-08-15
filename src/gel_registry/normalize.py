"""Pure, local normalization of a frozen legacy capture."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from pydantic import ValidationError

from .constants import CAPTURE_ID, ORIGIN
from .digest import canonical_json, hash_bytes
from .schema import CaptureManifest, PackageIndex

_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_AT_FDCWD = -100
_PACKAGE_HOST = "packages.geldata.com"


class NormalizationError(RuntimeError):
    """Raised when a frozen capture cannot be normalized safely."""


def _encoded_path(path: Path) -> bytes:
    encoded = os.fsencode(str(path))
    if b"\x00" in encoded:
        raise ValueError("normalization path contains an embedded NUL byte")
    return encoded


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a directory without replacing a destination."""

    source_bytes = _encoded_path(source)
    destination_bytes = _encoded_path(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            renamex_np = libc.renamex_np
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "renamex_np is unavailable") from exc
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, destination_bytes, _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        try:
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable") from exc
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def _load_manifest(capture_root: Path) -> CaptureManifest:
    if capture_root.is_symlink() or not capture_root.is_dir():
        raise NormalizationError(f"capture root is not a directory: {capture_root}")
    manifest_path = capture_root / "capture.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise NormalizationError(f"capture manifest is missing: {manifest_path}")
    try:
        manifest = CaptureManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise NormalizationError(f"invalid capture manifest: {exc}") from exc
    if manifest.capture != CAPTURE_ID or manifest.origin != ORIGIN:
        raise NormalizationError("capture manifest does not use the reviewed source")
    return manifest


def _tree_paths(root: Path) -> set[str]:
    """Return every relative path in a tree, rejecting symlinks."""

    if not root.exists():
        return set()
    if root.is_symlink() or not root.is_dir():
        raise NormalizationError(f"normalization root is not a directory: {root}")
    paths: set[str] = set()
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                raise NormalizationError(
                    f"normalization tree contains symlink: {relative}"
                )
            paths.add(path.relative_to(root).as_posix())
    except OSError as exc:
        raise NormalizationError(
            f"could not inspect normalization tree: {exc}"
        ) from exc
    return paths


def _expected_capture_paths(manifest: CaptureManifest) -> set[str]:
    expected = {"capture.json", "indexes"}
    for entry in manifest.entries:
        if entry.status == 200:
            if entry.path is None:
                raise NormalizationError(
                    f"{entry.channel}/{entry.platform}: successful entry has "
                    "no body path"
                )
            expected.add(entry.path)
    return expected


def _verify_capture_tree(capture_root: Path, manifest: CaptureManifest) -> None:
    expected = _expected_capture_paths(manifest)
    actual = _tree_paths(capture_root)
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra or missing:
        details: list[str] = []
        if extra:
            details.append("extra=" + ",".join(extra))
        if missing:
            details.append("missing=" + ",".join(missing))
        raise NormalizationError(
            "capture tree is not the recorded closed world: " + "; ".join(details)
        )


def _verify_body_hashes(
    capture_root: Path, manifest: CaptureManifest
) -> dict[str, bytes]:
    """Read and verify every body before attempting to parse any JSON."""

    snapshots: dict[str, bytes] = {}
    for entry in manifest.entries:
        if entry.status == 404:
            continue
        assert entry.path is not None
        body_path = capture_root / entry.path
        if body_path.is_symlink() or not body_path.is_file():
            raise NormalizationError(
                f"{entry.channel}/{entry.platform}: missing captured body {entry.path}"
            )
        try:
            body = body_path.read_bytes()
        except OSError as exc:
            raise NormalizationError(
                f"{entry.channel}/{entry.platform}: could not read {entry.path}: {exc}"
            ) from exc
        digests = hash_bytes(body)
        if (
            digests.size != entry.size
            or digests.sha256 != entry.sha256
            or digests.blake2b != entry.blake2b
        ):
            raise NormalizationError(
                f"{entry.channel}/{entry.platform}: captured body digest mismatch "
                f"(expected size={entry.size} sha256={entry.sha256} "
                f"blake2b={entry.blake2b}; observed size={digests.size} "
                f"sha256={digests.sha256} blake2b={digests.blake2b})"
            )
        snapshots[entry.path] = body
    return snapshots


def _resolve_installref(base_url: str, reference: str, label: str) -> str:
    try:
        resolved = urljoin(base_url, reference)
        parsed = urlsplit(resolved)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise NormalizationError(
            f"{label}: malformed installref URL {reference!r}"
        ) from exc
    if parsed.scheme != "https" or hostname != _PACKAGE_HOST:
        raise NormalizationError(
            f"{label}: installref resolves outside {_PACKAGE_HOST}: {resolved!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise NormalizationError(f"{label}: installref URL must not contain userinfo")
    if "#" in resolved or parsed.fragment:
        raise NormalizationError(f"{label}: installref URL must not contain a fragment")
    if port not in (None, 443):
        raise NormalizationError(
            f"{label}: installref URL must use the default HTTPS port"
        )
    return resolved


def _normalize_index(body: bytes, entry_url: str, label: str) -> PackageIndex:
    try:
        index = PackageIndex.model_validate_json(body)
    except (ValidationError, ValueError) as exc:
        raise NormalizationError(
            f"{label}: captured body is not a package index: {exc}"
        ) from exc

    packages = []
    for package in index.packages:
        installrefs = tuple(
            reference.model_copy(
                update={
                    "ref": _resolve_installref(
                        entry_url,
                        reference.ref,
                        f"{label} {package.basename} "
                        f"{reference.encoding or 'identity'}",
                    )
                }
            )
            for reference in package.installrefs
        )
        packages.append(
            package.model_copy(
                update={
                    "installref": _resolve_installref(
                        entry_url,
                        package.installref,
                        f"{label} {package.basename} installref",
                    ),
                    "installrefs": installrefs,
                }
            )
        )
    return PackageIndex(packages=tuple(packages))


def _normalize_to_stage(capture_root: Path, stage: Path) -> tuple[str, ...]:
    manifest = _load_manifest(capture_root)
    _verify_capture_tree(capture_root, manifest)
    body_snapshots = _verify_body_hashes(capture_root, manifest)

    output_names: list[str] = []
    for entry in manifest.entries:
        if entry.status == 404:
            continue
        assert entry.path is not None
        label = f"{entry.channel}/{entry.platform}"
        index = _normalize_index(body_snapshots[entry.path], entry.url, label)
        output_name = f"{entry.channel}-{entry.platform}.json"
        (stage / output_name).write_bytes(canonical_json(index))
        output_names.append(output_name)
    return tuple(output_names)


def _compare_trees(
    candidate: Path, existing: Path
) -> tuple[list[str], list[str], list[str]]:
    candidate_paths = _tree_paths(candidate)
    existing_paths = _tree_paths(existing)
    added = sorted(existing_paths - candidate_paths)
    missing = sorted(candidate_paths - existing_paths)
    changed: list[str] = []
    for relative in sorted(candidate_paths & existing_paths):
        candidate_path = candidate / relative
        existing_path = existing / relative
        if candidate_path.is_dir() or existing_path.is_dir():
            if not (candidate_path.is_dir() and existing_path.is_dir()):
                changed.append(relative)
            continue
        try:
            if candidate_path.read_bytes() != existing_path.read_bytes():
                changed.append(relative)
        except OSError as exc:
            raise NormalizationError(
                f"could not compare normalized file {relative}: {exc}"
            ) from exc
    return added, missing, changed


def _drift_error(
    added: list[str],
    missing: list[str],
    changed: list[str],
) -> NormalizationError | None:
    if not (added or missing or changed):
        return None
    details: list[str] = []
    if added:
        details.append("added=" + ",".join(added))
    if missing:
        details.append("missing=" + ",".join(missing))
    if changed:
        details.append("changed=" + ",".join(changed))
    return NormalizationError("normalization drift: " + "; ".join(details))


def normalize_capture(capture_root: Path, output_root: Path) -> tuple[Path, ...]:
    """Normalize captured indexes into an append-only bootstrap directory."""

    capture_root = Path(capture_root)
    output_root = Path(output_root)
    try:
        parent = output_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=str(parent)))
    except (OSError, ValueError) as exc:
        raise NormalizationError(
            f"could not create normalization staging tree: {exc}"
        ) from exc

    try:
        output_names = _normalize_to_stage(capture_root, stage)
        if output_root.exists() or output_root.is_symlink():
            added, missing, changed = _compare_trees(stage, output_root)
            drift = _drift_error(added, missing, changed)
            if drift is not None:
                raise drift
        else:
            try:
                _rename_noreplace(stage, output_root)
            except OSError as exc:
                raise NormalizationError(
                    f"could not atomically install normalized bootstrap: {exc}"
                ) from exc
        return tuple(output_root / name for name in output_names)
    except NormalizationError:
        raise
    except (OSError, ValueError, ValidationError) as exc:
        raise NormalizationError(f"normalization failed: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def validate_normalization(capture_root: Path, bootstrap_root: Path) -> None:
    """Check committed bootstrap bytes against a freshly normalized capture."""

    capture_root = Path(capture_root)
    bootstrap_root = Path(bootstrap_root)
    temporary_parent = Path(tempfile.mkdtemp(prefix=".gel-registry-normalize-"))
    stage = temporary_parent / "bootstrap"
    try:
        stage.mkdir()
        _normalize_to_stage(capture_root, stage)
        added, missing, changed = _compare_trees(stage, bootstrap_root)
        drift = _drift_error(added, missing, changed)
        if drift is not None:
            raise drift
    except NormalizationError:
        raise
    except (OSError, ValueError, ValidationError) as exc:
        raise NormalizationError(f"normalization validation failed: {exc}") from exc
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)


__all__ = [
    "NormalizationError",
    "normalize_capture",
    "validate_normalization",
]
