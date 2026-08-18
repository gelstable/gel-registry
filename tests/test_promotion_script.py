from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "promote.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("promotion_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_eligible_releases_is_a_successful_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_gh(*args: str, **kwargs: object) -> str:
        calls.append(("gh", args))
        return '[[{"draft": true, "prerelease": false, "tag_name": "v1.0.0"}]]'

    def record(kind: str, *args: str, **kwargs: object) -> str:
        calls.append((kind, args))
        return ""

    monkeypatch.setattr(module, "gh", fake_gh)
    monkeypatch.setattr(
        module, "git", lambda *args, **kwargs: record("git", *args, **kwargs)
    )
    monkeypatch.setattr(
        module, "uv", lambda *args, **kwargs: record("uv", *args, **kwargs)
    )
    monkeypatch.setenv("DEFAULT_BRANCH", "main")

    assert module.main(tmp_path) == 0
    assert "no stable Gel CLI releases discovered" in capsys.readouterr().out
    assert not any(kind == "uv" for kind, _ in calls)


def test_discovery_failure_has_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()

    def fail(*args: str, **kwargs: object) -> str:
        raise module.CommandError("gh failed")

    monkeypatch.setattr(module, "gh", fail)

    def fake_git(*args: str, **kwargs: object) -> str:
        return ""

    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setenv("DEFAULT_BRANCH", "main")

    assert module.main(tmp_path) == 1
    assert capsys.readouterr().err.strip() == (
        "could not discover stable Gel CLI releases"
    )


def test_happy_path_requests_promotion_lifecycle_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_gh(*args: str, **kwargs: object) -> str:
        calls.append(("gh", args))
        if args[:2] == ("api", "--paginate"):
            return (
                '[[{"draft": false, "prerelease": false, "tag_name": "v1.2.3"}, '
                '{"draft": true, "prerelease": false, "tag_name": "v9.0.0"}, '
                '{"draft": false, "prerelease": false, "tag_name": "bad"}]]'
            )
        return ""

    def fake_git(*args: str, **kwargs: object) -> str:
        calls.append(("git", args))
        if args[:2] == ("ls-remote", "--exit-code"):
            return ""
        if args[:3] == ("diff", "--cached", "--name-only"):
            return "releases/gel-cli/1.2.3.json\npointers/latest.json"
        return ""

    def fake_uv(*args: str, **kwargs: object) -> str:
        calls.append(("uv", args))
        return ""

    monkeypatch.setattr(module, "gh", fake_gh)
    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setattr(module, "uv", fake_uv)
    monkeypatch.setenv("DEFAULT_BRANCH", "main")

    assert module.main(tmp_path) == 0
    uv_positions = [index for index, (kind, _) in enumerate(calls) if kind == "uv"]
    commit_position = calls.index(
        ("git", ("commit", "-m", "data: promote gel-cli 1.2.3"))
    )
    push_position = calls.index(
        ("git", ("push", "--set-upstream", "origin", "promote/gel-cli-1.2.3"))
    )
    pr_position = next(
        index
        for index, (kind, args) in enumerate(calls)
        if kind == "gh" and args[:2] == ("pr", "create")
    )
    assert uv_positions == sorted(uv_positions)
    assert uv_positions[0] < commit_position < push_position < pr_position


def test_retained_branch_with_unrelated_path_fails_before_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_gh(*args: str, **kwargs: object) -> str:
        calls.append(("gh", args))
        return '[[{"draft": false, "prerelease": false, "tag_name": "v1.2.3"}]]'

    def fake_git(*args: str, **kwargs: object) -> str:
        calls.append(("git", args))
        if args[:2] == ("ls-remote", "--exit-code"):
            return "refs/remotes/origin/promote/gel-cli-1.2.3"
        if args[:2] == ("diff", "--name-only"):
            return "README.md"
        return ""

    monkeypatch.setattr(module, "gh", fake_gh)
    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setenv("DEFAULT_BRANCH", "main")

    assert module.main(tmp_path) == 1
    assert "outside the promotion phase" in capsys.readouterr().err
    assert not any(
        args[:2] == ("switch", "--detach") for kind, args in calls if kind == "git"
    )


def test_unexpected_staged_path_fails_before_commit_or_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_gh(*args: str, **kwargs: object) -> str:
        calls.append(("gh", args))
        if args[:2] == ("api", "--paginate"):
            return '[[{"draft": false, "prerelease": false, "tag_name": "v1.2.3"}]]'
        return ""

    def fake_git(*args: str, **kwargs: object) -> str:
        calls.append(("git", args))
        if args[:2] == ("diff", "--cached") and args[-1] == "--name-only":
            return "releases/gel-cli/1.2.3.json\nother.txt"
        return ""

    monkeypatch.setattr(module, "gh", fake_gh)
    monkeypatch.setattr(module, "git", fake_git)

    def fake_uv(*args: str, **kwargs: object) -> str:
        calls.append(("uv", args))
        return ""

    monkeypatch.setattr(module, "uv", fake_uv)
    monkeypatch.setenv("DEFAULT_BRANCH", "main")

    assert module.main(tmp_path) == 1
    assert "staged an unexpected path" in capsys.readouterr().err
    assert not any(args[:1] == ("commit",) for kind, args in calls if kind == "git")
    assert not any(
        kind == "gh" and args[:2] == ("pr", "create") for kind, args in calls
    )
