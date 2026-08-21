"""Maintaining exactly one rolling promotion pull request.

`promote.py` is the only automation with write access, so its contract is
narrow: build the candidate once, and keep a single branch and a single pull
request in sync with it. Every path it touches on either side — what the remote
branch already contains, what the build left in the working tree — must fall
inside the promotion phase, and any surprise aborts before a push or a pull
request mutation. Pushes use `--force-with-lease` against the OID it read, so a
branch that moved underneath it is a conflict rather than a silent overwrite.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "promote.py"
BRANCH = "promote/registry"
REMOTE_OID = "0123456789abcdef0123456789abcdef01234567"
ADVANCED_OID = "fedcba9876543210fedcba9876543210fedcba98"
CANDIDATE_PATHS = ("releases/gel-cli/1.2.3.json", "public/registry.json")

Call = tuple[str, tuple[str, ...]]
Arrange = Callable[[ModuleType, pytest.MonkeyPatch, Path, list[Call]], str]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("promotion_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _name_status(paths: tuple[str, ...]) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


def _fake_commands(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_changes: bool = True,
    pr_number: str = "42",
    candidate_paths: tuple[str, ...] = CANDIDATE_PATHS,
    candidate_status: str | None = None,
    candidate_status_z: str | None = None,
    remote_exists: bool = True,
    remote_paths: str = "releases/gel-cli/1.2.3.json\npublic/registry.json\n",
    remote_status: str | None = None,
    build_error: Exception | None = None,
) -> list[Call]:
    """Stand in for git, gh and uv, recording every command the script runs."""

    calls: list[Call] = []
    state = {"built": False, "committed": False, "deleted": False}

    def fake_git(*args: str, **_kwargs: object) -> str:
        calls.append(("git", args))
        pending = state["built"] and candidate_changes and not state["committed"]
        if args[:2] == ("ls-remote", "--heads"):
            if not remote_exists or state["deleted"]:
                return ""
            return f"{REMOTE_OID}\trefs/heads/{BRANCH}\n"
        if args[:1] == ("push",) and "--delete" in args:
            state["deleted"] = True
            return ""
        if args[:1] == ("merge-base",):
            return "base-oid\n"
        if args[:2] == ("rev-parse", "refs/remotes/origin/main"):
            return "base-oid\n"
        if args[:2] == ("status", "--porcelain"):
            if pending:
                return candidate_status or "".join(
                    f" M {path}\n" for path in candidate_paths
                )
            return ""
        if args[:3] == ("status", "--porcelain=v1", "-z"):
            if pending:
                return candidate_status_z or "".join(
                    f" M {path}\0" for path in candidate_paths
                )
            return ""
        if args[:2] == ("diff", "--name-only"):
            return remote_paths
        if args[:3] == ("diff", "--name-status", "-z"):
            return remote_status or _name_status(
                tuple(path for path in remote_paths.splitlines() if path)
            )
        if args[:3] == ("diff", "--cached", "--name-only"):
            return (
                "".join(f"{path}\n" for path in candidate_paths)
                if candidate_changes
                else ""
            )
        if args[:4] == ("diff", "--cached", "--name-status", "-z"):
            return _name_status(candidate_paths) if candidate_changes else ""
        if args[:1] == ("commit",):
            state["committed"] = True
            return ""
        return ""

    def fake_gh(*args: str, **_kwargs: object) -> str:
        calls.append(("gh", args))
        return f"{pr_number}\n" if args[:2] == ("pr", "list") else ""

    def fake_uv(*args: str, **_kwargs: object) -> str:
        calls.append(("uv", args))
        if build_error is not None:
            raise build_error
        state["built"] = True
        return "snapshot-id\n"

    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setattr(module, "gh", fake_gh)
    monkeypatch.setattr(module, "uv", fake_uv)
    monkeypatch.setenv("DEFAULT_BRANCH", "main")
    return calls


def _pushes(calls: list[Call]) -> list[tuple[str, ...]]:
    return [args for kind, args in calls if kind == "git" and args[:1] == ("push",)]


def _pr_mutations(calls: list[Call]) -> list[tuple[str, ...]]:
    return [
        args
        for kind, args in calls
        if kind == "gh"
        and args[:2] in {("pr", "create"), ("pr", "edit"), ("pr", "close")}
    ]


def test_an_existing_rolling_pull_request_is_updated_rather_than_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    calls = _fake_commands(
        module, monkeypatch, remote_paths="releases/gel-cli/1.2.3.json\n"
    )
    original_git = cast(Callable[..., str], module.git)

    def merge_base_git(*args: str, **kwargs: object) -> str:
        if args[:2] == ("merge-base", "refs/remotes/origin/main"):
            calls.append(("git", args))
            return "rolling-base\n"
        if args[:3] == ("diff", "--name-status", "-z") and args[3] == "rolling-base":
            calls.append(("git", args))
            return "M\0releases/gel-cli/1.2.3.json\0"
        return original_git(*args, **kwargs)

    monkeypatch.setattr(module, "git", merge_base_git)

    assert module.main(tmp_path) == 0

    # The candidate is built exactly once.
    assert (
        calls.count(("uv", ("run", "gel-registry", "build-candidate", "--repo", ".")))
        == 1
    )
    # The retained branch is inspected against its own merge base with main,
    # not against main's tip.
    assert (
        "git",
        ("merge-base", "refs/remotes/origin/main", f"refs/remotes/origin/{BRANCH}"),
    ) in calls
    assert (
        "git",
        (
            "diff",
            "--name-status",
            "-z",
            "rolling-base",
            f"refs/remotes/origin/{BRANCH}",
        ),
    ) in calls
    # The push is leased against the OID that was read, and the pull request is
    # edited rather than created a second time.
    assert _pushes(calls) == [
        (
            "push",
            f"--force-with-lease=refs/heads/{BRANCH}:{REMOTE_OID}",
            "origin",
            f"HEAD:refs/heads/{BRANCH}",
        )
    ]
    assert [args[:2] for args in _pr_mutations(calls)] == [("pr", "edit")]


def test_exactly_one_pull_request_is_created_when_none_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    calls = _fake_commands(module, monkeypatch, pr_number="")

    assert module.main(tmp_path) == 0

    created = [args for args in _pr_mutations(calls) if args[:2] == ("pr", "create")]
    assert len(created) == 1
    assert BRANCH in created[0]


def test_an_empty_candidate_closes_the_pull_request_and_deletes_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    calls = _fake_commands(module, monkeypatch, candidate_changes=False)

    assert module.main(tmp_path) == 0

    assert ("gh", ("pr", "close", "42")) in calls
    assert _pushes(calls) == [
        (
            "push",
            f"--force-with-lease=refs/heads/{BRANCH}:{REMOTE_OID}",
            "origin",
            "--delete",
            BRANCH,
        )
    ]
    assert not any(kind == "git" and args[:1] == ("commit",) for kind, args in calls)


def _build_failure(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, _root: Path, calls: list[Call]
) -> str:
    calls.extend(
        _fake_commands(
            module, monkeypatch, build_error=module.CommandError("upstream unavailable")
        )
    )
    return ""


def _malformed_candidate_summary(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, root: Path, calls: list[Call]
) -> str:
    (root / "promotion").mkdir()
    (root / "promotion" / "blocked.json").write_text("{")
    calls.extend(
        _fake_commands(module, monkeypatch, candidate_paths=("promotion/blocked.json",))
    )
    return "invalid candidate blocked manifest"


def _unexpected_remote_path(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, _root: Path, calls: list[Call]
) -> str:
    calls.extend(_fake_commands(module, monkeypatch, remote_paths="README.md\n"))
    return "outside the promotion phase"


def _remote_rename_from_an_unexpected_source(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, _root: Path, calls: list[Call]
) -> str:
    calls.extend(
        _fake_commands(
            module,
            monkeypatch,
            remote_paths="releases/gel-cli/1.2.3.json\n",
            remote_status="R100\0README.md\0releases/gel-cli/1.2.3.json\0",
        )
    )
    return "outside the promotion phase"


def _candidate_rename_from_an_unexpected_source(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, _root: Path, calls: list[Call]
) -> str:
    calls.extend(
        _fake_commands(
            module,
            monkeypatch,
            candidate_paths=("releases/gel-cli/1.2.3.json",),
            candidate_status="R  README.md -> releases/gel-cli/1.2.3.json\n",
            candidate_status_z="R  releases/gel-cli/1.2.3.json\0README.md\0",
        )
    )
    return "outside the promotion phase"


def _unexpected_candidate_path(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, _root: Path, calls: list[Call]
) -> str:
    calls.extend(_fake_commands(module, monkeypatch))
    original_git = cast(Callable[..., str], module.git)
    reads = 0

    def unexpected_status(*args: str, **kwargs: object) -> str:
        nonlocal reads
        is_status = args[:2] == ("status", "--porcelain") or args[:3] == (
            "status",
            "--porcelain=v1",
            "-z",
        )
        if is_status:
            reads += 1
            calls.append(("git", args))
            if reads < 3:
                return ""
            return (
                " M README.md\0"
                if args[:3] == ("status", "--porcelain=v1", "-z")
                else " M README.md\n"
            )
        return original_git(*args, **kwargs)

    monkeypatch.setattr(module, "git", unexpected_status)
    return "outside the promotion phase"


def _dirty_tree_after_commit(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, _root: Path, calls: list[Call]
) -> str:
    calls.extend(_fake_commands(module, monkeypatch))
    original_git = cast(Callable[..., str], module.git)
    reads = 0

    def dirty_after_commit(*args: str, **kwargs: object) -> str:
        nonlocal reads
        if args[:2] == ("status", "--porcelain"):
            reads += 1
            calls.append(("git", args))
            if reads == 4:
                return " M README.md\n"
            if reads == 3:
                return " M releases/gel-cli/1.2.3.json\n"
            return ""
        return original_git(*args, **kwargs)

    monkeypatch.setattr(module, "git", dirty_after_commit)
    return "clean after promotion commit"


@pytest.mark.parametrize(
    "arrange",
    [
        pytest.param(_build_failure, id="transient-build-failure"),
        pytest.param(_malformed_candidate_summary, id="malformed-candidate-summary"),
        pytest.param(_unexpected_remote_path, id="unexpected-remote-path"),
        pytest.param(
            _remote_rename_from_an_unexpected_source, id="remote-rename-source"
        ),
        pytest.param(
            _candidate_rename_from_an_unexpected_source, id="candidate-rename-source"
        ),
        pytest.param(_unexpected_candidate_path, id="unexpected-candidate-path"),
        pytest.param(_dirty_tree_after_commit, id="dirty-tree-after-commit"),
    ],
)
def test_a_surprise_on_either_side_stops_before_any_push_or_pr_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arrange: Arrange,
) -> None:
    module = _load_script()
    calls: list[Call] = []
    message = arrange(module, monkeypatch, tmp_path, calls)

    assert module.main(tmp_path) == 1

    if message:
        assert message in capsys.readouterr().err
    assert _pushes(calls) == []
    assert _pr_mutations(calls) == []


def test_the_lease_aborts_when_the_remote_branch_advances_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script()
    calls = _fake_commands(module, monkeypatch, candidate_changes=False)
    original_git = cast(Callable[..., str], module.git)
    reads = 0

    def advancing_remote(*args: str, **kwargs: object) -> str:
        nonlocal reads
        if args[:2] == ("ls-remote", "--heads"):
            reads += 1
            calls.append(("git", args))
            oid = REMOTE_OID if reads == 1 else ADVANCED_OID
            return f"{oid}\trefs/heads/{BRANCH}\n"
        return original_git(*args, **kwargs)

    monkeypatch.setattr(module, "git", advancing_remote)

    assert module.main(tmp_path) == 1

    assert "changed during empty-candidate cleanup" in capsys.readouterr().err
    assert _pushes(calls) == []
    assert _pr_mutations(calls) == []
