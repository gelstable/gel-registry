"""Fixed source and release contracts for the registry."""

from __future__ import annotations

from typing import Final

CAPTURE_ID: Final = "legacy-2026-08-bootstrap"
ORIGIN: Final = "https://packages.geldata.com"

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

PRODUCT_REPOSITORIES: Final = {
    "gel-cli": "gelstable/gel-cli",
    "gel-server": "gelstable/gel",
    "gel-ls": "gelstable/gel",
}

type Channel = str
type LegacyPlatform = str
type CliPlatform = str
type CaptureUrl = tuple[str, str, str]


def capture_urls() -> tuple[CaptureUrl, ...]:
    """Return the reviewed, ordered legacy capture matrix."""

    return tuple(
        (
            channel,
            platform,
            f"{ORIGIN}/archive/.jsonindexes/{platform}{CHANNEL_SUFFIX[channel]}.json",
        )
        for channel in CHANNELS
        for platform in LEGACY_PLATFORMS
    )
