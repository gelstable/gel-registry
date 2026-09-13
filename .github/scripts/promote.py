#!/usr/bin/env python3
"""Maintain the cumulative registry promotion pull request."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

ALLOWED_PREFIXES = (
    "releases/",
    "pointers/",
    "public/",
)
ALLOWED_EXACT = frozenset({"vercel.toml"})
BRANCH = "promote/registry"
TITLE = "data: promote registry releases"

Runner = Callable[[list[str]], str]


def _run(command: list[str]) -> str:
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout


def _paths_from_status(status: str) -> list[str]:
    return [line[3:] for line in status.splitlines() if line]


def _allowed(path: str) -> bool:
    return path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIXES)


def _validate_paths(paths: Sequence[str], *, description: str) -> None:
    unexpected = sorted(path for path in paths if path and not _allowed(path))
    if unexpected:
        raise RuntimeError(f"unexpected {description} path: {unexpected[0]}")


def _remote_oid(run: Runner) -> str:
    output = run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{BRANCH}"])
    return output.split("\t", maxsplit=1)[0].strip() if output else ""


def _candidate_body(added: Sequence[str], rejected: Sequence[object]) -> str:
    added_lines = [f"- `{path}`" for path in added]
    rejected_lines = [
        "- " + json.dumps(item, sort_keys=True)
        for item in rejected
        if isinstance(item, dict)
    ]
    return "\n".join(
        [
            "## Added release records",
            *(added_lines or ["- None"]),
            "",
            "## Rejected releases",
            *(rejected_lines or ["- None"]),
        ]
    )


def _open_pr_numbers(run: Runner) -> list[int]:
    output = run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            BRANCH,
            "--state",
            "open",
            "--json",
            "number",
        ]
    )
    data: Any = json.loads(output) if output.strip() else []
    return [item["number"] for item in data if isinstance(item.get("number"), int)]


def _update_pr(run: Runner, body: str) -> None:
    numbers = _open_pr_numbers(run)
    if numbers:
        run(["gh", "pr", "edit", str(numbers[0]), "--title", TITLE, "--body", body])
    else:
        run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--head",
                BRANCH,
                "--title",
                TITLE,
                "--body",
                body,
            ]
        )


def _delete_candidate_and_close_pr(run: Runner, observed_oid: str) -> None:
    if observed_oid:
        run(
            [
                "git",
                "push",
                f"--force-with-lease=refs/heads/{BRANCH}:{observed_oid}",
                "origin",
                f":refs/heads/{BRANCH}",
            ]
        )
    for number in _open_pr_numbers(run):
        run(["gh", "pr", "close", str(number)])


def main(*, run: Runner = _run) -> None:
    """Rebuild, publish, and describe the one rolling promotion candidate."""
    if run(["git", "status", "--porcelain", "--untracked-files=all"]):
        raise RuntimeError("checkout is not clean")
    observed_oid = _remote_oid(run)
    run(["git", "fetch", "--no-tags", "origin", "main"])
    if observed_oid:
        run(["git", "fetch", "--no-tags", "origin", BRANCH])
        merge_base = run(
            ["git", "merge-base", "origin/main", f"origin/{BRANCH}"]
        ).strip()
        _validate_paths(
            run(
                ["git", "diff", "--name-only", f"{merge_base}..origin/{BRANCH}"]
            ).splitlines(),
            description="existing candidate",
        )

    run(["git", "switch", "--detach", "origin/main"])
    run(["git", "switch", "-C", BRANCH])
    result: Any = json.loads(run(["gel-registry", "build-candidate", "--repo", "."]))
    changed = _paths_from_status(
        run(["git", "status", "--porcelain", "--untracked-files=all", "--ignored=no"])
    )
    _validate_paths(changed, description="candidate")
    if not changed:
        _delete_candidate_and_close_pr(run, observed_oid)
        return

    run(["git", "add", "--", *ALLOWED_PREFIXES, *sorted(ALLOWED_EXACT)])
    staged = run(["git", "diff", "--name-only", "--cached"]).splitlines()
    _validate_paths(staged, description="staged candidate")
    run(
        [
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            TITLE,
        ]
    )
    added = [
        path
        for path in run(
            ["git", "diff", "--name-only", "--diff-filter=A", "origin/main...HEAD"]
        ).splitlines()
        if path.startswith("releases/") and path.endswith(".json")
    ]
    run(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{BRANCH}:{observed_oid}",
            "origin",
            f"HEAD:refs/heads/{BRANCH}",
        ]
    )
    _update_pr(run, _candidate_body(added, result.get("rejected", [])))


if __name__ == "__main__":
    main()
