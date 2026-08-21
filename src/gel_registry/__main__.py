"""Command-line entry point for explicit local registry operator phases."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx

from . import capture, normalize
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
    "httpx",
    "main",
    "normalize",
]
