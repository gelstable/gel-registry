from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import pytest

from gel_registry.constants import CAPTURE_ID, ORIGIN, capture_urls
from gel_registry.contracts import CaptureEntry, CaptureManifest, PackageIndex
from gel_registry.digest import canonical_json, hash_bytes
from gel_registry.normalize import (
    NormalizationError,
    normalize_capture,
    validate_normalization,
)

CAPTURED_AT = datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "upstream" / "relative-index.json"


def _make_manifest(body: bytes, *, sha256: str | None = None) -> CaptureManifest:
    digests = hash_bytes(body)
    entries: list[CaptureEntry] = []
    for index, (channel, platform, url) in enumerate(capture_urls()):
        if index == 0:
            entries.append(
                CaptureEntry(
                    channel=channel,
                    platform=platform,
                    url=url,
                    status=200,
                    final_url=url,
                    size=digests.size,
                    sha256=digests.sha256 if sha256 is None else sha256,
                    blake2b=digests.blake2b,
                    path=f"indexes/{channel}-{platform}.json",
                )
            )
        else:
            entries.append(
                CaptureEntry(
                    channel=channel,
                    platform=platform,
                    url=url,
                    status=404,
                )
            )
    return CaptureManifest(
        capture=CAPTURE_ID,
        origin=ORIGIN,
        captured_at=CAPTURED_AT,
        entries=tuple(entries),
    )


def _write_capture(
    root: Path,
    body: bytes,
    *,
    sha256: str | None = None,
) -> CaptureManifest:
    manifest = _make_manifest(body, sha256=sha256)
    body_path = root / "indexes/stable-x86_64-unknown-linux-gnu.json"
    body_path.parent.mkdir(parents=True)
    body_path.write_bytes(body)
    (root / "capture.json").write_bytes(canonical_json(manifest))
    return manifest


def _fixture_body() -> bytes:
    return FIXTURE.read_bytes()


def test_normalize_capture_preserves_every_package_field_and_resolves_urls(
    tmp_path: Path,
) -> None:
    body = _fixture_body()
    capture_root = tmp_path / "capture"
    manifest = _write_capture(capture_root, body)
    output_root = tmp_path / "bootstrap"

    paths = normalize_capture(capture_root, output_root)

    assert paths == (output_root / "stable-x86_64-unknown-linux-gnu.json",)
    source = PackageIndex.model_validate_json(body)
    normalized = PackageIndex.model_validate_json(paths[0].read_bytes())
    expected_packages = []
    for package in source.packages:
        expected_refs = tuple(
            reference.model_copy(
                update={
                    "ref": urljoin(manifest.entries[0].url, reference.ref),
                }
            )
            for reference in package.installrefs
        )
        expected_packages.append(
            package.model_copy(
                update={
                    "installref": urljoin(manifest.entries[0].url, package.installref),
                    "installrefs": expected_refs,
                }
            )
        )
    assert normalized == PackageIndex(packages=tuple(expected_packages))
    assert normalized.packages[0].tags == {
        "channel": "stable",
        "featured": True,
    }
    assert normalized.packages[0].installrefs[0].verification.sha256 is None
    assert normalized.packages[0].installrefs[1].verification.sha256 is not None
    assert normalized.packages[1].installrefs[0].verification.sha256 is None
    assert normalized.packages[1].installrefs[1].verification.sha256 is None
    assert normalized.packages[0].installrefs[0].encoding == "identity"
    assert normalized.packages[0].installrefs[1].encoding == "zstd"
    assert "?" not in normalized.packages[0].installrefs[0].ref


def test_normalize_capture_preserves_resolved_installref_query(tmp_path: Path) -> None:
    data = json.loads(_fixture_body())
    assert isinstance(data, dict)
    packages = data["packages"]
    assert isinstance(packages, list)
    first_package = packages[0]
    assert isinstance(first_package, dict)
    references = first_package["installrefs"]
    assert isinstance(references, list)
    first_reference = references[0]
    assert isinstance(first_reference, dict)
    first_reference["ref"] = "../artifacts/gel-cli-1.2.3?download=1"
    body = json.dumps(data, indent=2).encode() + b"\n"
    capture_root = tmp_path / "capture"
    _write_capture(capture_root, body)

    paths = normalize_capture(capture_root, tmp_path / "bootstrap")

    normalized = PackageIndex.model_validate_json(paths[0].read_bytes())
    assert (
        normalized.packages[0]
        .installrefs[0]
        .ref.endswith("/archive/artifacts/gel-cli-1.2.3?download=1")
    )


def test_normalize_capture_rejects_unsafe_resolved_installrefs_atomically(
    tmp_path: Path,
) -> None:
    unsafe_ref = "https://evil.example/archive/x"
    data = json.loads(_fixture_body())
    assert isinstance(data, dict)
    packages = data["packages"]
    assert isinstance(packages, list)
    first_package = packages[0]
    assert isinstance(first_package, dict)
    references = first_package["installrefs"]
    assert isinstance(references, list)
    first_reference = references[0]
    assert isinstance(first_reference, dict)
    first_reference["ref"] = unsafe_ref
    body = json.dumps(data, indent=2).encode() + b"\n"
    capture_root = tmp_path / "capture"
    _write_capture(capture_root, body)
    output_root = tmp_path / "bootstrap"

    with pytest.raises(NormalizationError):
        normalize_capture(capture_root, output_root)

    assert not output_root.exists()


def test_normalize_capture_rejects_digest_mismatch_without_partial_output(
    tmp_path: Path,
) -> None:
    body = _fixture_body()
    capture_root = tmp_path / "capture"
    _write_capture(capture_root, body, sha256="0" * 64)
    output_root = tmp_path / "bootstrap"

    with pytest.raises(NormalizationError, match="sha256"):
        normalize_capture(capture_root, output_root)

    assert not output_root.exists()


def test_normalize_capture_rejects_extra_unrecorded_body_atomically(
    tmp_path: Path,
) -> None:
    body = _fixture_body()
    capture_root = tmp_path / "capture"
    _write_capture(capture_root, body)
    (capture_root / "indexes/unrecorded.json").write_bytes(body)
    output_root = tmp_path / "bootstrap"

    with pytest.raises(NormalizationError, match="extra"):
        normalize_capture(capture_root, output_root)

    assert not output_root.exists()


def test_validate_normalization_reports_changed_bootstrap_bytes(tmp_path: Path) -> None:
    body = _fixture_body()
    capture_root = tmp_path / "capture"
    _write_capture(capture_root, body)
    output_root = tmp_path / "bootstrap"
    normalize_capture(capture_root, output_root)
    output_path = output_root / "stable-x86_64-unknown-linux-gnu.json"
    output_path.write_bytes(output_path.read_bytes() + b"\n")

    with pytest.raises(NormalizationError, match="changed"):
        validate_normalization(capture_root, output_root)
