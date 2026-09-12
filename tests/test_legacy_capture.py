"""Capturing the legacy package index as immutable evidence.

The capture step fetches a fixed 24-entry matrix of upstream URLs twice: once
to record them and once to recheck that nothing moved underneath it. It is
fail-closed by construction — any host, scheme, redirect, status, or byte that
does not match what was recorded aborts the run, and an aborted run must leave
no destination behind for a later step to mistake for evidence.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from gel_registry import __main__ as cli
from gel_registry import storage
from gel_registry.capture import (
    CaptureError,
    _origin_url,
    capture_legacy,
    verify_live_capture,
)
from gel_registry.constants import CAPTURE_ID, ORIGIN, capture_urls
from gel_registry.contracts import CaptureEntry, CaptureManifest
from gel_registry.digest import canonical_json

CAPTURED_AT = datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)
USER_AGENT = "gel-registry-importer/1"

Setup = Callable[[HTTPXMock, pytest.MonkeyPatch, Path, bytes], None]


def test_capture_origin_configuration_rejects_unapproved_hosts_before_fetching() -> (
    None
):
    """Removing the allowlist must not permit capture requests to arbitrary hosts."""

    with pytest.raises(CaptureError, match="allowlisted"):
        _origin_url("https://example.com/index.json", origin_url="https://example.com")
    with pytest.raises(ValueError, match="allowlisted"):
        capture_urls(origin="https://example.com")


def _index_bytes(package_index_data: dict[str, object], *, pretty: bool) -> bytes:
    """Return upstream bytes in one of the two shapes the origin serves."""

    if pretty:
        return (
            b" \r\n"
            + json.dumps(package_index_data, indent=2, ensure_ascii=False).encode()
            + b"\n\t"
        )
    return (
        b'\n { "packages": '
        + json.dumps(package_index_data["packages"], separators=(",", ":")).encode()
        + b" } \r\n"
    )


def _register_matrix(
    httpx_mock: HTTPXMock, bodies: dict[str, bytes], *, validators: bool = False
) -> None:
    for index, (_channel, _platform, url) in enumerate(capture_urls()):
        if url in bodies:
            httpx_mock.add_response(
                method="GET",
                url=url,
                status_code=200,
                content=bodies[url],
                headers={"ETag": f'"capture-{index}"'} if validators else {},
            )
        else:
            httpx_mock.add_response(method="GET", url=url, status_code=404)


def _capture(destination: Path) -> CaptureManifest:
    with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
        return capture_legacy(client, destination, CAPTURED_AT)


def test_capture_records_the_whole_matrix_with_exact_bytes_and_rechecks(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    bodies = {
        urls[0][2]: _index_bytes(package_index_data, pretty=False),
        urls[16][2]: _index_bytes(package_index_data, pretty=True),
    }
    _register_matrix(httpx_mock, bodies, validators=True)
    _register_matrix(httpx_mock, bodies)

    destination = tmp_path / "capture"
    manifest = _capture(destination)

    requests = httpx_mock.get_requests()
    assert [str(request.url) for request in requests] == [url for _, _, url in urls] * 2
    assert all(request.headers["user-agent"] == USER_AGENT for request in requests)
    assert requests[24].headers["if-none-match"] == '"capture-0"'
    assert requests[24 + 16].headers["if-none-match"] == '"capture-16"'

    assert manifest.captured_at == CAPTURED_AT
    assert [(entry.channel, entry.platform) for entry in manifest.entries] == [
        (channel, platform) for channel, platform, _ in urls
    ]
    successful = [entry for entry in manifest.entries if entry.status == 200]
    assert len(successful) == 2
    for entry in successful:
        assert entry.final_url == entry.url
        assert entry.etag is not None
        assert entry.size == len(bodies[entry.url])
        assert entry.path == f"indexes/{entry.channel}-{entry.platform}.json"
        assert (destination / entry.path).read_bytes() == bodies[entry.url]
    assert all(
        entry.path is None and entry.size is None and entry.sha256 is None
        for entry in manifest.entries
        if entry.status == 404
    )
    assert (destination / "capture.json").read_bytes() == canonical_json(manifest)

    # The manifest is the matrix: it sorts back into matrix order whatever the
    # order it was built from, and it refuses to describe a partial matrix.
    entries = [
        CaptureEntry(channel=channel, platform=platform, url=url, status=404)
        for channel, platform, url in reversed(urls)
    ]
    common = {"capture": CAPTURE_ID, "origin": ORIGIN, "captured_at": CAPTURED_AT}
    assert [
        (entry.channel, entry.platform)
        for entry in CaptureManifest(entries=entries, **common).entries
    ] == [(channel, platform) for channel, platform, _ in urls]
    with pytest.raises(ValueError):
        CaptureManifest(entries=entries[:-1], **common)


def _first_status(status: int) -> Setup:
    def setup(
        httpx_mock: HTTPXMock,
        _monkeypatch: pytest.MonkeyPatch,
        _root: Path,
        _body: bytes,
    ) -> None:
        httpx_mock.add_response(
            method="GET", url=capture_urls()[0][2], status_code=status
        )

    return setup


def _first_response(
    status: int, *, headers: dict[str, str] | None = None, content: bytes | None = None
) -> Setup:
    def setup(
        httpx_mock: HTTPXMock,
        _monkeypatch: pytest.MonkeyPatch,
        _root: Path,
        _body: bytes,
    ) -> None:
        httpx_mock.add_response(
            method="GET",
            url=capture_urls()[0][2],
            status_code=status,
            headers=headers or {},
            content=content,
        )

    return setup


def _exception(error: Exception) -> Setup:
    def setup(
        httpx_mock: HTTPXMock,
        _monkeypatch: pytest.MonkeyPatch,
        _root: Path,
        _body: bytes,
    ) -> None:
        httpx_mock.add_exception(error)

    return setup


def _unconditional_304(
    httpx_mock: HTTPXMock, _monkeypatch: pytest.MonkeyPatch, _root: Path, body: bytes
) -> None:
    """The origin answers a recheck with 304 without ever offering a validator."""

    _register_matrix(httpx_mock, {capture_urls()[0][2]: body})
    httpx_mock.add_response(method="GET", url=capture_urls()[0][2], status_code=304)


def _recheck_changes_bytes(
    httpx_mock: HTTPXMock, _monkeypatch: pytest.MonkeyPatch, _root: Path, body: bytes
) -> None:
    _register_matrix(httpx_mock, {capture_urls()[0][2]: body})
    httpx_mock.add_response(
        method="GET", url=capture_urls()[0][2], status_code=200, content=body + b"\n"
    )


def _recheck_changes_status(
    httpx_mock: HTTPXMock, _monkeypatch: pytest.MonkeyPatch, _root: Path, body: bytes
) -> None:
    """An entry absent during capture is present during the recheck."""

    urls = capture_urls()
    _register_matrix(httpx_mock, {urls[0][2]: body})
    httpx_mock.add_response(method="GET", url=urls[0][2], status_code=200, content=body)
    httpx_mock.add_response(method="GET", url=urls[1][2], status_code=200, content=body)


def _existing_destination(
    _httpx_mock: HTTPXMock, _monkeypatch: pytest.MonkeyPatch, root: Path, _body: bytes
) -> None:
    (root / "capture").mkdir()
    (root / "capture" / "marker").write_text("keep")


def _nul_in_destination(
    _httpx_mock: HTTPXMock, _monkeypatch: pytest.MonkeyPatch, _root: Path, _body: bytes
) -> None:
    return None


def _racing_destination(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, _root: Path, body: bytes
) -> None:
    """Another writer wins the destination between the check and the install."""

    _register_matrix(httpx_mock, {capture_urls()[0][2]: body})
    _register_matrix(httpx_mock, {capture_urls()[0][2]: body})
    real_rename = storage.rename_noreplace

    def create_destination_then_rename(source: Path, destination: Path) -> None:
        destination.mkdir()
        real_rename(source, destination)

    monkeypatch.setattr(storage, "rename_noreplace", create_destination_then_rename)


@pytest.mark.parametrize(
    ("setup", "message", "leftover"),
    [
        pytest.param(_first_status(403), "", "none", id="forbidden"),
        pytest.param(_first_status(429), "", "none", id="rate-limited"),
        pytest.param(_first_status(500), "", "none", id="server-error"),
        pytest.param(
            _exception(httpx.ReadTimeout("read timed out")), "", "none", id="timeout"
        ),
        pytest.param(
            _exception(httpx.ConnectError("TLS failure")),
            "",
            "none",
            id="transport-error",
        ),
        pytest.param(
            _first_response(
                302, headers={"Location": "https://evil.example/index.json"}
            ),
            "",
            "none",
            id="off-origin-redirect",
        ),
        pytest.param(
            _first_response(
                302, headers={"Location": "http://packages.geldata.com/index.json"}
            ),
            "",
            "none",
            id="downgraded-scheme-redirect",
        ),
        pytest.param(
            _first_response(200, content=b'{"packages": [}'),
            "",
            "none",
            id="malformed-json",
        ),
        pytest.param(
            _unconditional_304,
            "expected status 200, observed 304",
            "none",
            id="bare-304",
        ),
        pytest.param(
            _recheck_changes_bytes,
            "stable.*x86_64-unknown-linux-gnu",
            "none",
            id="recheck-byte-change",
        ),
        pytest.param(
            _recheck_changes_status,
            "stable.*x86_64-unknown-linux-musl",
            "none",
            id="recheck-status-flip",
        ),
        pytest.param(
            _existing_destination, "already exists", "kept", id="existing-destination"
        ),
        pytest.param(_nul_in_destination, "NUL|null", "none", id="nul-in-destination"),
        pytest.param(_racing_destination, "", "kept", id="racing-destination"),
    ],
)
def test_capture_fails_closed_without_installing_a_destination(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    package_index_data: dict[str, object],
    setup: Setup,
    message: str,
    leftover: str,
) -> None:
    body = _index_bytes(package_index_data, pretty=False)
    setup(httpx_mock, monkeypatch, tmp_path, body)
    destination = tmp_path / "capture"
    if setup is _nul_in_destination:
        destination = tmp_path / "capture\x00ignored"

    with pytest.raises(CaptureError, match=message or None):
        _capture(destination)

    if leftover == "kept":
        # A destination someone else owns is never written into or replaced.
        assert (tmp_path / "capture").is_dir()
        assert not (tmp_path / "capture" / "capture.json").exists()
    else:
        assert not (tmp_path / "capture").exists()


@pytest.mark.parametrize(
    ("validator", "value", "request_header", "expected_status"),
    [
        ("ETag", '"capture-0"', "if-none-match", 200),
        ("Last-Modified", "Sat, 15 Aug 2026 12:34:56 GMT", "if-modified-since", 200),
        (None, None, None, 404),
    ],
)
def test_capture_rechecks_conditionally_only_on_a_recorded_validator(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
    validator: str | None,
    value: str | None,
    request_header: str | None,
    expected_status: int,
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    if validator is None:
        # A 404 carries no body to recheck, so its validator is never replayed.
        httpx_mock.add_response(
            method="GET", url=urls[0][2], status_code=404, headers={"ETag": '"missing"'}
        )
    else:
        assert value is not None
        httpx_mock.add_response(
            method="GET",
            url=urls[0][2],
            status_code=200,
            content=body,
            headers={validator: value},
        )
    for _channel, _platform, url in urls[1:]:
        httpx_mock.add_response(method="GET", url=url, status_code=404)
    httpx_mock.add_response(
        method="GET",
        url=urls[0][2],
        status_code=304 if validator is not None else 404,
    )
    for _channel, _platform, url in urls[1:]:
        httpx_mock.add_response(method="GET", url=url, status_code=404)

    manifest = _capture(tmp_path / "capture")

    assert manifest.entries[0].status == expected_status
    recheck = httpx_mock.get_requests()[24]
    if request_header is None:
        assert "if-none-match" not in recheck.headers
    else:
        assert recheck.headers[request_header] == value


def test_verify_capture_live_confirms_the_capture_or_names_the_changed_entry(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    bodies = {urls[0][2]: body}
    for _ in range(3):
        _register_matrix(httpx_mock, bodies)

    destination = tmp_path / "capture"
    with httpx.Client() as client:
        manifest = capture_legacy(client, destination, CAPTURED_AT)
        verify_live_capture(client, destination, manifest)

        # A live rehearsal that observes changed bytes names the matrix entry.
        httpx_mock.add_response(
            method="GET", url=urls[0][2], status_code=200, content=body + b"\n"
        )
        with pytest.raises(
            CaptureError,
            match=(
                "stable.*x86_64-unknown-linux-gnu.*expected status 200.*"
                "observed status 200.*sha256"
            ),
        ):
            verify_live_capture(client, destination, manifest)


def test_capture_command_requires_an_explicit_utc_timestamp(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli._build_parser().parse_args(
            ["capture", "--captured-at", "2026-08-20T12:00:00-04:00"]
        )

    assert raised.value.code == 2
    assert "RFC3339 UTC" in capsys.readouterr().err
