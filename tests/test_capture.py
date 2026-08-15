from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

import gel_registry.capture as capture_module
from gel_registry.capture import CaptureError, capture_legacy, verify_live_capture
from gel_registry.constants import capture_urls

CAPTURED_AT = datetime(2026, 8, 15, 12, 34, 56, tzinfo=UTC)
USER_AGENT = "gel-registry-importer/1"


def _index_bytes(package_index_data: dict[str, object], *, pretty: bool) -> bytes:
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


def _register_initial_matrix(
    httpx_mock: HTTPXMock,
    bodies: dict[str, bytes],
    *,
    first_status: int | None = None,
) -> None:
    if first_status is not None:
        httpx_mock.add_response(
            method="GET", url=capture_urls()[0][2], status_code=first_status
        )
        return
    for index, (_channel, _platform, url) in enumerate(capture_urls()):
        status = 404
        if url in bodies:
            status = 200
            httpx_mock.add_response(
                method="GET",
                url=url,
                status_code=status,
                content=bodies[url],
                headers={"ETag": f'"capture-{index}"'},
            )
        else:
            httpx_mock.add_response(method="GET", url=url, status_code=status)


def test_capture_legacy_preserves_matrix_order_bytes_and_rechecks(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    bodies = {
        urls[0][2]: _index_bytes(package_index_data, pretty=False),
        urls[16][2]: _index_bytes(package_index_data, pretty=True),
    }
    _register_initial_matrix(httpx_mock, bodies)
    for _channel, _platform, url in urls:
        if url in bodies:
            httpx_mock.add_response(
                method="GET",
                url=url,
                status_code=200,
                content=bodies[url],
            )
        else:
            httpx_mock.add_response(method="GET", url=url, status_code=404)

    destination = tmp_path / "capture"
    with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
        manifest = capture_legacy(client, destination, CAPTURED_AT)

    requests = httpx_mock.get_requests()
    assert [str(request.url) for request in requests] == [
        url for _channel, _platform, url in urls
    ] * 2
    assert all(request.headers["user-agent"] == USER_AGENT for request in requests)
    assert requests[24].headers["if-none-match"] == '"capture-0"'
    assert requests[24 + 16].headers["if-none-match"] == '"capture-16"'

    assert manifest.captured_at == CAPTURED_AT
    assert [(entry.channel, entry.platform) for entry in manifest.entries] == [
        (channel, platform) for channel, platform, _url in urls
    ]
    successful = [entry for entry in manifest.entries if entry.status == 200]
    assert len(successful) == 2
    for entry in successful:
        assert entry.final_url == entry.url
        assert entry.etag is not None
        assert entry.size == len(bodies[entry.url])
        assert entry.path == f"indexes/{entry.channel}-{entry.platform}.json"
        assert (destination / entry.path).read_bytes() == bodies[entry.url]
        assert entry.sha256 is not None
        assert entry.blake2b is not None
    assert all(
        entry.status == 404
        and entry.path is None
        and entry.size is None
        and entry.sha256 is None
        and entry.blake2b is None
        for entry in manifest.entries
        if entry.status == 404
    )
    assert (destination / "capture.json").exists()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(lambda _mock, _body: {"first_status": 403}, id="403"),
        pytest.param(lambda _mock, _body: {"first_status": 429}, id="429"),
        pytest.param(lambda _mock, _body: {"first_status": 500}, id="500"),
        pytest.param(
            lambda mock, _body: mock.add_exception(httpx.ReadTimeout("read timed out")),
            id="timeout",
        ),
        pytest.param(
            lambda mock, _body: mock.add_exception(httpx.ConnectError("TLS failure")),
            id="transport-error",
        ),
        pytest.param(
            lambda mock, _body: mock.add_response(
                method="GET",
                url=capture_urls()[0][2],
                status_code=302,
                headers={"Location": "https://evil.example/index.json"},
            ),
            id="off-origin-redirect",
        ),
        pytest.param(
            lambda mock, _body: mock.add_response(
                method="GET",
                url=capture_urls()[0][2],
                status_code=302,
                headers={"Location": "http://packages.geldata.com/index.json"},
            ),
            id="http-redirect",
        ),
        pytest.param(
            lambda mock, _body: mock.add_response(
                method="GET",
                url=capture_urls()[0][2],
                status_code=200,
                content=b'{"packages": [}',
            ),
            id="malformed-json",
        ),
    ],
)
def test_capture_legacy_failure_is_atomic(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
    failure: Callable[[HTTPXMock, bytes], object],
) -> None:
    body = _index_bytes(package_index_data, pretty=False)
    result = failure(httpx_mock, body)
    if isinstance(result, dict):
        _register_initial_matrix(httpx_mock, {}, **result)

    destination = tmp_path / "capture"
    with httpx.Client() as client, pytest.raises(CaptureError):
        capture_legacy(client, destination, CAPTURED_AT)
    assert not destination.exists()


