"""Orchestrate stable Gel CLI promotion from a checked-out repository."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class CommandError(RuntimeError):
    """A required external command failed."""


class PromotionError(RuntimeError):
    """Promotion safety checks failed."""


_TAG = re.compile(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)([-+][0-9A-Za-z.-]+)?$")
_ALLOWED_PREFIXES = ("releases/gel-cli/", "pointers/", "public/")


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


def _version_key(tag: str) -> tuple[Any, ...]:
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"invalid release tag: {tag}")
    major, minor, patch = (int(value) for value in match.group(1, 2, 3))
    suffix = match.group(4) or ""
    prerelease = ""
    build = ""
    if suffix.startswith("-"):
        prerelease, _, build = suffix[1:].partition("+")
    elif suffix.startswith("+"):
        build = suffix[1:]

    def identifier_key(identifier: str) -> tuple[int, int | str]:
        return (0, int(identifier)) if identifier.isdigit() else (1, identifier)

    prerelease_key = tuple(identifier_key(item) for item in prerelease.split("."))
    # A release without a prerelease sorts after all prereleases.  The final
    # tag tie-breaker keeps build metadata and otherwise equivalent tags stable.
    return (major, minor, patch, 1 if not prerelease else 0, prerelease_key, build, tag)


def _discover_tags() -> tuple[str, ...]:
    raw = gh("api", "--paginate", "--slurp", "repos/gelstable/gel-cli/releases")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid release discovery response") from exc

    pages: list[object]
    if isinstance(payload, list) and all(isinstance(page, list) for page in payload):
        pages = [item for page in payload for item in page]
    elif isinstance(payload, list):
        pages = payload
    else:
        raise ValueError("invalid release discovery response")

    tags: set[str] = set()
    for release in pages:
        if not isinstance(release, dict):
            continue
        if release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        tag = release.get("tag_name")
        if isinstance(tag, str) and _TAG.fullmatch(tag):
            tags.add(tag)
    return tuple(sorted(tags, key=_version_key))


def _branch_paths_are_allowed(paths: str) -> bool:
    return all(
        not path or path.startswith(_ALLOWED_PREFIXES) for path in paths.splitlines()
    )


def _promote_tag(repo: Path, default_branch: str, tag: str) -> None:
    version = tag[1:]
    record = repo / "releases" / "gel-cli" / f"{version}.json"
    if record.is_file():
        print(f"release {tag} is already recorded")
        return

    branch = f"promote/gel-cli-{version}"
    remote_exists = bool(
        git(
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            branch,
            cwd=repo,
            check=False,
        )
    )
    if remote_exists:
        git(
            "fetch",
            "--no-tags",
            "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
            cwd=repo,
        )
        paths = git(
            "diff",
            "--name-only",
            f"refs/remotes/origin/{default_branch}",
            f"refs/remotes/origin/{branch}",
            cwd=repo,
        )
        if not _branch_paths_are_allowed(paths):
            unexpected = " ".join(
                path
                for path in paths.splitlines()
                if path and not path.startswith(_ALLOWED_PREFIXES)
            )
            raise PromotionError(
                "remote "
                f"{branch} contains paths outside the promotion phase: {unexpected}"
            )
        git("switch", "--detach", f"refs/remotes/origin/{branch}", cwd=repo)
        git("switch", "-C", branch, cwd=repo)
    else:
        git("fetch", "--no-tags", "origin", default_branch, cwd=repo)
        git("switch", "--detach", f"refs/remotes/origin/{default_branch}", cwd=repo)
        git("switch", "-c", branch, cwd=repo)

    if git("status", "--porcelain", cwd=repo):
        raise PromotionError(f"promotion branch {branch} is not clean")

    uv(
        "run",
        "gel-registry",
        "promote",
        "--repo",
        ".",
        "--release-tag",
        tag,
        cwd=repo,
    )
    uv("run", "gel-registry", "validate", "--repo", ".", cwd=repo)

    git("add", "--", "releases/gel-cli", "pointers", "public", cwd=repo)
    staged = git("diff", "--cached", "--name-only", cwd=repo)
    unexpected_paths = [
        path
        for path in staged.splitlines()
        if path and not path.startswith(_ALLOWED_PREFIXES)
    ]
    if unexpected_paths:
        raise PromotionError(
            "promotion branch staged an unexpected path: " + " ".join(unexpected_paths)
        )

    if staged.strip():
        git("commit", "-m", f"data: promote gel-cli {version}", cwd=repo)
        if remote_exists:
            git("push", "origin", branch, cwd=repo)
        else:
            git("push", "--set-upstream", "origin", branch, cwd=repo)

    number = _first_line(
        gh(
            "pr",
            "list",
            "--state",
            "open",
            "--head",
            branch,
            "--base",
            default_branch,
            "--json",
            "number",
            "--jq",
            ".[0].number",
            cwd=repo,
        )
    )
    if not number:
        gh(
            "pr",
            "create",
            "--base",
            default_branch,
            "--head",
            branch,
            "--title",
            f"data: promote gel-cli {version}",
            "--body",
            "Review the verified stable Gel CLI release record and immutable snapshot.",
            cwd=repo,
        )
    else:
        print(f"reusing promotion pull request #{number}")

    git("fetch", "--no-tags", "origin", default_branch, cwd=repo)
    git("switch", "--detach", f"refs/remotes/origin/{default_branch}", cwd=repo)


def main(repo: Path | None = None) -> int:
    """Run promotion from ``repo`` (or the current directory)."""

    root = Path.cwd() if repo is None else Path(repo)
    default_branch = os.environ.get("DEFAULT_BRANCH", "").strip()
    if not default_branch:
        print("DEFAULT_BRANCH is required", file=sys.stderr)
        return 1
    try:
        git("fetch", "--no-tags", "origin", default_branch, cwd=root)
        try:
            tags = _discover_tags()
        except Exception as exc:
            del exc
            print("could not discover stable Gel CLI releases", file=sys.stderr)
            return 1
        if not tags:
            print("no stable Gel CLI releases discovered")
            return 0
        for tag in tags:
            _promote_tag(root, default_branch, tag)
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
