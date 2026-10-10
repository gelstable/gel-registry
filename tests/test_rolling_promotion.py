"""The scheduled promotion always rebuilds from the current main branch."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "promote.py"


class PromotionScript(Protocol):
    def build(
        self,
        *,
        run: Callable[[list[str]], str],
        cache: str = "/tmp/gel-registry-packages",
    ) -> dict[str, Any]: ...
    def publish(
        self, result: dict[str, Any], *, run: Callable[[list[str]], str]
    ) -> None: ...


def _build_and_publish(
    promote: PromotionScript, *, run: Callable[[list[str]], str]
) -> None:
    """Unit-test build/publish contracts; full transport is tested separately."""
    promote.publish(promote.build(run=run), run=run)


@pytest.fixture
def promote() -> PromotionScript:
    spec = importlib.util.spec_from_file_location("promote", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(PromotionScript, module)


def _is_git_subcommand(command: list[str], subcommand: str) -> bool:
    arguments = command[1:]
    while len(arguments) >= 2 and arguments[0] == "-c":
        arguments = arguments[2:]
    return bool(command) and command[0] == "git" and arguments[:1] == [subcommand]


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

    _build_and_publish(promote, run=recorder)

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
        _build_and_publish(promote, run=recorder)

    assert not any(
        _is_git_subcommand(command, "commit") for command in recorder.commands
    )
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
        _build_and_publish(promote, run=recorder)

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
        _build_and_publish(promote, run=recorder)

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

    _build_and_publish(promote, run=recorder)

    assert [
        "git",
        "push",
        "--force-with-lease=refs/heads/promote/registry:abc123",
        "origin",
        ":refs/heads/promote/registry",
    ] in recorder.commands
    assert ["gh", "pr", "close", "42"] in recorder.commands
    assert not any(
        _is_git_subcommand(command, "commit") for command in recorder.commands
    )


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

    _build_and_publish(promote, run=recorder)

    assert any(
        command[:4] == ["gh", "pr", "edit", "42"] for command in recorder.commands
    )
    assert not any(
        command[:3] == ["gh", "pr", "create"] for command in recorder.commands
    )


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
        (
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            "data: promote registry releases",
        ): "",
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
    _build_and_publish(promote, run=recorder1)

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
        (
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            "data: promote registry releases",
        ): "",
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
    _build_and_publish(promote, run=recorder2)

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
        _build_and_publish(promote, run=recorder3)

    assert not any(_is_git_subcommand(cmd, "commit") for cmd in recorder3.commands)
    assert not any(cmd[:2] == ["git", "push"] for cmd in recorder3.commands)
    assert not any(cmd[:3] == ["gh", "pr"] for cmd in recorder3.commands)


def test_build_has_no_signing_or_remote_mutations(promote: PromotionScript) -> None:
    recorder = Recorder(_responses())
    result = promote.build(run=recorder, cache="/tmp/cache")
    assert result["rejected"] == []
    assert [
        "gel-registry",
        "native",
        "render",
        "--repo",
        ".",
        "--cache",
        "/tmp/cache",
        "--out",
        "public",
    ] in recorder.commands
    assert not any(command[:2] == ["git", "push"] for command in recorder.commands)
    assert not any(command[:2] == ["gh", "pr"] for command in recorder.commands)
    assert not any(
        _is_git_subcommand(command, "commit") for command in recorder.commands
    )
    assert not any(
        command[:2] == ["gel-registry", "validate"] for command in recorder.commands
    )


def test_publish_validates_before_committing_and_never_downloads(
    promote: PromotionScript,
) -> None:
    recorder = Recorder(_responses())
    promote.publish({"observed_oid": "abc123", "rejected": []}, run=recorder)
    validate = recorder.commands.index(["gel-registry", "validate", "--repo", "."])
    commit = next(
        i
        for i, cmd in enumerate(recorder.commands)
        if _is_git_subcommand(cmd, "commit")
    )
    assert validate < commit
    assert not any(
        command[:2] == ["gel-registry", "build-candidate"]
        or command[:3] == ["gel-registry", "native", "render"]
        for command in recorder.commands
    )


def test_artifact_roundtrip_and_stale_base_rejection(
    promote: PromotionScript, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "artifact"
    builder = tmp_path / "builder"
    builder.mkdir()
    monkeypatch.chdir(builder)
    (builder / "native").mkdir()
    (builder / "native/packages.lock.json").write_text("[]\n")
    recorder = Recorder(
        _responses(status="?? native/packages.lock.json\n D public/old.json\n")
    )
    result = {"base_oid": "base", "observed_oid": "abc123"}
    promote.write_artifact(artifact, result, run=recorder)  # type: ignore[attr-defined]
    checkout = tmp_path / "checkout"
    (checkout / "public").mkdir(parents=True)
    (checkout / "public/old.json").write_text("old")
    monkeypatch.chdir(checkout)
    stale = Recorder({("git", "rev-parse", "HEAD"): "new-base\n"})
    with pytest.raises(RuntimeError, match="default branch changed"):
        promote.apply_artifact(artifact, run=stale)  # type: ignore[attr-defined]
    assert (checkout / "public/old.json").exists()
    current = Recorder({("git", "rev-parse", "HEAD"): "base\n"})
    assert promote.apply_artifact(artifact, run=current) == result  # type: ignore[attr-defined]
    assert (checkout / "native/packages.lock.json").read_text() == "[]\n"
    assert not (checkout / "public/old.json").exists()


@pytest.mark.parametrize(
    "name,link", [("../escaped", False), ("src/code.py", False), ("public/link", True)]
)
def test_unsafe_artifact_rejected_before_deletions(
    promote: PromotionScript,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    link: bool,
) -> None:
    import io
    import tarfile

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "result.json").write_text('{"base_oid": "base"}')
    (artifact / "deleted.json").write_text('["public/keep"]')
    with tarfile.open(artifact / "candidate.tar", "w") as archive:
        member = tarfile.TarInfo(name)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = "../../escaped"
            archive.addfile(member)
        else:
            member.size = 4
            archive.addfile(member, io.BytesIO(b"evil"))
    checkout = tmp_path / "checkout"
    (checkout / "public").mkdir(parents=True)
    (checkout / "public/keep").write_text("keep")
    monkeypatch.chdir(checkout)
    recorder = Recorder({("git", "rev-parse", "HEAD"): "base\n"})
    with pytest.raises(RuntimeError, match="artifact"):
        promote.apply_artifact(artifact, run=recorder)  # type: ignore[attr-defined]
    assert (checkout / "public/keep").read_text() == "keep"


def test_native_lock_diff_is_in_pr_body(promote: PromotionScript) -> None:
    responses = _responses()
    responses[
        (
            "git",
            "diff",
            "--unified=0",
            "origin/main...HEAD",
            "--",
            "native/packages.lock.json",
        )
    ] = (
        "--- a/native/packages.lock.json\n+++ b/native/packages.lock.json\n"
        '-    "name": "gel-old",\n+    "name": "gel-cli",\n'
    )
    recorder = Recorder(responses)
    _build_and_publish(promote, run=recorder)
    create = next(cmd for cmd in recorder.commands if cmd[:3] == ["gh", "pr", "create"])
    body = create[-1]
    assert '-    "name": "gel-old",' in body
    assert '+    "name": "gel-cli",' in body
    assert "--- a/native" not in body
