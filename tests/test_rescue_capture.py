"""Capture immutable legacy indexes from the retired package origin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from gel_registry.capture import CaptureError
from gel_registry.constants import (
    CAPTURE_ID,
    RESCUE_KNOWN_ABSENT_INDEXES,
    capture_urls,
)
from gel_registry.rescue.indexes import (
    RescueCaptureManifest,
    capture_rescue_indexes,
)

CAPTURED_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
EDGEDB_ORIGIN = "https://packages.edgedb.com"


def _index_bytes(
    package_index_data: dict[str, object], *, suffix: bytes = b""
) -> bytes:
    return b" \r\n" + json.dumps(package_index_data, indent=2).encode() + suffix + b"\n"


def _register_matrix(httpx_mock: HTTPXMock, body: bytes) -> None:
    for _channel, _platform, url in capture_urls(origin=EDGEDB_ORIGIN):
        httpx_mock.add_response(method="GET", url=url, status_code=200, content=body)


def _manifest_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "capture": CAPTURE_ID,
        "captured_at": "2026-08-31T12:00:00Z",
        "indexes": [
            {
                "source_url": url,
                "channel": channel,
                "platform": platform,
                "captured_at": "2026-08-31T12:00:00Z",
                "byte_size": 1,
                "sha256": "a" * 64,
            }
            for channel, platform, url in capture_urls(origin=EDGEDB_ORIGIN)
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.__setitem__("schema_version", 2),
        lambda value: value.__setitem__("capture", "other-capture"),
        lambda value: value.__setitem__("indexes", value["indexes"][:-1]),
        lambda value: value.__setitem__("indexes", list(reversed(value["indexes"]))),
        lambda value: value["indexes"][0].__setitem__(
            "source_url", capture_urls()[0][2]
        ),
        lambda value: value["indexes"][0].__setitem__(
            "captured_at", "2026-08-31T12:00:01Z"
        ),
    ],
)
def test_rescue_manifest_rejects_any_noncanonical_evidence_matrix(
    mutation: object,
) -> None:
    """Removing matrix checks must not let partial or Geldata evidence through."""

    data = _manifest_data()
    assert callable(mutation)
    mutation(data)

    with pytest.raises(ValueError):
        RescueCaptureManifest.model_validate(data)


def test_rescue_manifest_accepts_only_its_canonical_json_bytes() -> None:
    """Relaxing the canonical input check must not permit reformatted evidence."""

    noncanonical = json.dumps(_manifest_data(), separators=(",", ":")).encode()

    with pytest.raises(ValueError, match="canonical JSON"):
        RescueCaptureManifest.model_validate_json(noncanonical)


def test_rescue_capture_preserves_exact_edgedb_bytes_and_relative_references(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures/upstream/relative-index.json"
    body = fixture.read_bytes()
    _register_matrix(httpx_mock, body)

    with httpx.Client() as client:
        manifest = capture_rescue_indexes(client, tmp_path, CAPTURED_AT)

    root = tmp_path / "upstream/packages.edgedb.com" / CAPTURE_ID
    assert len(manifest.indexes) == 24
    assert (root / "indexes/stable-x86_64-unknown-linux-gnu.json").read_bytes() == body
    assert manifest.indexes[0].source_url.startswith(EDGEDB_ORIGIN)
    assert json.loads((root / "capture.json").read_bytes())["indexes"][0][
        "byte_size"
    ] == len(body)


def test_rescue_capture_removes_partial_data_after_any_failed_index(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls(origin=EDGEDB_ORIGIN)
    httpx_mock.add_response(
        method="GET",
        url=urls[0][2],
        status_code=200,
        content=_index_bytes(package_index_data),
    )
    httpx_mock.add_response(method="GET", url=urls[1][2], status_code=500)

    with httpx.Client() as client, pytest.raises(CaptureError, match="linux-musl"):
        capture_rescue_indexes(client, tmp_path, CAPTURED_AT)

    assert not (tmp_path / "upstream/packages.edgedb.com" / CAPTURE_ID).exists()


def test_rescue_capture_rejects_even_an_edgedb_redirect(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
) -> None:
    url = capture_urls(origin=EDGEDB_ORIGIN)[0][2]
    httpx_mock.add_response(
        method="GET",
        url=url,
        status_code=302,
        headers={"Location": url + "?redirected=1"},
    )

    with httpx.Client() as client, pytest.raises(CaptureError, match="redirect"):
        capture_rescue_indexes(client, tmp_path, CAPTURED_AT)

    assert not (tmp_path / "upstream/packages.edgedb.com" / CAPTURE_ID).exists()


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_rescue_capture_never_substitutes_different_geldata_content(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    edgedb = _index_bytes(package_index_data, suffix=b" ")
    geldata = _index_bytes(package_index_data, suffix=b"\t")
    _register_matrix(httpx_mock, edgedb)
    for _channel, _platform, url in capture_urls():
        httpx_mock.add_response(method="GET", url=url, status_code=200, content=geldata)

    with httpx.Client() as client:
        capture_rescue_indexes(client, tmp_path, CAPTURED_AT)

    saved = (
        tmp_path
        / "upstream/packages.edgedb.com"
        / CAPTURE_ID
        / "indexes/stable-x86_64-unknown-linux-gnu.json"
    )
    assert saved.read_bytes() == edgedb
    assert all(
        request.url.host == "packages.edgedb.com"
        for request in httpx_mock.get_requests()
    )


def _absent_url() -> str:
    channel, platform = next(iter(RESCUE_KNOWN_ABSENT_INDEXES))
    return next(
        url
        for entry_channel, entry_platform, url in capture_urls(origin=EDGEDB_ORIGIN)
        if (entry_channel, entry_platform) == (channel, platform)
    )


def test_rescue_capture_records_a_known_absent_index_without_keeping_a_file(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    """A cell upstream never published is evidence, not a capture failure.

    ``testing/aarch64-pc-windows-msvc`` answers a genuine S3 404 while all
    23 other cells answer 200, so requiring 24 retrievals would make the
    rescue capture impossible to run at all.
    """

    absent_url = _absent_url()
    body = _index_bytes(package_index_data)
    for _channel, _platform, url in capture_urls(origin=EDGEDB_ORIGIN):
        if url == absent_url:
            httpx_mock.add_response(method="GET", url=url, status_code=404)
        else:
            httpx_mock.add_response(
                method="GET", url=url, status_code=200, content=body
            )

    with httpx.Client() as client:
        manifest = capture_rescue_indexes(client, tmp_path, CAPTURED_AT)

    assert len(manifest.indexes) == 23
    assert len(manifest.absent) == 1
    absence = manifest.absent[0]
    assert (absence.channel, absence.platform) == ("testing", "aarch64-pc-windows-msvc")
    assert absence.observed_status == 404
    assert absence.source_url == absent_url

    root = tmp_path / "upstream/packages.edgedb.com" / CAPTURE_ID
    assert not (root / "indexes/testing-aarch64-pc-windows-msvc.json").exists()
    assert len(list((root / "indexes").iterdir())) == 23
    # The absence is durable in the committed manifest, not just in memory.
    reloaded = RescueCaptureManifest.model_validate_json(
        (root / "capture.json").read_bytes()
    )
    assert reloaded.absent == manifest.absent


def test_rescue_capture_still_fails_on_a_404_for_any_other_index(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
) -> None:
    """Tolerating one known hole must not become tolerating any hole."""

    channel, platform, url = capture_urls(origin=EDGEDB_ORIGIN)[0]
    assert (channel, platform) not in RESCUE_KNOWN_ABSENT_INDEXES
    httpx_mock.add_response(method="GET", url=url, status_code=404)

    with httpx.Client() as client, pytest.raises(CaptureError, match="observed 404"):
        capture_rescue_indexes(client, tmp_path, CAPTURED_AT)

    assert not (tmp_path / "upstream/packages.edgedb.com" / CAPTURE_ID).exists()


def test_rescue_capture_keeps_a_known_absent_index_that_reappears(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    """The list tolerates an absence; it never requires one."""

    _register_matrix(httpx_mock, _index_bytes(package_index_data))

    with httpx.Client() as client:
        manifest = capture_rescue_indexes(client, tmp_path, CAPTURED_AT)

    assert len(manifest.indexes) == 24
    assert manifest.absent == ()


def test_rescue_manifest_rejects_an_absence_that_is_not_known_upstream() -> None:
    data = _manifest_data()
    indexes = list(data["indexes"])  # type: ignore[call-overload]
    moved = indexes.pop(0)
    data["indexes"] = indexes
    data["absent"] = [
        {
            "source_url": moved["source_url"],
            "channel": moved["channel"],
            "platform": moved["platform"],
            "captured_at": "2026-08-31T12:00:00Z",
            "observed_status": 404,
        }
    ]

    with pytest.raises(ValueError, match="not known upstream"):
        RescueCaptureManifest.model_validate(data)
