"""Turning captured bytes into canonical, self-contained bootstrap indexes.

Normalization rehashes every captured body before it parses it, resolves each
relative installref against the URL it was actually fetched from, and writes
canonical JSON. It refuses anything it cannot account for — a reference that
escapes the origin, a body whose digest disagrees with the manifest, a file the
manifest never recorded — and it refuses without leaving output behind.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import pytest

from gel_registry import __main__ as cli
from gel_registry.constants import CAPTURE_ID, ORIGIN, capture_urls
from gel_registry.contracts import CaptureEntry, CaptureManifest, PackageIndex
from gel_registry.digest import canonical_json, hash_bytes
from gel_registry.normalize import (
    NormalizationError,
    _compare_trees,
    normalize_capture,
)
from gel_registry.storage import read_tree

CAPTURED_AT = datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "upstream" / "relative-index.json"
BODY_PATH = "indexes/stable-x86_64-unknown-linux-gnu.json"


def _fixture_body() -> bytes:
    return FIXTURE.read_bytes()


def _with_first_installref(ref: str) -> bytes:
    """Return the fixture body with the first package's first installref replaced."""

    data = json.loads(_fixture_body())
    data["packages"][0]["installrefs"][0]["ref"] = ref
    return json.dumps(data, indent=2).encode() + b"\n"


def _write_capture(root: Path, body: bytes, *, sha256: str | None = None) -> None:
    digests = hash_bytes(body)
    entries = [
        CaptureEntry(
            channel=channel,
            platform=platform,
            url=url,
            status=200,
            final_url=url,
            size=digests.size,
            sha256=sha256 or digests.sha256,
            blake2b=digests.blake2b,
            path=BODY_PATH,
        )
        if index == 0
        else CaptureEntry(channel=channel, platform=platform, url=url, status=404)
        for index, (channel, platform, url) in enumerate(capture_urls())
    ]
    (root / BODY_PATH).parent.mkdir(parents=True)
    (root / BODY_PATH).write_bytes(body)
    (root / "capture.json").write_bytes(
        canonical_json(
            CaptureManifest(
                capture=CAPTURE_ID,
                origin=ORIGIN,
                captured_at=CAPTURED_AT,
                entries=tuple(entries),
            )
        )
    )


def test_normalization_preserves_every_field_and_resolves_references(
    tmp_path: Path,
) -> None:
    body = _fixture_body()
    capture_root = tmp_path / "capture"
    _write_capture(capture_root, body)
    output_root = tmp_path / "bootstrap"

    paths = normalize_capture(capture_root, output_root)

    assert paths == (output_root / "stable-x86_64-unknown-linux-gnu.json",)
    base = capture_urls()[0][2]
    source = PackageIndex.model_validate_json(body)
    expected = PackageIndex(
        packages=tuple(
            entry.model_copy(
                update={
                    "installref": urljoin(base, entry.installref),
                    "installrefs": tuple(
                        reference.model_copy(
                            update={"ref": urljoin(base, reference.ref)}
                        )
                        for reference in entry.installrefs
                    ),
                }
            )
            for entry in source.packages
        )
    )
    normalized = PackageIndex.model_validate_json(paths[0].read_bytes())
    assert normalized == expected
    assert paths[0].read_bytes() == canonical_json(normalized)

    # Upstream's own verification data and tags survive untouched, and only the
    # references are rewritten.
    first = normalized.packages[0]
    assert first.tags == {"channel": "stable", "featured": True}
    assert (first.installrefs[0].encoding, first.installrefs[1].encoding) == (
        "identity",
        "zstd",
    )
    assert first.installrefs[0].verification.sha256 is None
    assert first.installrefs[1].verification.sha256 is not None
    assert normalized.packages[1].installrefs[1].verification.sha256 is None
    assert "?" not in first.installrefs[0].ref


def test_normalization_preserves_a_query_on_a_resolved_reference(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "capture"
    _write_capture(
        capture_root, _with_first_installref("../artifacts/gel-cli?download=1")
    )

    paths = normalize_capture(capture_root, tmp_path / "bootstrap")

    normalized = PackageIndex.model_validate_json(paths[0].read_bytes())
    assert normalized.packages[0].installrefs[0].ref == (
        f"{ORIGIN}/archive/artifacts/gel-cli?download=1"
    )


def _write_an_unrecorded_body(root: Path) -> None:
    _write_capture(root, _fixture_body())
    (root / "indexes" / "unrecorded.json").write_bytes(_fixture_body())


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        pytest.param(
            lambda root: _write_capture(
                root, _with_first_installref("https://evil.example/archive/x")
            ),
            "",
            id="off-origin-installref",
        ),
        pytest.param(
            lambda root: _write_capture(root, _fixture_body(), sha256="0" * 64),
            "sha256",
            id="digest-mismatch",
        ),
        pytest.param(_write_an_unrecorded_body, "extra", id="unrecorded-body"),
    ],
)
def test_normalization_rejects_unaccountable_evidence_atomically(
    tmp_path: Path,
    prepare: Callable[[Path], None],
    message: str,
) -> None:
    capture_root = tmp_path / "capture"
    prepare(capture_root)
    output_root = tmp_path / "bootstrap"

    with pytest.raises(NormalizationError, match=message or None):
        normalize_capture(capture_root, output_root)

    assert not output_root.exists()


def test_normalize_check_reports_drift_without_touching_committed_bytes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    capture_root = tmp_path / "upstream" / "packages.geldata.com" / CAPTURE_ID
    _write_capture(capture_root, _fixture_body())
    normalize_capture(capture_root, tmp_path / "bootstrap")

    assert cli.main(["normalize", "--repo", str(tmp_path), "--check"]) == 0

    before = read_tree(tmp_path / "bootstrap")
    output = tmp_path / "bootstrap" / "stable-x86_64-unknown-linux-gnu.json"
    output.write_bytes(output.read_bytes() + b"\n")
    drifted = read_tree(tmp_path / "bootstrap")

    assert cli.main(["normalize", "--repo", str(tmp_path), "--check"]) == 1
    assert "changed" in capsys.readouterr().err
    assert read_tree(tmp_path / "bootstrap") == drifted != before


def test_drift_buckets_are_named_from_the_fresh_render(tmp_path: Path) -> None:
    """``added``/``missing`` describe the candidate, which is the fresh render."""

    candidate = tmp_path / "candidate"
    existing = tmp_path / "existing"
    candidate.mkdir()
    existing.mkdir()
    (candidate / "only-rendered.json").write_bytes(b"{}\n")
    (existing / "only-committed.json").write_bytes(b"{}\n")

    added, missing, changed = _compare_trees(candidate, existing)

    assert added == ["only-rendered.json"]
    assert missing == ["only-committed.json"]
    assert changed == []
