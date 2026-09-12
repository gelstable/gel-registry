"""The scheduled promotion always rebuilds from the current main branch."""

from __future__ import annotations

import importlib.util
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest
from pytest_httpx import HTTPXMock
from support import package, write_bootstrap

from gel_registry.candidate import build_candidate
from gel_registry.digest import canonical_json
from gel_registry.github import create_github_client
from gel_registry.render.errors import ContestedReplacementError

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "promote.py"


class PromotionScript(Protocol):
    def main(self, *, run: Callable[[list[str]], str]) -> None: ...


@pytest.fixture
def promote() -> PromotionScript:
    spec = importlib.util.spec_from_file_location("promote", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(PromotionScript, module)


def _identity(release: dict[str, object]) -> tuple[str, int]:
    return release["repository"], release["id"]  # type: ignore[return-value]


def _candidate_from_main(
    main_identities: set[tuple[str, int]], upstream: list[dict[str, object]]
) -> set[tuple[str, int]]:
    """Model build-candidate's gather against a fresh main checkout."""
    return main_identities | {_identity(release) for release in upstream}


def test_second_fresh_candidate_contains_release_a_and_new_release_b() -> None:
    """A waiting PR cannot hide an earlier release from the next run."""
    release_a = {"repository": "gelstable/gel", "id": 101}
    release_b = {"repository": "gelstable/gel-cli", "id": 202}
    main: set[tuple[str, int]] = set()

    first = _candidate_from_main(main, [release_a])
    assert first == {_identity(release_a)}

    second = _candidate_from_main(main, [release_a, release_b])
    assert second == {_identity(release_a), _identity(release_b)}


class Recorder:
    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.commands.append(command)
        return self.responses.get(tuple(command), "")


def _responses(
    *, status: str = " M releases/gelstable/gel/101.json\n"
) -> dict[tuple[str, ...], str]:
    return {
        ("git", "status", "--porcelain", "--untracked-files=all"): "",
        ("git", "ls-remote", "--heads", "origin", "refs/heads/promote/registry"): (
            "abc123\trefs/heads/promote/registry\n"
        ),
        ("git", "fetch", "--no-tags", "origin", "main"): "",
        ("git", "fetch", "--no-tags", "origin", "promote/registry"): "",
        ("git", "merge-base", "origin/main", "origin/promote/registry"): "base\n",
        ("git", "diff", "--name-only", "base..origin/promote/registry"): "",
        ("git", "switch", "--detach", "origin/main"): "",
        ("git", "switch", "-C", "promote/registry"): "",
        ("gel-registry", "build-candidate", "--repo", "."): json.dumps(
            {
                "records": ["releases/gelstable/gel/101.json"],
                "rejected": [],
                "snapshot": "snapshot",
            }
        ),
        (
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=no",
        ): status,
        ("git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"): (
            "releases/gelstable/gel/101.json\n"
        ),
        ("git", "diff", "--name-only", "--cached"): (
            "releases/gelstable/gel/101.json\n"
        ),
        (
            "gh",
            "pr",
            "list",
            "--head",
            "promote/registry",
            "--state",
            "open",
            "--json",
            "number",
        ): "[]\n",
    }


def test_candidate_push_uses_the_observed_remote_oid_as_a_force_with_lease(
    promote: PromotionScript,
) -> None:
    """A concurrent branch update cannot be overwritten."""
    recorder = Recorder(_responses())

    promote.main(run=recorder)

    assert [
        "git",
        "push",
        "--force-with-lease=refs/heads/promote/registry:abc123",
        "origin",
        "HEAD:refs/heads/promote/registry",
    ] in recorder.commands


def test_candidate_commit_uses_github_actions_bot_identity(
    promote: PromotionScript,
) -> None:
    """The workflow can commit from a clean Actions checkout."""
    recorder = Recorder(_responses())

    promote.main(run=recorder)

    assert [
        "git",
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "-m",
        "data: promote registry releases",
    ] in recorder.commands


def test_unexpected_candidate_path_aborts_before_commit_or_push(
    promote: PromotionScript,
) -> None:
    """Promotion can mutate only generated registry paths."""
    recorder = Recorder(_responses(status="?? unexpected.txt\n"))

    with pytest.raises(RuntimeError, match="unexpected candidate path"):
        promote.main(run=recorder)

    assert not any(command[:2] == ["git", "commit"] for command in recorder.commands)
    assert not any(command[:2] == ["git", "push"] for command in recorder.commands)


def test_dirty_working_tree_aborts_immediately(
    promote: PromotionScript,
) -> None:
    """Promotion refuses to run when working directory is not clean."""
    responses = _responses()
    responses[("git", "status", "--porcelain", "--untracked-files=all")] = (
        " M dirty.txt\n"
    )
    recorder = Recorder(responses)

    with pytest.raises(RuntimeError, match="checkout is not clean"):
        promote.main(run=recorder)

    assert len(recorder.commands) == 1


def test_unexpected_existing_candidate_diff_aborts(
    promote: PromotionScript,
) -> None:
    """Unexpected paths on the existing remote candidate abort before rebuilding."""
    responses = _responses()
    responses[("git", "diff", "--name-only", "base..origin/promote/registry")] = (
        "unexpected.txt\n"
    )
    recorder = Recorder(responses)

    with pytest.raises(RuntimeError, match="unexpected existing candidate path"):
        promote.main(run=recorder)

    assert not any(command[:2] == ["git", "switch"] for command in recorder.commands)


def test_no_changes_deletes_candidate_branch_and_closes_open_pr(
    promote: PromotionScript,
) -> None:
    """When candidate build produces no changes, branch and PR are cleaned up."""
    responses = _responses(status="")
    responses[
        (
            "gh",
            "pr",
            "list",
            "--head",
            "promote/registry",
            "--state",
            "open",
            "--json",
            "number",
        )
    ] = '[{"number": 42}]\n'
    recorder = Recorder(responses)

    promote.main(run=recorder)

    assert [
        "git",
        "push",
        "--force-with-lease=refs/heads/promote/registry:abc123",
        "origin",
        ":refs/heads/promote/registry",
    ] in recorder.commands
    assert ["gh", "pr", "close", "42"] in recorder.commands
    assert not any(command[:2] == ["git", "commit"] for command in recorder.commands)


def test_candidate_updates_existing_open_pr(
    promote: PromotionScript,
) -> None:
    """An existing open PR is edited rather than creating a duplicate."""
    responses = _responses()
    responses[
        (
            "gh",
            "pr",
            "list",
            "--head",
            "promote/registry",
            "--state",
            "open",
            "--json",
            "number",
        )
    ] = '[{"number": 42}]\n'
    recorder = Recorder(responses)

    promote.main(run=recorder)

    assert any(
        command[:4] == ["gh", "pr", "edit", "42"] for command in recorder.commands
    )
    assert not any(
        command[:3] == ["gh", "pr", "create"] for command in recorder.commands
    )


def test_rolling_candidate_build_sequence_a_then_b_and_conflict_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Scheduled candidate build gathers releases from main and aborts on conflict."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-rolling-token")
    client = create_github_client()

    bootstrap_pkg = package("gel-server", "1.0.0", ref_suffix="server")
    main_repo = tmp_path / "main"
    write_bootstrap(main_repo, "stable", "x86_64-unknown-linux-gnu", [bootstrap_pkg])
    (main_repo / "sources").mkdir(parents=True)
    (main_repo / "sources" / "github.json").write_bytes(
        canonical_json(
            {
                "repositories": ["gelstable/gel-cli", "gelstable/gel"],
                "schema_version": 1,
            }
        )
    )

    # 1. main has no releases
    assert not (main_repo / "releases").exists()

    # 2. release A appears -> candidate contains A
    release_a_payload = {
        "id": 101,
        "tag_name": "v1.0.1",
        "published_at": "2026-08-16T00:00:00Z",
        "draft": False,
        "assets": [
            {
                "name": "gel-registry.json",
                "url": "https://api.github.com/assets/101",
            },
            {
                "name": "artifact",
                "url": "https://api.github.com/assets/101/artifact",
            },
        ],
    }
    pkg_a = package("gel-cli", "1.0.1", ref_suffix="cli-1.0.1")
    url_a = "https://github.com/gelstable/gel-cli/releases/download/v1.0.1/artifact"
    pkg_a["installref"] = url_a
    pkg_a["installrefs"] = [
        {
            "ref": url_a,
            "type": "application/octet-stream",
            "encoding": "identity",
            "verification": {"size": 3, "sha256": "1" * 64, "blake2b": "2" * 128},
        }
    ]
    manifest_a = {
        "schema_version": 1,
        "indexes": [
            {
                "channel": "stable",
                "platform": "x86_64-unknown-linux-gnu",
                "packages": [pkg_a],
            }
        ],
    }

    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel-cli/releases?per_page=100",
        json=[release_a_payload],
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
        json=[],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/101",
        content=canonical_json(manifest_a),
    )

    candidate_run_1 = tmp_path / "run1"
    shutil.copytree(main_repo, candidate_run_1)
    result_1 = build_candidate(candidate_run_1, client)

    assert len(result_1.records) == 1
    assert result_1.records[0].source.repository == "gelstable/gel-cli"
    assert result_1.records[0].source.release_id == 101
    assert result_1.rejected == ()
    assert (
        candidate_run_1 / "releases" / "gelstable" / "gel-cli" / "101.json"
    ).exists()

    for req in httpx_mock.get_requests():
        assert req.headers.get("authorization") == "Bearer test-rolling-token"
        assert req.headers.get("x-github-api-version") == "2022-11-28"

    httpx_mock.reset()

    # 3. release B appears before A is merged -> candidate rebuilds from main
    release_b_payload = {
        "id": 202,
        "tag_name": "v2.0.0",
        "published_at": "2026-08-17T00:00:00Z",
        "draft": False,
        "assets": [
            {
                "name": "gel-registry.json",
                "url": "https://api.github.com/assets/202",
            },
            {
                "name": "artifact",
                "url": "https://api.github.com/assets/202/artifact",
            },
        ],
    }
    pkg_b = package("gel-server", "2.0.0", ref_suffix="server-2.0.0")
    url_b = "https://github.com/gelstable/gel/releases/download/v2.0.0/artifact"
    pkg_b["installref"] = url_b
    pkg_b["installrefs"] = [
        {
            "ref": url_b,
            "type": "application/octet-stream",
            "encoding": "identity",
            "verification": {"size": 3, "sha256": "3" * 64, "blake2b": "4" * 128},
        }
    ]
    manifest_b = {
        "schema_version": 1,
        "indexes": [
            {
                "channel": "stable",
                "platform": "x86_64-unknown-linux-gnu",
                "packages": [pkg_b],
            }
        ],
    }

    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel-cli/releases?per_page=100",
        json=[release_a_payload],
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
        json=[release_b_payload],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/101",
        content=canonical_json(manifest_a),
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/202",
        content=canonical_json(manifest_b),
    )

    candidate_run_2 = tmp_path / "run2"
    shutil.copytree(main_repo, candidate_run_2)
    result_2 = build_candidate(candidate_run_2, client)

    assert {(r.source.repository, r.source.release_id) for r in result_2.records} == {
        ("gelstable/gel-cli", 101),
        ("gelstable/gel", 202),
    }
    assert (
        candidate_run_2 / "releases" / "gelstable" / "gel-cli" / "101.json"
    ).exists()
    assert (candidate_run_2 / "releases" / "gelstable" / "gel" / "202.json").exists()

    httpx_mock.reset()

    # 4. if A and B conflict on a replacement claim, candidate build aborts
    conflicting_sha = "c" * 64
    conflict_manifest_a = {
        "schema_version": 1,
        "replacements": [
            {
                "sha256": conflicting_sha,
                "url": "https://github.com/gelstable/gel-cli/releases/download/v1.0.1/artifact",
            }
        ],
    }
    conflict_manifest_b = {
        "schema_version": 1,
        "replacements": [
            {
                "sha256": conflicting_sha,
                "url": "https://github.com/gelstable/gel/releases/download/v2.0.0/artifact",
            }
        ],
    }

    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel-cli/releases?per_page=100",
        json=[release_a_payload],
    )
    httpx_mock.add_response(
        url="https://api.github.com/repos/gelstable/gel/releases?per_page=100",
        json=[release_b_payload],
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/101",
        content=canonical_json(conflict_manifest_a),
    )
    httpx_mock.add_response(
        url="https://api.github.com/assets/202",
        content=canonical_json(conflict_manifest_b),
    )

    candidate_run_3 = tmp_path / "run3"
    shutil.copytree(main_repo, candidate_run_3)
    with pytest.raises(ContestedReplacementError) as exc_info:
        build_candidate(candidate_run_3, client)

    assert conflicting_sha in str(exc_info.value)
    assert "gelstable/gel-cli@v1.0.1 (release 101)" in str(exc_info.value)
    assert "gelstable/gel@v2.0.0 (release 202)" in str(exc_info.value)
    assert not (candidate_run_3 / "pointers" / "latest.json").exists()


