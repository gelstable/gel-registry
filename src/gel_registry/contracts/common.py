"""Shared primitives for every registry wire contract.

Model configuration, digest and URL validators, and the strict SemVer
parser used by the capture, legacy, release and public documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import ConfigDict, StringConstraints

MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    populate_by_name=True,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX128 = re.compile(r"^[0-9a-f]{128}$")
SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True, slots=True)
class SemVer:
    """Strict SemVer components and prerelease identifiers."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...]


def parse_semver(value: str) -> SemVer | None:
    """Parse the repository's strict ASCII SemVer contract."""

    match = _SEMVER.fullmatch(value)
    if match is None:
        return None
    prerelease: list[int | str] = []
    raw_prerelease = match.group(4)
    if raw_prerelease is not None:
        for identifier in raw_prerelease.split("."):
            if identifier.isdigit():
                if len(identifier) > 1 and identifier.startswith("0"):
                    return None
                prerelease.append(int(identifier))
            else:
                prerelease.append(identifier)
    return SemVer(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        prerelease=tuple(prerelease),
    )


def semver_key(
    value: str,
) -> tuple[int, int, int, tuple[tuple[int, int | str], ...], int]:
    """Return the SemVer precedence key, ignoring build metadata."""

    parsed = parse_semver(value)
    if parsed is None:
        raise ValueError(f"invalid SemVer: {value!r}")
    if not parsed.prerelease:
        prerelease_key: tuple[tuple[int, int | str], ...] = ((2, 0),)
    else:
        prerelease_key = tuple(
            (0, identifier) if isinstance(identifier, int) else (1, identifier)
            for identifier in parsed.prerelease
        )
    # The final marker makes a release version sort after all prereleases.
    return (parsed.major, parsed.minor, parsed.patch, prerelease_key, 0)


type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]

StrictString = Annotated[str, StringConstraints(min_length=1)]
SnapshotId = Annotated[str, StringConstraints(pattern=SNAPSHOT_ID.pattern)]


def digest_value(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} must be lowercase hexadecimal")
    return value


def url_parts(
    value: str,
    *,
    absolute: bool = False,
    host: str | None = None,
) -> tuple[str, Any]:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL must not contain userinfo")
    if parsed.fragment:
        raise ValueError("URL must not contain a fragment")
    if absolute and (parsed.scheme != "https" or not parsed.netloc):
        raise ValueError("URL must be an absolute HTTPS URL")
    if parsed.scheme and parsed.scheme != "https":
        raise ValueError("URL must use HTTPS")
    if parsed.netloc and host is not None and parsed.hostname != host:
        raise ValueError(f"URL must use {host}")
    if parsed.netloc:
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("URL has an invalid port") from exc
        if port not in (None, 443):
            raise ValueError("URL must use the default HTTPS port")
    return value, parsed


def validate_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    offset = value.utcoffset()
    assert offset is not None
    if offset.total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return value


def validate_installref_url(value: str) -> None:
    """Validate legacy and verified Gelstable installref URL origins."""

    _, parsed = url_parts(value)
    if parsed.netloc and parsed.hostname not in {
        "packages.geldata.com",
        "github.com",
    }:
        raise ValueError("installref URL must use packages.geldata.com or github.com")
