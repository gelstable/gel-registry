"""Deciding which upstream releases the registry is willing to consider.

The registry owns its own source policy: `sources/products.json` names the
products, the repositories they come from, and the tags, platforms and
encodings each expects. Discovery then walks the GitHub releases endpoint for
those repositories — re-validating every pagination link, because a `Link`
header is attacker-influenced input — and the adapter decides eligibility.
Nothing is downloaded until this stage has agreed on what to look at.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from support import github_release, mock_client, product_policy, release_record
from support import write_source_policy as write_policy

from gel_registry.adapters import adapter_for, validate_record_policy
from gel_registry.constants import CLI_PLATFORMS
from gel_registry.contracts import ReleaseSource
from gel_registry.github import (
    GitHubError,
    GitHubRelease,
    discover_cli_releases,
    discover_repository_releases,
)
from gel_registry.policy import PolicyError, load_source_policy

Mutate = Callable[[dict[str, object]], None]


def _release_with_duplicate_assets() -> dict[str, object]:
    payload: dict[str, object] = json.loads(json.dumps(github_release("v2.0.0", 2)))
    assets = payload["assets"]
    assert isinstance(assets, list)
    payload["assets"] = [*assets, assets[0]]
    return payload


def _drop(field: str) -> Mutate:
    def mutate(policy: dict[str, object]) -> None:
        del policy[field]

    return mutate


def _set(field: str, value: object) -> Mutate:
    def mutate(policy: dict[str, object]) -> None:
        policy[field] = value

    return mutate


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_drop("product"), id="missing-product"),
        pytest.param(_drop("repository"), id="missing-repository"),
        pytest.param(_drop("adapter"), id="missing-adapter"),
        pytest.param(_drop("platforms"), id="missing-platforms"),
        pytest.param(_set("repository", "gelstable"), id="repository-without-owner"),
        pytest.param(
            _set("repository", "gelstable/gel-cli/extra"), id="repository-with-extra"
        ),
        pytest.param(
            _set("repository", "https://github.com/gelstable/gel-cli"),
            id="repository-as-url",
        ),
        pytest.param(
            _set("tag_pattern", "v[0-9]+$"), id="tag-pattern-unanchored-start"
        ),
        pytest.param(
            _set("tag_pattern", r"^v[0-9]+\.[0-9]+\.[0-9]+"),
            id="tag-pattern-unanchored-end",
        ),
        pytest.param(_set("tag_pattern", "["), id="tag-pattern-uncompilable"),
        pytest.param(_set("adapter", "unknown"), id="unknown-adapter"),
        pytest.param(
            _set("platforms", [CLI_PLATFORMS[0], CLI_PLATFORMS[0]]),
            id="duplicate-platforms",
        ),
        pytest.param(_set("platforms", []), id="empty-platforms"),
        pytest.param(
            _set("encodings", ["identity", "identity"]), id="duplicate-encodings"
        ),
        pytest.param(_set("encodings", []), id="empty-encodings"),
    ],
)
def test_source_policy_rejects_a_product_it_cannot_act_on(
    tmp_path: Path, mutate: Mutate
) -> None:
    policy = product_policy()
    mutate(policy)
    write_policy(tmp_path, [policy])

    with pytest.raises(PolicyError):
        load_source_policy(tmp_path)


@pytest.mark.parametrize(
    ("products", "canonical"),
    [
        pytest.param(
            [product_policy(), product_policy()], True, id="duplicate-product"
        ),
        pytest.param(
            [product_policy("gel-ls", repository="gelstable/gel"), product_policy()],
            True,
            id="non-canonical-ordering",
        ),
        pytest.param([product_policy()], False, id="non-canonical-bytes"),
    ],
)
def test_source_policy_must_be_canonical_and_unambiguous(
    tmp_path: Path, products: list[dict[str, object]], canonical: bool
) -> None:
    write_policy(tmp_path, products, canonical=canonical)

    with pytest.raises(PolicyError, match="canonical|duplicate"):
        load_source_policy(tmp_path)


def test_discovery_paginates_filters_drafts_and_sorts_by_version() -> None:
    pages = {
        1: [
            github_release("v2.0.0", 200),
            github_release("v1.2.3", 123),
            github_release("v9.0.0", 900, draft=True),
        ],
        2: [
            github_release("v1.10.0", 110),
            github_release("gel-server-v3.0.0-rc.1", 301, prerelease=True),
            github_release("gel-lsp-v4.0.0", 400),
        ],
        3: [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "2"))
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        headers = (
            {
                "Link": (
                    "<https://api.github.com/repos/gelstable/gel-cli/releases"
                    '?per_page=100&page=2>; rel="next"'
                )
            }
            if page == 1
            else {}
        )
        return httpx.Response(200, headers=headers, json=pages[page])

    with mock_client(handler) as client:
        releases = discover_cli_releases(client, known_versions={"1.2.3"})

    # Drafts, prereleases and already-known versions are gone; what remains is
    # sorted by SemVer rather than by the order GitHub happened to return.
    assert [release.version for release in releases] == ["1.10.0", "2.0.0", "4.0.0"]
    assert [release.release_id for release in releases] == [110, 200, 400]


@pytest.mark.parametrize(
    "next_link",
    [
        pytest.param(
            "https://api.github.com/repos/gelstable/gel-cli/releases?page=2",
            id="switches-repository",
        ),
        pytest.param(
            "https://api.github.com:444/repos/gelstable/gel/releases?page=2",
            id="nonstandard-port",
        ),
        pytest.param(
            "https://evil.example/repos/gelstable/gel/releases?page=2", id="off-host"
        ),
        pytest.param(
            "https://api.github.com/repos/gelstable/gel/issues?page=2",
            id="different-endpoint",
        ),
    ],
)
def test_pagination_cannot_be_redirected_off_the_requested_endpoint(
    next_link: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Link": f'<{next_link}>; rel="next"'}, json=[]
        )

    with (
        mock_client(handler) as client,
        pytest.raises(GitHubError, match="outside the releases endpoint"),
    ):
        discover_repository_releases(client, "gelstable/gel")


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v8.0.0", "8.0.0"),
        ("gel-server-v8.0.0-rc1", "8.0.0-rc1"),
        ("gel-lsp-v8.0.0", "8.0.0"),
        ("vscode-v8.0.0", "8.0.0"),
        ("nightly", ""),
    ],
)
def test_a_tag_is_parsed_into_a_product_neutral_version(tag: str, version: str) -> None:
    repository = "gelstable/gel"
    payload = github_release(tag, owner=repository)
    assets = payload["assets"]
    assert isinstance(assets, list)
    payload["assets"] = assets[:1]

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[payload])

    with mock_client(handler) as client:
        releases = discover_repository_releases(client, repository)

    # Repository-scoped discovery reports everything it finds, with an empty
    # version for tags it cannot parse, and leaves the filtering to the
    # adapter. It does not require a complete CLI asset set to do so.
    assert seen == [f"/repos/{repository}/releases"]
    assert [(item.tag_name, item.version, item.repository) for item in releases] == [
        (tag, version, repository)
    ]


@pytest.mark.parametrize(
    ("payloads", "message"),
    [
        pytest.param(
            [github_release("not-a-version")], "invalid release tag", id="unparseable"
        ),
        pytest.param(
            [github_release("v1.0.0", 1), github_release("v1.0.0", 2)],
            "ambiguous",
            id="two-ids-for-one-version",
        ),
        pytest.param(
            [_release_with_duplicate_assets()], "duplicate", id="duplicate-assets"
        ),
    ],
)
def test_cli_discovery_refuses_evidence_it_cannot_resolve(
    payloads: list[dict[str, object]], message: str
) -> None:
    with (
        mock_client(lambda _request: httpx.Response(200, json=payloads)) as client,
        pytest.raises(GitHubError, match=message),
    ):
        discover_cli_releases(client, known_versions=())


def test_eligibility_requires_the_policy_tag_and_one_repository_of_evidence(
    tmp_path: Path,
) -> None:
    write_policy(
        tmp_path,
        [product_policy(), product_policy("gel-ls", repository="gelstable/gel")],
    )
    source_policy = load_source_policy(tmp_path)
    # Two products may share one repository; it is named once.
    assert source_policy.repositories() == ("gelstable/gel", "gelstable/gel-cli")

    policy = source_policy.by_product()["gel-cli"]
    assert policy.adapter == "gel-cli"
    adapter = adapter_for(policy.adapter)
    release = GitHubRelease.model_validate(github_release("v1.2.3"))

    assert adapter.eligible(policy, release)
    assert not adapter.eligible(
        policy, release.model_copy(update={"tag_name": "nightly", "version": ""})
    )
    # A release whose assets point at more than one repository is ambiguous
    # provenance, not a partially valid release.
    assert not adapter.eligible(
        policy,
        release.model_copy(
            update={"repositories": frozenset({"gelstable/gel-cli", "other/repo"})}
        ),
    )

    validate_record_policy(release_record(), policy)
    foreign = release_record().model_copy(
        update={
            "source": ReleaseSource(
                repository="other/repo", release_tag="v1.2.3", release_id=1
            )
        }
    )
    with pytest.raises(PolicyError, match="repository"):
        validate_record_policy(foreign, policy)
