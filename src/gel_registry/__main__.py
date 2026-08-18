"""Command-line entry point for explicit registry operator phases."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
from pydantic import ValidationError

from . import capture, github, normalize, promote, render, validate, verify
from .constants import CAPTURE_ID
from .schema import ReleaseRecord

type Handler = Callable[[argparse.Namespace], int]

_RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|\+00:00)"
)


def _capture_root(repo: Path) -> Path:
    return repo / "upstream" / "packages.geldata.com" / CAPTURE_ID


def _add_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: current directory)",
    )


def _parse_captured_at(value: str) -> datetime:
    """Parse the one timestamp supplied for a capture attempt."""

    candidate = value
    if _RFC3339_UTC.fullmatch(candidate) is None:
        raise argparse.ArgumentTypeError("captured-at must be an RFC3339 UTC timestamp")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "captured-at must be an RFC3339 UTC timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("captured-at must be an RFC3339 UTC timestamp")
    return parsed.astimezone(UTC)


def _error_text(error: BaseException) -> str:
    message = str(error).strip()
    if not message:
        return error.__class__.__name__
    return message.splitlines()[0]


def _print_report(report: validate.ValidationReport) -> int:
    if report.ok:
        print("ok")
        return 0
    for error in report.errors:
        print(f"error: {_error_text(ValueError(error))}", file=sys.stderr)
    return 1


def _run_capture(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    destination = _capture_root(repo)
    with httpx.Client() as client:
        manifest = capture.capture_legacy(client, destination, args.captured_at)
    print(manifest.capture)
    return 0


def _run_normalize(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    capture_root = _capture_root(repo)
    bootstrap_root = repo / "bootstrap"
    if args.check:
        normalize.validate_normalization(capture_root, bootstrap_root)
    else:
        normalize.normalize_capture(capture_root, bootstrap_root)
    print("ok")
    return 0


def _run_build_snapshot(args: argparse.Namespace) -> int:
    print(render.build_snapshot(Path(args.repo)))
    return 0


def _run_select_snapshot(args: argparse.Namespace) -> int:
    render.select_snapshot(Path(args.repo))
    print("ok")
    return 0


def _run_publish_bootstrap(args: argparse.Namespace) -> int:
    result = promote.publish_bootstrap(Path(args.repo))
    print(result.snapshot)
    return 0


def _known_versions(repo: Path) -> tuple[str, ...]:
    root = repo / "releases" / "gel-cli"
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"release records are not a directory: {root}")
    versions: list[str] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"release record is not a regular file: {path}")
        try:
            record = ReleaseRecord.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            raise ValueError(f"invalid release record {path}: {exc}") from exc
        versions.append(record.version)
    return tuple(versions)


def _requested_tag(value: str) -> str:
    tag = value.strip()
    if not tag:
        raise ValueError("release tag must not be empty")
    return tag if tag.startswith("v") else f"v{tag}"


def _select_releases(
    releases: Iterable[github.GitHubRelease], requested_tag: str | None
) -> tuple[github.GitHubRelease, ...]:
    discovered = tuple(releases)
    if requested_tag is None:
        return discovered
    selected = tuple(
        release for release in discovered if release.tag_name == requested_tag
    )
    if not selected:
        raise github.GitHubError(f"release tag was not discovered: {requested_tag}")
    return selected


def _run_promote(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    requested_tag = (
        _requested_tag(args.release_tag) if args.release_tag is not None else None
    )
    known = _known_versions(repo)
    if requested_tag is not None:
        requested_version = requested_tag[1:]
        known = tuple(version for version in known if version != requested_version)

    with httpx.Client() as client:
        releases = _select_releases(
            github.discover_cli_releases(client, known), requested_tag
        )
        if not releases:
            print("no new releases")
            return 0
        for release in releases:
            with tempfile.TemporaryDirectory(
                prefix=".gel-registry-assets-"
            ) as directory:
                assets = github.download_assets(
                    client, release, Path(directory) / "assets"
                )
                record = verify.verify_cli_release(release, assets)
            result = promote.promote_release(repo, record)
            print(result.snapshot)
    return 0


def _run_validate(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    report = validate.validate_local(repo, args.base)
    if args.remote_release:
        remote = validate.validate_release_remotes(repo, args.remote_release)
        report = validate.ValidationReport(
            errors=report.errors + remote.errors,
            checks=report.checks
            + tuple(check for check in remote.checks if check not in report.checks),
        )
    return _print_report(report)


def _run_validate_capture(args: argparse.Namespace) -> int:
    report = validate.validate_capture_local(Path(args.repo), args.base)
    return _print_report(report)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gel-registry")
    commands = parser.add_subparsers(dest="command", required=True)

    capture_parser = commands.add_parser("capture", help="capture legacy indexes")
    _add_repo(capture_parser)
    capture_parser.add_argument(
        "--captured-at",
        type=_parse_captured_at,
        required=True,
        help="one RFC3339 UTC timestamp for this capture attempt",
    )
    capture_parser.set_defaults(handler=_run_capture)

    normalize_parser = commands.add_parser(
        "normalize", help="normalize a frozen capture locally"
    )
    _add_repo(normalize_parser)
    normalize_parser.add_argument(
        "--check",
        action="store_true",
        help="verify committed bootstrap bytes instead of writing them",
    )
    normalize_parser.set_defaults(handler=_run_normalize)

    build_parser = commands.add_parser(
        "build-snapshot", help="build one immutable snapshot"
    )
    _add_repo(build_parser)
    build_parser.set_defaults(handler=_run_build_snapshot)

    select_parser = commands.add_parser(
        "select-snapshot", help="select an existing snapshot through the pointer"
    )
    _add_repo(select_parser)
    select_parser.set_defaults(handler=_run_select_snapshot)

    publish_parser = commands.add_parser(
        "publish-bootstrap", help="publish and select the bootstrap snapshot"
    )
    _add_repo(publish_parser)
    publish_parser.set_defaults(handler=_run_publish_bootstrap)

    promote_parser = commands.add_parser(
        "promote", help="verify and promote one or all new stable releases"
    )
    _add_repo(promote_parser)
    release_choice = promote_parser.add_mutually_exclusive_group(required=True)
    release_choice.add_argument(
        "--release-tag",
        "--tag",
        dest="release_tag",
        help="explicit v<semver> release tag",
    )
    release_choice.add_argument(
        "--all-new",
        action="store_true",
        help="promote every newly discovered stable release",
    )
    promote_parser.set_defaults(handler=_run_promote)

    validate_parser = commands.add_parser(
        "validate", help="run deterministic local validation"
    )
    _add_repo(validate_parser)
    validate_parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="optional merge-base tree for immutable-history checks",
    )
    validate_parser.add_argument(
        "--remote-release",
        type=Path,
        action="append",
        default=(),
        help="explicit release record to verify remotely",
    )
    validate_parser.set_defaults(handler=_run_validate)

    capture_validate_parser = commands.add_parser(
        "validate-capture", help="validate capture and bootstrap before publication"
    )
    _add_repo(capture_validate_parser)
    capture_validate_parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="optional merge-base tree for immutable capture-history checks",
    )
    capture_validate_parser.set_defaults(handler=_run_validate_capture)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch one explicit operator command and return its exit status."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = cast(Handler, args.handler)
    try:
        return handler(args)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"error: {_error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised by the console script
    raise SystemExit(main())


__all__ = [
    "capture",
    "github",
    "httpx",
    "main",
    "normalize",
    "promote",
    "render",
    "validate",
    "verify",
]
