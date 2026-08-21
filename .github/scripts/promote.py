"""Maintain the fixed rolling registry promotion branch and pull request."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


class CommandError(RuntimeError):
    """A required external command failed."""


class PromotionError(RuntimeError):
    """Promotion safety checks failed."""


BRANCH = "promote/registry"
_ALLOWED_PREFIXES = ("releases/", "pointers/", "public/")
_ALLOWED_PATHS = frozenset({"promotion/blocked.json"})
_COMMIT_MESSAGE = "data: update rolling registry promotion candidate"
_PR_TITLE = "data: promote registry"


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _run_checked(
    command: Sequence[str], *, cwd: Path | None = None, check: bool = True
) -> str:
    """Run one command without a shell and return its standard output."""

    result = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        detail = _first_line(result.stderr) or _first_line(result.stdout)
        suffix = f": {detail}" if detail else ""
        raise CommandError(f"{' '.join(command)} failed{suffix}")
    return result.stdout


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run git; kept as a small monkeypatchable command boundary."""

    return _run_checked(("git", *args), cwd=cwd, check=check)


def gh(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run gh; kept as a small monkeypatchable command boundary."""

    return _run_checked(("gh", *args), cwd=cwd, check=check)


def uv(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    """Run uv; kept as a small monkeypatchable command boundary."""

    return _run_checked(("uv", *args), cwd=cwd, check=check)


def _status_paths(status: str) -> tuple[str, ...]:
    paths: list[str] = []
    records = status.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " " or not record[3:]:
            raise PromotionError("invalid git status record")
        code = record[:2]
        paths.append(record[3:])
        if any(flag in code for flag in "RC"):
            if index >= len(records) or not records[index]:
                raise PromotionError("invalid git status rename record")
            paths.append(records[index])
            index += 1
    return tuple(paths)


def _name_status_paths(status: str) -> tuple[str, ...]:
    paths: list[str] = []
    records = status.split("\0")
    index = 0
    while index < len(records):
        code = records[index]
        index += 1
        if not code:
            continue
        if index >= len(records) or not records[index]:
            raise PromotionError("invalid git name-status record")
        if code[0] in "RC":
            source = records[index]
            index += 1
            if index >= len(records) or not records[index]:
                raise PromotionError("invalid git name-status rename record")
            paths.extend((source, records[index]))
            index += 1
        else:
            paths.append(records[index])
            index += 1
    return tuple(paths)


def _validate_allowed_paths(paths: Sequence[str], *, context: str) -> None:
    unexpected = sorted(
        path
        for path in paths
        if path
        and path not in _ALLOWED_PATHS
        and not path.startswith(_ALLOWED_PREFIXES)
    )
    if unexpected:
        raise PromotionError(
            f"{context} contains paths outside the promotion phase: "
            + " ".join(unexpected)
        )


def _remote_oid(repo: Path) -> str:
    raw = git("ls-remote", "--heads", "origin", BRANCH, cwd=repo)
    line = _first_line(raw)
    if not line:
        return ""
    fields = line.split()
    if len(fields) > 1 and fields[1] != f"refs/heads/{BRANCH}":
        raise PromotionError(f"unexpected remote ref for {BRANCH}: {fields[1]}")
    if not fields[0]:
        raise PromotionError(f"could not read remote OID for {BRANCH}")
    return fields[0]


def _prepare_branch(repo: Path, default_branch: str) -> str:
    if git("status", "--porcelain", cwd=repo).strip():
        raise PromotionError("repository must be clean before promotion")

    remote_oid = _remote_oid(repo)
    default_ref = f"refs/remotes/origin/{default_branch}"
    remote_ref = f"refs/remotes/origin/{BRANCH}"

    git("fetch", "--no-tags", "origin", default_branch, cwd=repo)
    if remote_oid:
        git(
            "fetch",
            "--no-tags",
            "origin",
            f"refs/heads/{BRANCH}:{remote_ref}",
            cwd=repo,
        )
        merge_base = _first_line(git("merge-base", default_ref, remote_ref, cwd=repo))
        if not merge_base:
            raise PromotionError(f"could not determine merge base for remote {BRANCH}")
        remote_paths = git(
            "diff", "--name-status", "-z", merge_base, remote_ref, cwd=repo
        )
        _validate_allowed_paths(
            _name_status_paths(remote_paths),
            context=f"remote {BRANCH}",
        )

    git("switch", "--detach", default_ref, cwd=repo)
    git("switch", "-C", BRANCH, cwd=repo)
    if git("status", "--porcelain", cwd=repo).strip():
        raise PromotionError(f"promotion branch {BRANCH} is not clean")
    return remote_oid


def _candidate_paths(repo: Path) -> tuple[str, ...]:
    status = git("status", "--porcelain=v1", "-z", cwd=repo)
    paths = _status_paths(status)
    _validate_allowed_paths(paths, context="candidate")
    return paths


def _candidate_summary(repo: Path, paths: Sequence[str]) -> str:
    releases = sorted(
        "/".join(Path(path).parts[1:])
        for path in paths
        if len(Path(path).parts) == 3
        and Path(path).parts[0] == "releases"
        and Path(path).parts[-1].endswith(".json")
    )

    blocked: list[dict[str, object]] = []
    blocked_path = repo / "promotion" / "blocked.json"
    if "promotion/blocked.json" in paths and blocked_path.is_file():
        try:
            payload = json.loads(blocked_path.read_text())
        except (OSError, TypeError, ValueError) as exc:
            raise PromotionError(f"invalid candidate blocked manifest: {exc}") from exc
        if not isinstance(payload, dict):
            raise PromotionError("invalid candidate blocked manifest: expected object")
        entries = payload.get("entries", ())
        if not isinstance(entries, list):
            raise PromotionError(
                "invalid candidate blocked manifest: entries must be an array"
            )
        blocked = [entry for entry in entries if isinstance(entry, dict)]

    release_lines = "\n".join(f"- `{path}`" for path in releases) or "- None"
    blocked_lines = (
        "\n".join(
            "- `{product}` `{tag}` ({category})".format(
                product=entry.get("product", "unknown"),
                tag=entry.get("tag", "unknown"),
                category=entry.get("category", "unknown"),
            )
            for entry in blocked
        )
        or "- None"
    )
    return (
        "## Rolling registry promotion\n\n"
        "Verified release records:\n"
        f"{release_lines}\n\n"
        "Blocked deterministic discoveries:\n"
        f"{blocked_lines}\n"
    )


def _open_pr(repo: Path, default_branch: str) -> str:
    return _first_line(
        gh(
            "pr",
            "list",
            "--state",
            "open",
            "--head",
            BRANCH,
            "--base",
            default_branch,
            "--json",
            "number",
            "--jq",
            ".[0].number",
            cwd=repo,
        )
    )


def _close_empty_candidate(repo: Path, default_branch: str, remote_oid: str) -> None:
    current_oid = _remote_oid(repo)
    if current_oid != remote_oid:
        raise PromotionError(f"remote {BRANCH} changed during empty-candidate cleanup")
    if remote_oid:
        lease = f"--force-with-lease=refs/heads/{BRANCH}:{remote_oid}"
        git("push", lease, "origin", "--delete", BRANCH, cwd=repo)
        if _remote_oid(repo):
            raise PromotionError(
                f"remote {BRANCH} changed during empty-candidate cleanup"
            )
    number = _open_pr(repo, default_branch)
    if number:
        gh("pr", "close", number, cwd=repo)


def _update_pr(repo: Path, default_branch: str, body: str) -> None:
    number = _open_pr(repo, default_branch)
    if number:
        gh(
            "pr",
            "edit",
            number,
            "--title",
            _PR_TITLE,
            "--body",
            body,
            cwd=repo,
        )
        return
    gh(
        "pr",
        "create",
        "--base",
        default_branch,
        "--head",
        BRANCH,
        "--title",
        _PR_TITLE,
        "--body",
        body,
        cwd=repo,
    )


def main(repo: Path | None = None) -> int:
    """Run rolling promotion from ``repo`` (or the current directory)."""

    root = Path.cwd() if repo is None else Path(repo)
    default_branch = os.environ.get("DEFAULT_BRANCH", "").strip()
    if not default_branch:
        print("DEFAULT_BRANCH is required", file=sys.stderr)
        return 1
    try:
        remote_oid = _prepare_branch(root, default_branch)
        uv(
            "run",
            "gel-registry",
            "build-candidate",
            "--repo",
            ".",
            cwd=root,
        )
        paths = _candidate_paths(root)
        if not paths:
            _close_empty_candidate(root, default_branch, remote_oid)
            return 0

        git("add", "--all", "--", ".", cwd=root)
        staged = _name_status_paths(
            git("diff", "--cached", "--name-status", "-z", cwd=root)
        )
        _validate_allowed_paths(staged, context="staged candidate")
        if not staged:
            raise PromotionError("candidate build produced no staged changes")
        body = _candidate_summary(root, staged)
        git("commit", "-m", _COMMIT_MESSAGE, cwd=root)
        if git("status", "--porcelain", cwd=root).strip():
            raise PromotionError("repository is not clean after promotion commit")
        lease = f"--force-with-lease=refs/heads/{BRANCH}:{remote_oid}"
        git(
            "push",
            lease,
            "origin",
            f"HEAD:refs/heads/{BRANCH}",
            cwd=root,
        )
        _update_pr(root, default_branch, body)
        return 0
    except (
        CommandError,
        OSError,
        PromotionError,
        subprocess.SubprocessError,
        ValueError,
    ) as exc:
        message = _first_line(str(exc)) or exc.__class__.__name__
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
