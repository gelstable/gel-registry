"""Tests for rescue preflight, idempotency, and draft publication."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from gel_registry.digest import canonical_json
from gel_registry.rescue import RescueAsset, RescuePlan, RescueRelease
from gel_registry.rescue.manifests import (
    MANIFEST_NAME,
    release_manifest_bytes,
    release_manifest_digest,
)
from gel_registry.rescue.operations import (
    RescueManualCleanupError,
    RescuePreflightError,
    RescuePublishError,
    describe_operations,
    execute,
    load_plan,
    preflight,
    publish_plan,
)

REPOSITORY = "gelstable/gel"
RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/42"
SOURCE = "https://packages.edgedb.com/archive/gel-server-1.0.0"
BODY = b"abc"
BODY_SHA256 = hashlib.sha256(BODY).hexdigest()


def _plan(
    *,
    repository: str = REPOSITORY,
    expected_size: int = len(BODY),
    asset_name: str | None = None,
) -> RescuePlan:
    asset = RescueAsset(
        source_url=SOURCE,
        destination_name=asset_name or "server.tar.gz",
        content_type="application/gzip",
        expected_size=expected_size,
        expected_sha256=BODY_SHA256,
    )
    return RescuePlan(
        schema_version=1,
        capture="legacy-2026-08-bootstrap",
        releases=(
            RescueRelease(
                repository=repository,
                tag="legacy-v7",
                target_commitish="abc123",
                assets=(asset,),
            ),
        ),
    )


def _release(*, upload_url: str | None = None) -> dict[str, object]:
    return {
        "id": 42,
        "tag_name": "legacy-v7",
        "draft": True,
        "prerelease": False,
        "url": RELEASE_URL,
        "upload_url": upload_url
        or f"https://uploads.github.com/repos/{REPOSITORY}/releases/42/assets{{?name,label}}",
        "assets": [],
    }


def _uploaded_asset(
    name: str = "server.tar.gz",
    *,
    asset_id: int = 9,
    size: int | None = len(BODY),
    digest: str | None = "sha256:" + BODY_SHA256,
    state: str | None = "uploaded",
) -> dict[str, object]:
    return {
        "id": asset_id,
        "name": name,
        "size": size,
        "digest": digest,
        "state": state,
        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}",
    }


def _uploaded_manifest(
    plan: RescuePlan,
    *,
    asset_id: int = 10,
    state: str = "uploaded",
) -> dict[str, object]:
    data = release_manifest_bytes(plan.releases[0])
    return {
        "id": asset_id,
        "name": MANIFEST_NAME,
        "size": len(data),
        "digest": "sha256:" + release_manifest_digest(data),
        "state": state,
        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}",
    }


class _Github:
    """One scripted GitHub + package-host transport for a whole plan run."""

    def __init__(
        self,
        *,
        release: dict[str, object] | None = None,
        assets: list[dict[str, object]] | None = None,
        source_status: int = 200,
        source_body: bytes = BODY,
        upload_asset: dict[str, object] | None = None,
        assets_after_upload: list[dict[str, object]] | None = None,
    ) -> None:
        self.release = release
        self.assets = list(assets or [])
        self.source_status = source_status
        self.source_body = source_body
        self.upload_asset = upload_asset or _uploaded_asset()
        self.assets_after_upload = assets_after_upload
        self.requests: list[tuple[str, str]] = []
        self._uploaded = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url)))
        host = request.url.host
        if host == "packages.edgedb.com":
            if request.method == "HEAD":
                return httpx.Response(self.source_status)
            self._uploaded = True
            return httpx.Response(self.source_status, content=self.source_body)
        if host == "uploads.github.com":
            self._uploaded = True
            name = request.url.params.get("name")
            if name == MANIFEST_NAME:
                body = request.read()
                manifest_resp = {
                    "id": 99,
                    "name": MANIFEST_NAME,
                    "size": len(body),
                    "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "state": "uploaded",
                    "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/99",
                }
                return httpx.Response(201, json=manifest_resp)
            return httpx.Response(201, json=self.upload_asset)
        if request.method == "POST":
            self.release = _release()
            return httpx.Response(201, json=self.release)
        if request.url.path.endswith("/assets"):
            listing = self.assets
            if self._uploaded and self.assets_after_upload is not None:
                listing = self.assets_after_upload
            return httpx.Response(200, json=listing)
        if self.release is None:
            return httpx.Response(404)
        return httpx.Response(200, json=self.release)

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


def test_load_plan_requires_the_canonical_byte_representation(tmp_path: Path) -> None:
    plan = _plan()
    good = tmp_path / "plan.json"
    good.write_bytes(canonical_json(plan))
    assert load_plan(good) == plan

    bad = tmp_path / "reformatted.json"
    bad.write_bytes(canonical_json(plan) + b"\n")
    with pytest.raises(RescuePublishError, match="invalid rescue plan"):
        load_plan(bad)


def test_preflight_rejects_unallowlisted_repository_before_any_request() -> None:
    github = _Github()
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, _plan(repository="attacker/repo"))
    assert "not allowlisted" in str(excinfo.value)
    assert github.requests == []


def test_preflight_reports_every_conflict_and_makes_no_mutation() -> None:
    github = _Github(
        release=_release(), assets=[_uploaded_asset(size=999, digest=None)]
    )
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, _plan())
    assert "has size 999" in str(excinfo.value)
    assert excinfo.value.problems
    assert "POST" not in github.methods


def test_preflight_treats_an_incomplete_starter_asset_as_a_conflict() -> None:
    github = _Github(release=_release(), assets=[_uploaded_asset(state="starter")])
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, _plan())
    message = str(excinfo.value)
    assert "'starter'" in message
    assert "remove asset id 9 explicitly" in message
    assert "DELETE" not in github.methods


def test_preflight_rejects_an_existing_published_release() -> None:
    published = _release()
    published["draft"] = False
    github = _Github(release=published)
    with github.client() as client, pytest.raises(RescuePreflightError, match="draft"):
        preflight(client, _plan())
    assert "POST" not in github.methods


def test_preflight_rejects_duplicate_asset_names_on_github() -> None:
    github = _Github(
        release=_release(),
        assets=[_uploaded_asset(asset_id=1), _uploaded_asset(asset_id=2)],
    )
    with (
        github.client() as client,
        pytest.raises(RescuePreflightError, match="duplicate asset name"),
    ):
        preflight(client, _plan())


def test_preflight_permits_extra_assets_without_deleting_them() -> None:
    github = _Github(
        release=_release(),
        assets=[
            _uploaded_asset(),
            _uploaded_manifest(_plan()),
            _uploaded_asset("extra-experimental.zst", asset_id=99, size=50),
        ],
    )
    with github.client() as client:
        report = preflight(client, _plan())
    assert report.upload_count == 0


def test_preflight_rejects_existing_manifest_when_binaries_are_absent() -> None:
    plan = _plan()
    github = _Github(
        release=_release(),
        assets=[_uploaded_manifest(plan)],
    )
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, plan)
    assert "planned binary assets are absent" in str(excinfo.value)


def test_preflight_requires_every_source_object_to_remain_available() -> None:
    github = _Github(source_status=404)
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, _plan())
    assert "source returned HTTP 404" in str(excinfo.value)


def test_dry_run_performs_the_preflight_and_creates_nothing() -> None:
    github = _Github()
    with github.client() as client:
        lines = publish_plan(client, _plan(), dry_run=True)
    assert lines[0] == "dry run: no releases or assets were created"
    assert f"create draft release {REPOSITORY}@legacy-v7" in lines
    assert "  upload server.tar.gz (3 bytes)" in lines
    assert f"  upload {MANIFEST_NAME}" in [line.split(" (")[0] for line in lines]
    assert "total uploads: 2" in lines
    assert "POST" not in github.methods
    assert github.methods.count("HEAD") == 1


def test_execute_creates_a_draft_uploads_binaries_and_manifest() -> None:
    github = _Github()
    plan = _plan()
    with github.client() as client:
        report = preflight(client, plan)
        assert report.upload_count == 2
        count = execute(client, report)

    assert count == 2
    assert ("POST", f"https://api.github.com/repos/{REPOSITORY}/releases") in (
        github.requests
    )
    upload_calls = [url for _, url in github.requests if "uploads.github.com" in url]
    assert len(upload_calls) == 2
    assert "name=server.tar.gz" in upload_calls[0]
    assert f"name={MANIFEST_NAME}" in upload_calls[1]


def test_rerunning_the_same_plan_resumes_from_github_state() -> None:
    plan = _plan()
    github = _Github(
        release=_release(),
        assets=[_uploaded_asset(), _uploaded_manifest(plan)],
    )
    with github.client() as client:
        report = preflight(client, plan)
        assert [outcome.status for outcome in report.releases[0].assets] == ["matching"]
        assert report.releases[0].manifest is not None
        assert report.releases[0].manifest.status == "matching"
        assert report.upload_count == 0
        count = execute(client, report)

    assert count == 0
    assert "POST" not in github.methods
    lines = describe_operations(report)
    assert "reuse draft release" in lines[0]
    assert "  skip server.tar.gz (already matches)" in lines
    assert f"  skip {MANIFEST_NAME} (already matches)" in lines


def test_resuming_uploads_manifest_when_binaries_match_but_manifest_missing() -> None:
    plan = _plan()
    github = _Github(
        release=_release(),
        assets=[_uploaded_asset()],
    )
    with github.client() as client:
        report = preflight(client, plan)
        assert report.upload_count == 1
        assert report.releases[0].upload_manifest is True
        count = execute(client, report)

    assert count == 1
    upload_calls = [url for _, url in github.requests if "uploads.github.com" in url]
    assert len(upload_calls) == 1
    assert f"name={MANIFEST_NAME}" in upload_calls[0]


def test_state_unknown_upload_reports_the_leftover_asset_id_for_cleanup() -> None:
    github = _Github(
        source_body=b"abcd",
        assets_after_upload=[_uploaded_asset(state="starter", asset_id=11)],
    )
    with github.client() as client:
        report = preflight(client, _plan())
        with pytest.raises(RescueManualCleanupError) as excinfo:
            execute(client, report)
    assert excinfo.value.asset_id == 11
    assert "remove it explicitly" in str(excinfo.value)
    assert "DELETE" not in github.methods


def test_source_digest_mismatch_without_a_leftover_asset_is_rerunnable() -> None:
    github = _Github(source_body=b"xyz", assets_after_upload=[])
    with github.client() as client:
        report = preflight(client, _plan())
        with pytest.raises(RescuePublishError) as excinfo:
            execute(client, report)
    assert not isinstance(excinfo.value, RescueManualCleanupError)
    assert "no asset was left" in str(excinfo.value)


def test_upload_integrity_mismatch_is_reconciled_not_retried() -> None:
    github = _Github(
        upload_asset=_uploaded_asset(size=99, asset_id=12),
        assets_after_upload=[_uploaded_asset(asset_id=12, size=99)],
    )
    with github.client() as client:
        report = preflight(client, _plan())
        with pytest.raises(RescueManualCleanupError) as excinfo:
            execute(client, report)
    assert excinfo.value.asset_id == 12
    assert github.methods.count("POST") == 2


def test_preflight_rejects_existing_asset_without_digest() -> None:
    github = _Github(
        release=_release(),
        assets=[_uploaded_asset(digest=None)],
    )
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, _plan())
    assert "missing required verification metadata" in str(excinfo.value)
    assert "exact sha256" in str(excinfo.value)


def test_preflight_rejects_existing_asset_without_uploaded_state() -> None:
    github = _Github(
        release=_release(),
        assets=[_uploaded_asset(state=None)],
    )
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, _plan())
    assert "missing required verification metadata" in str(excinfo.value)
    assert "state 'uploaded'" in str(excinfo.value)


def test_preflight_rejects_existing_manifest_without_full_verification() -> None:
    plan = _plan()
    manifest = _uploaded_manifest(plan)
    manifest["digest"] = None
    github = _Github(
        release=_release(),
        assets=[_uploaded_asset(), manifest],
    )
    with github.client() as client, pytest.raises(RescuePreflightError) as excinfo:
        preflight(client, plan)
    assert "missing required verification metadata" in str(excinfo.value)
    assert "exact sha256" in str(excinfo.value)


def test_manifest_upload_integrity_mismatch_is_reconciled() -> None:
    plan = _plan()
    manifest_id = 99
    uploaded = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        host = request.url.host
        if host == "packages.edgedb.com":
            return httpx.Response(200, content=BODY)
        if host == "uploads.github.com":
            name = request.url.params.get("name")
            if name == MANIFEST_NAME:
                uploaded = True
                return httpx.Response(
                    201,
                    json={
                        "id": manifest_id,
                        "name": MANIFEST_NAME,
                        "size": 254,
                        "digest": "sha256:" + "f" * 64,
                        "state": "uploaded",
                        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{manifest_id}",
                    },
                )
            return httpx.Response(201, json=_uploaded_asset())
        if request.url.path.endswith("/assets"):
            assets = [_uploaded_asset()]
            if uploaded:
                assets.append(
                    {
                        "id": manifest_id,
                        "name": MANIFEST_NAME,
                        "size": 254,
                        "digest": "sha256:" + "f" * 64,
                        "state": "uploaded",
                        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{manifest_id}",
                    }
                )
            return httpx.Response(200, json=assets)
        return httpx.Response(200, json=_release())

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with client:
        report = preflight(client, plan)
        with pytest.raises(RescueManualCleanupError) as excinfo:
            execute(client, report)
    assert excinfo.value.asset_id == manifest_id
    assert "remove it explicitly" in str(excinfo.value)


def test_upload_201_with_malformed_response_json_is_reconciled() -> None:
    asset_id = 45
    uploaded = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded
        host = request.url.host
        if host == "packages.edgedb.com":
            return httpx.Response(200, content=BODY)
        if host == "uploads.github.com":
            uploaded = True
            return httpx.Response(201, content=b"not json")
        if request.url.path.endswith("/assets"):
            assets = []
            if uploaded:
                assets.append(_uploaded_asset(asset_id=asset_id))
            return httpx.Response(200, json=assets)
        return httpx.Response(200, json=_release())

    client = httpx.Client(transport=httpx.MockTransport(handle))
    with client:
        report = preflight(client, _plan())
        with pytest.raises(RescueManualCleanupError) as excinfo:
            execute(client, report)
    assert excinfo.value.asset_id == asset_id
    assert "remove it explicitly" in str(excinfo.value)
