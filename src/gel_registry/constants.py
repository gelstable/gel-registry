"""Fixed source and release contracts for the registry."""

from __future__ import annotations

from typing import Final

CAPTURE_ID: Final = "legacy-2026-08-bootstrap"
ORIGIN: Final = "https://packages.geldata.com"
RESCUE_ORIGIN: Final = "https://packages.edgedb.com"
PACKAGE_ORIGINS: Final = frozenset({ORIGIN, RESCUE_ORIGIN})

#: Index cells that ``packages.edgedb.com`` never published, verified by a
#: direct sweep of the whole matrix on 2026-08-31: every other cell answers
#: 200, and this one answers a genuine S3 404. Only the rescue capture
#: tolerates these -- and only these -- so any other missing index still
#: fails the whole capture. No selection policy reads the testing channel.
RESCUE_KNOWN_ABSENT_INDEXES: Final = frozenset({("testing", "aarch64-pc-windows-msvc")})

CHANNELS: Final = ("stable", "testing", "nightly")
CHANNEL_SUFFIX: Final = {
    "stable": "",
    "testing": ".testing",
    "nightly": ".nightly",
}

LEGACY_PLATFORMS: Final = (
    "x86_64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-gnu",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)

CLI_PLATFORMS: Final = (
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)

# Compatibility mapping for callers that have not migrated to SourcePolicy yet.
# It is intentionally retained until the later migration cleanup.
PRODUCT_REPOSITORIES: Final = {
    "gel-cli": "gelstable/gel-cli",
    "gel-server": "gelstable/gel",
    "gel-ls": "gelstable/gel",
}

type Channel = str
type LegacyPlatform = str
type CliPlatform = str
type CaptureUrl = tuple[str, str, str]


def validate_capture_origin(origin: str) -> str:
    """Reject every origin other than the two reviewed package hosts."""

    if origin not in PACKAGE_ORIGINS:
        raise ValueError("capture origin must be an allowlisted package origin")
    return origin


def capture_urls(*, origin: str = ORIGIN) -> tuple[CaptureUrl, ...]:
    """Return the reviewed, ordered legacy capture matrix."""

    origin = validate_capture_origin(origin)
    return tuple(
        (
            channel,
            platform,
            f"{origin}/archive/.jsonindexes/{platform}{CHANNEL_SUFFIX[channel]}.json",
        )
        for channel in CHANNELS
        for platform in LEGACY_PLATFORMS
    )