def test_capture_legacy_rejects_changed_recheck_bytes_atomically(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    changed = body + b"\n"
    _register_initial_matrix(httpx_mock, {urls[0][2]: body})
    httpx_mock.add_response(
        method="GET", url=urls[0][2], status_code=200, content=changed
    )

    destination = tmp_path / "capture"
    with (
        httpx.Client() as client,
        pytest.raises(CaptureError, match="stable.*x86_64-unknown-linux-gnu"),
    ):
        capture_legacy(client, destination, CAPTURED_AT)
    assert not destination.exists()


@pytest.mark.parametrize(
    ("validator", "request_header"),
    [("ETag", "if-none-match"), ("Last-Modified", "if-modified-since")],
)
def test_capture_legacy_accepts_validator_backed_304(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
    validator: str,
    request_header: str,
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    first_header = (
        '"capture-0"' if validator == "ETag" else "Sat, 15 Aug 2026 12:34:56 GMT"
    )
    httpx_mock.add_response(
        method="GET",
        url=urls[0][2],
        status_code=200,
        content=body,
        headers={validator: first_header},
    )
    for _channel, _platform, url in urls[1:]:
        httpx_mock.add_response(method="GET", url=url, status_code=404)
    httpx_mock.add_response(method="GET", url=urls[0][2], status_code=304)
    for _channel, _platform, url in urls[1:]:
        httpx_mock.add_response(method="GET", url=url, status_code=404)

    destination = tmp_path / "capture"
    with httpx.Client() as client:
        manifest = capture_legacy(client, destination, CAPTURED_AT)

    assert manifest.entries[0].status == 200
    requests = httpx_mock.get_requests()
    assert requests[24].headers[request_header] == first_header


def test_capture_legacy_rejects_unconditional_304(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    httpx_mock.add_response(method="GET", url=urls[0][2], status_code=200, content=body)
    for _channel, _platform, url in urls[1:]:
        httpx_mock.add_response(method="GET", url=url, status_code=404)
    httpx_mock.add_response(method="GET", url=urls[0][2], status_code=304)

    destination = tmp_path / "capture"
    with (
        httpx.Client() as client,
        pytest.raises(CaptureError, match="expected status 200, observed 304"),
    ):
        capture_legacy(client, destination, CAPTURED_AT)
    assert not destination.exists()


def test_capture_legacy_no_replace_publication_survives_destination_race(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    _register_initial_matrix(httpx_mock, {urls[0][2]: body})
    for _channel, _platform, url in urls:
        httpx_mock.add_response(
            method="GET",
            url=url,
            status_code=200 if url == urls[0][2] else 404,
            content=body if url == urls[0][2] else b"",
        )

    real_rename = capture_module._rename_noreplace

    def create_destination_then_rename(source: Path, destination: Path) -> None:
        destination.mkdir()
        real_rename(source, destination)

    monkeypatch.setattr(
        capture_module, "_rename_noreplace", create_destination_then_rename
    )
    destination = tmp_path / "capture"
    with httpx.Client() as client, pytest.raises(CaptureError):
        capture_legacy(client, destination, CAPTURED_AT)
    assert destination.is_dir()
    assert not (destination / "capture.json").exists()


def test_capture_legacy_rejects_changed_404_to_200_atomically(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    _register_initial_matrix(httpx_mock, {urls[0][2]: body})
    # The first entry is initially successful; the second entry is absent.
    httpx_mock.add_response(method="GET", url=urls[0][2], status_code=200, content=body)
    httpx_mock.add_response(method="GET", url=urls[1][2], status_code=200, content=body)

    destination = tmp_path / "capture"
    with (
        httpx.Client() as client,
        pytest.raises(CaptureError, match="stable.*x86_64-unknown-linux-musl"),
    ):
        capture_legacy(client, destination, CAPTURED_AT)
    assert not destination.exists()


def test_capture_legacy_refuses_to_overwrite_existing_destination(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    destination = tmp_path / "capture"
    destination.mkdir()
    marker = destination / "marker"
    marker.write_text("keep")

    with httpx.Client() as client, pytest.raises(CaptureError, match="already exists"):
        capture_legacy(client, destination, CAPTURED_AT)
    assert marker.read_text() == "keep"
    assert not httpx_mock.get_requests()


def test_verify_live_capture_requires_current_status_and_exact_bytes(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    _register_initial_matrix(httpx_mock, {urls[0][2]: body})
    for _channel, _platform, url in urls:
        httpx_mock.add_response(
            method="GET",
            url=url,
            status_code=200 if url in {urls[0][2]} else 404,
            content=body if url in {urls[0][2]} else b"",
        )
    for _channel, _platform, url in urls:
        httpx_mock.add_response(
            method="GET",
            url=url,
            status_code=200 if url in {urls[0][2]} else 404,
            content=body if url in {urls[0][2]} else b"",
        )

    destination = tmp_path / "capture"
    with httpx.Client() as client:
        manifest = capture_legacy(client, destination, CAPTURED_AT)
        verify_live_capture(client, destination, manifest)


def test_verify_live_capture_error_identifies_matrix_entry(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
    package_index_data: dict[str, object],
) -> None:
    urls = capture_urls()
    body = _index_bytes(package_index_data, pretty=False)
    _register_initial_matrix(httpx_mock, {urls[0][2]: body})
    for _channel, _platform, url in urls:
        httpx_mock.add_response(
            method="GET",
            url=url,
            status_code=200 if url == urls[0][2] else 404,
            content=body if url == urls[0][2] else b"",
        )
    # The initial capture is valid, but live rehearsal observes changed bytes.
    httpx_mock.add_response(
        method="GET", url=urls[0][2], status_code=200, content=body + b"\n"
    )

    destination = tmp_path / "capture"
    with httpx.Client() as client:
        manifest = capture_legacy(client, destination, CAPTURED_AT)
        with pytest.raises(
            CaptureError,
            match=(
                "stable.*x86_64-unknown-linux-gnu.*expected status 200.*"
                "observed status 200.*sha256"
            ),
        ):
            verify_live_capture(client, destination, manifest)