def test_rolling_promotion_script_sequence_a_then_b_and_conflict_aborts(
    promote: PromotionScript,
) -> None:
    """Script creates PR for A, edits for B, and aborts on conflict."""
    # Sequence Run 1: Release A appears. Candidate branch doesn't exist yet on remote.
    run1_responses = {
        ("git", "status", "--porcelain", "--untracked-files=all"): "",
        ("git", "ls-remote", "--heads", "origin", "refs/heads/promote/registry"): "",
        ("git", "fetch", "--no-tags", "origin", "main"): "",
        ("git", "switch", "--detach", "origin/main"): "",
        ("git", "switch", "-C", "promote/registry"): "",
        ("gel-registry", "build-candidate", "--repo", "."): json.dumps(
            {
                "records": ["releases/gelstable/gel-cli/101.json"],
                "rejected": [],
                "snapshot": "snap1",
            }
        ),
        (
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=no",
        ): " M releases/gelstable/gel-cli/101.json\n",
        ("git", "add", "--", "releases/", "pointers/", "public/", "vercel.toml"): "",
        (
            "git",
            "diff",
            "--name-only",
            "--cached",
        ): "releases/gelstable/gel-cli/101.json\n",
        ("git", "commit", "-m", "data: promote registry releases"): "",
        ("git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"): (
            "releases/gelstable/gel-cli/101.json\n"
        ),
        (
            "git",
            "push",
            "--force-with-lease=refs/heads/promote/registry:",
            "origin",
            "HEAD:refs/heads/promote/registry",
        ): "",
        (
            "gh",
            "pr",
            "list",
            "--head",
            "promote/registry",
            "--state",
            "open",
            "--json",
            "number",
        ): "[]\n",
        (
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            "promote/registry",
            "--title",
            "data: promote registry releases",
            "--body",
            (
                "## Added release records\n"
                "- `releases/gelstable/gel-cli/101.json`\n\n"
                "## Rejected releases\n- None"
            ),
        ): "",
    }
    recorder1 = Recorder(run1_responses)
    promote.main(run=recorder1)

    assert any(cmd[:3] == ["gh", "pr", "create"] for cmd in recorder1.commands)
    assert not any(cmd[:3] == ["gh", "pr", "edit"] for cmd in recorder1.commands)

    # Sequence Run 2: Release B appears before A is merged. PR #12 is open.
    run2_responses = {
        ("git", "status", "--porcelain", "--untracked-files=all"): "",
        ("git", "ls-remote", "--heads", "origin", "refs/heads/promote/registry"): (
            "oid1\trefs/heads/promote/registry\n"
        ),
        ("git", "fetch", "--no-tags", "origin", "main"): "",
        ("git", "fetch", "--no-tags", "origin", "promote/registry"): "",
        ("git", "merge-base", "origin/main", "origin/promote/registry"): "base1\n",
        ("git", "diff", "--name-only", "base1..origin/promote/registry"): (
            "releases/gelstable/gel-cli/101.json\n"
        ),
        ("git", "switch", "--detach", "origin/main"): "",
        ("git", "switch", "-C", "promote/registry"): "",
        ("gel-registry", "build-candidate", "--repo", "."): json.dumps(
            {
                "records": [
                    "releases/gelstable/gel-cli/101.json",
                    "releases/gelstable/gel/202.json",
                ],
                "rejected": [],
                "snapshot": "snap2",
            }
        ),
        (
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=no",
        ): (
            " M releases/gelstable/gel-cli/101.json\n"
            " M releases/gelstable/gel/202.json\n"
        ),
        ("git", "add", "--", "releases/", "pointers/", "public/", "vercel.toml"): "",
        ("git", "diff", "--name-only", "--cached"): (
            "releases/gelstable/gel-cli/101.json\nreleases/gelstable/gel/202.json\n"
        ),
        ("git", "commit", "-m", "data: promote registry releases"): "",
        ("git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"): (
            "releases/gelstable/gel-cli/101.json\nreleases/gelstable/gel/202.json\n"
        ),
        (
            "git",
            "push",
            "--force-with-lease=refs/heads/promote/registry:oid1",
            "origin",
            "HEAD:refs/heads/promote/registry",
        ): "",
        (
            "gh",
            "pr",
            "list",
            "--head",
            "promote/registry",
            "--state",
            "open",
            "--json",
            "number",
        ): '[{"number": 12}]\n',
        (
            "gh",
            "pr",
            "edit",
            "12",
            "--title",
            "data: promote registry releases",
            "--body",
            (
                "## Added release records\n"
                "- `releases/gelstable/gel-cli/101.json`\n"
                "- `releases/gelstable/gel/202.json`\n\n"
                "## Rejected releases\n- None"
            ),
        ): "",
    }
    recorder2 = Recorder(run2_responses)
    promote.main(run=recorder2)

    assert any(cmd[:4] == ["gh", "pr", "edit", "12"] for cmd in recorder2.commands)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in recorder2.commands)

    # Sequence Run 3: Conflict on replacement claim causes build-candidate to fail.
    # Script aborts; branch is not pushed and PR is not edited.
    class ErrorRunner(Recorder):
        def __call__(self, command: list[str]) -> str:
            if command[:2] == ["gel-registry", "build-candidate"]:
                raise RuntimeError(
                    "build-candidate failed: duplicate replacement claim"
                )
            return super().__call__(command)

    run3_responses = dict(run2_responses)
    recorder3 = ErrorRunner(run3_responses)

    with pytest.raises(RuntimeError, match="duplicate replacement claim"):
        promote.main(run=recorder3)

    assert not any(cmd[:2] == ["git", "commit"] for cmd in recorder3.commands)
    assert not any(cmd[:2] == ["git", "push"] for cmd in recorder3.commands)
    assert not any(cmd[:3] == ["gh", "pr"] for cmd in recorder3.commands)
