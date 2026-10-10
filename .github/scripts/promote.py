#!/usr/bin/env python3
"""Maintain the cumulative registry promotion pull request."""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

ALLOWED_PREFIXES = (
    "releases/",
    "pointers/",
    "public/",
    "native/",
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


def _candidate_body(
    added: Sequence[str], rejected: Sequence[object], native_diff: str = ""
) -> str:
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
            "",
            "## Native package lock changes",
            "```diff",
            *(
                line
                for line in native_diff.splitlines()
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            ),
            "```",
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


def build(
    *, run: Runner = _run, cache: str = "/tmp/gel-registry-packages"
) -> dict[str, Any]:
    """Discover and render unsigned data, without any remote mutation."""
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
    run(
        [
            "gel-registry",
            "native",
            "render",
            "--repo",
            ".",
            "--cache",
            cache,
            "--out",
            "public",
        ]
    )
    changed = _paths_from_status(
        run(["git", "status", "--porcelain", "--untracked-files=all", "--ignored=no"])
    )
    _validate_paths(changed, description="candidate")
    return {
        "base_oid": run(["git", "rev-parse", "HEAD"]).strip(),
        "observed_oid": observed_oid,
        "rejected": result.get("rejected", []),
    }


def publish(result: dict[str, Any], *, run: Runner = _run) -> None:
    """Validate signed data and update the disposable candidate and PR."""
    changed = _paths_from_status(
        run(["git", "status", "--porcelain", "--untracked-files=all", "--ignored=no"])
    )
    _validate_paths(changed, description="candidate")
    observed_oid = result["observed_oid"]
    run(["gel-registry", "validate", "--repo", "."])
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
    native_diff = run(
        [
            "git",
            "diff",
            "--unified=0",
            "origin/main...HEAD",
            "--",
            "native/packages.lock.json",
        ]
    )
    _update_pr(run, _candidate_body(added, result.get("rejected", []), native_diff))


def write_artifact(
    destination: Path, result: dict[str, Any], *, run: Runner = _run
) -> None:
    """Transfer only changed generated files, plus explicit deletions."""
    status = run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--ignored=no"]
    )
    changed = _paths_from_status(status)
    _validate_paths(changed, description="artifact")
    destination.mkdir(parents=True, exist_ok=True)
    deleted = []
    with tarfile.open(destination / "candidate.tar", "w") as archive:
        for path in changed:
            source = Path(path)
            if source.is_symlink():
                raise RuntimeError(f"artifact path is a symlink: {path}")
            if source.exists():
                if not source.is_file():
                    raise RuntimeError(f"artifact path is not a file: {path}")
                archive.add(source, arcname=path, recursive=False)
            else:
                deleted.append(path)
    (destination / "deleted.json").write_text(json.dumps(deleted) + "\n")
    (destination / "result.json").write_text(json.dumps(result, sort_keys=True) + "\n")


def _safe_path(path: str) -> Path:
    parts = PurePosixPath(path)
    if (
        parts.is_absolute()
        or ".." in parts.parts
        or str(parts) != path
        or not _allowed(path)
    ):
        raise RuntimeError(f"unexpected artifact path: {path}")
    target = Path(path)
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise RuntimeError(f"artifact path traverses symlink: {path}")
    return target


def apply_artifact(source: Path, *, run: Runner = _run) -> dict[str, Any]:
    """Apply a validated artifact only to the exact clean default-branch base."""
    result: dict[str, Any] = json.loads((source / "result.json").read_text())
    if run(["git", "status", "--porcelain", "--untracked-files=all"]):
        raise RuntimeError("checkout is not clean")
    if run(["git", "rev-parse", "HEAD"]).strip() != result["base_oid"]:
        raise RuntimeError("default branch changed after render; rerun promotion")
    deleted = json.loads((source / "deleted.json").read_text())
    with tarfile.open(source / "candidate.tar") as archive:
        members = archive.getmembers()
        for member in members:
            _safe_path(member.name)
            if not member.isfile():
                raise RuntimeError(
                    f"artifact member is not a regular file: {member.name}"
                )
        for path in deleted:
            _safe_path(path)
        # Validate all names and types before mutating the checkout. The data
        # filter additionally rejects unsafe modes and archive links.
        for path in deleted:
            _safe_path(path).unlink(missing_ok=True)
        archive.extractall(filter="data")
    return result


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("build", "apply", "publish"))
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--cache", default="/tmp/gel-registry-packages")
    args = parser.parse_args()
    if args.phase == "build":
        write_artifact(args.artifact, build(cache=args.cache))
    elif args.phase == "apply":
        apply_artifact(args.artifact)
    else:
        result = json.loads((args.artifact / "result.json").read_text())
        if _run(["git", "rev-parse", "HEAD"]).strip() != result["base_oid"]:
            raise RuntimeError("candidate base differs from render base")
        publish(result)


if __name__ == "__main__":
    cli()
