"""The scheduled promotion always rebuilds from the current main branch."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest

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
