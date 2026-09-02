"""Command-line entry point for explicit local registry operator phases."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx

from . import candidate, capture, normalize, publication, render, validation
from .constants import CAPTURE_ID
from .contracts import CaptureManifest

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


def _load_capture_manifest(root: Path) -> CaptureManifest:
    manifest_path = root / "capture.json"
    return CaptureManifest.model_validate_json(manifest_path.read_bytes())


def _run_capture(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    destination = _capture_root(repo)
    with httpx.Client() as client:
        manifest = capture.capture_legacy(client, destination, args.captured_at)
    print(manifest.capture)
    return 0


def _run_verify_capture_live(args: argparse.Namespace) -> int:
    root = _capture_root(Path(args.repo))
    manifest = _load_capture_manifest(root)
    with httpx.Client() as client:
        capture.verify_live_capture(client, root, manifest)
    print("ok")
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
    result = publication.publish_registry(Path(args.repo))
    print(result.snapshot)
    return 0


def _run_build_candidate(args: argparse.Namespace) -> int:
    with httpx.Client() as client:
        result = candidate.build_candidate(Path(args.repo), client)
    print(
        json.dumps(
            {
                "snapshot": result.snapshot,
                "records": [
                    str(
                        candidate.release_record_path(
                            Path(args.repo), record
                        ).relative_to(args.repo)
                    )
                    for record in result.records
                ],
                "rejected": [
                    {
                        "repository": rejected.repository,
                        "release_id": rejected.release_id,
                        "tag": rejected.tag,
                        "reason": rejected.reason,
                    }
                    for rejected in result.rejected
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def _print_report(report: validation.ValidationReport) -> int:
    if report.ok:
        print("ok")
        return 0
    for error in report.errors:
        print(f"error: {_error_text(ValueError(error))}", file=sys.stderr)
    return 1


def _run_validate(args: argparse.Namespace) -> int:
    report = validation.validate_local(Path(args.repo), args.base)
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

    live_parser = commands.add_parser(
        "verify-capture-live", help="explicitly rehearse the capture remotely"
    )
    _add_repo(live_parser)
    live_parser.set_defaults(handler=_run_verify_capture_live)

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

    candidate_parser = commands.add_parser(
        "build-candidate", help="gather release manifests and publish a candidate"
    )
    _add_repo(candidate_parser)
    candidate_parser.set_defaults(handler=_run_build_candidate)

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
    validate_parser.set_defaults(handler=_run_validate)

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
    "candidate",
    "httpx",
    "main",
    "normalize",
    "publication",
    "render",
    "validation",
]
