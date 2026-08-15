"""Strict, immutable models for registry source and rendered documents."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .constants import (
    CAPTURE_ID,
    CHANNELS,
    CLI_PLATFORMS,
    LEGACY_PLATFORMS,
    ORIGIN,
    PRODUCT_REPOSITORIES,
    capture_urls,
)

_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    populate_by_name=True,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")
_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{16}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]

StrictString = Annotated[str, StringConstraints(min_length=1)]
SnapshotId = Annotated[str, StringConstraints(pattern=_SNAPSHOT_ID.pattern)]


def _digest(value: str, pattern: re.Pattern[str], name: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} must be lowercase hexadecimal")
    return value


def _url_parts(
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


def _validate_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    offset = value.utcoffset()
    assert offset is not None
    if offset.total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return value


class Verification(BaseModel):
    """Legacy or release artifact byte verification metadata."""

    model_config = _MODEL_CONFIG

    size: int = Field(ge=0)
    sha256: str | None = None
    blake2b: str

    @field_validator("size", mode="before")
    @classmethod
    def reject_bool_size(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("size must be an integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _digest(value, _HEX64, "sha256")

    @field_validator("blake2b")
    @classmethod
    def validate_blake2b(cls, value: str) -> str:
        return _digest(value, _HEX128, "blake2b")


class InstallRef(BaseModel):
    """An upstream package install reference."""

    model_config = _MODEL_CONFIG

    ref: StrictString
    type: StrictString
    encoding: str | None = None
    verification: Verification

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str) -> str:
        _url_parts(value, host="packages.geldata.com")
        return value


class PackageEntry(BaseModel):
    """One package and all install references represented by an index."""

    model_config = _MODEL_CONFIG

    basename: StrictString
    name: StrictString
    version: StrictString
    version_details: dict[str, JsonValue]
    version_key: StrictString
    revision: StrictString
    build_date: StrictString
    architecture: StrictString
    slot: str
    tags: dict[str, JsonValue] = Field(default_factory=dict)
    installref: StrictString
    installrefs: tuple[InstallRef, ...]

    @field_validator("installref")
    @classmethod
    def validate_installref(cls, value: str) -> str:
        _url_parts(value, host="packages.geldata.com")
        return value


class PackageIndex(BaseModel):
    """The client-compatible legacy package-index document."""

    model_config = _MODEL_CONFIG

    packages: tuple[PackageEntry, ...]

    @model_validator(mode="after")
    def reject_duplicate_identities(self) -> PackageIndex:
        identities = [
            (item.basename, item.version, item.slot) for item in self.packages
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("package identity (basename, version, slot) is duplicated")
        return self


class CaptureEntry(BaseModel):
    """One attempted URL in the fixed legacy retrieval matrix."""

    model_config = _MODEL_CONFIG

    channel: StrictString
    platform: StrictString
    url: StrictString
    status: Literal[200, 404]
    final_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    size: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    blake2b: str | None = None
    path: str | None = None

    @field_validator("size", mode="before")
    @classmethod
    def reject_bool_capture_size(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("size must be an integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_capture_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _digest(value, _HEX64, "sha256")

    @field_validator("blake2b")
    @classmethod
    def validate_capture_blake2b(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _digest(value, _HEX128, "blake2b")

    @model_validator(mode="after")
    def validate_matrix_entry(self) -> CaptureEntry:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        if self.platform not in LEGACY_PLATFORMS:
            raise ValueError(f"unknown platform {self.platform!r}")
        expected = next(
            url
            for channel, platform, url in capture_urls()
            if channel == self.channel and platform == self.platform
        )
        if self.url != expected:
            raise ValueError("capture URL does not match the fixed matrix")
        _url_parts(self.url, absolute=True)

        body_fields = (
            self.final_url,
            self.size,
            self.sha256,
            self.blake2b,
            self.path,
        )
        if self.status == 200:
            if any(value is None for value in body_fields):
                raise ValueError(
                    "HTTP 200 capture entries require byte metadata and path"
                )
            assert self.final_url is not None
            _url_parts(self.final_url, absolute=True, host="packages.geldata.com")
            if self.path != f"indexes/{self.channel}-{self.platform}.json":
                raise ValueError("capture body path does not match the matrix entry")
        elif any(value is not None for value in body_fields):
            raise ValueError("HTTP 404 capture entries must not carry byte metadata")
        return self


class CaptureManifest(BaseModel):
    """Immutable retrieval evidence for all 24 legacy matrix URLs."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    capture: str
    origin: str
    captured_at: datetime
    entries: tuple[CaptureEntry, ...]

    @field_validator("captured_at")
    @classmethod
    def validate_capture_time(cls, value: datetime) -> datetime:
        return _validate_utc(value)

    @field_validator("entries")
    @classmethod
    def sort_entries(cls, value: tuple[CaptureEntry, ...]) -> tuple[CaptureEntry, ...]:
        expected = [(channel, platform) for channel, platform, _ in capture_urls()]
        return tuple(
            sorted(
                value,
                key=lambda entry: expected.index((entry.channel, entry.platform)),
            )
        )

    @model_validator(mode="after")
    def validate_matrix(self) -> CaptureManifest:
        if self.capture != CAPTURE_ID:
            raise ValueError(f"capture must be {CAPTURE_ID!r}")
        if self.origin != ORIGIN:
            raise ValueError(f"origin must be {ORIGIN!r}")
        expected = [(channel, platform) for channel, platform, _ in capture_urls()]
        actual = [(entry.channel, entry.platform) for entry in self.entries]
        if len(actual) != len(set(actual)):
            raise ValueError("capture matrix contains duplicate entries")
        if set(actual) != set(expected):
            raise ValueError("capture manifest must cover the exact 24-entry matrix")
        return self


class Artifact(BaseModel):
    """One independently verified Gelstable release artifact."""

    model_config = _MODEL_CONFIG

    platform: StrictString
    encoding: Literal["identity", "zstd"]
    media_type: StrictString
    url: StrictString
    size: int = Field(ge=0)
    sha256: str
    blake2b: str

    @field_validator("size", mode="before")
    @classmethod
    def reject_bool_artifact_size(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("size must be an integer")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        return _digest(value, _HEX64, "sha256")

    @field_validator("blake2b")
    @classmethod
    def validate_artifact_blake2b(cls, value: str) -> str:
        return _digest(value, _HEX128, "blake2b")

    @model_validator(mode="after")
    def validate_artifact_contract(self) -> Artifact:
        if self.platform not in CLI_PLATFORMS:
            raise ValueError(f"unsupported CLI platform {self.platform!r}")
        _url_parts(self.url, absolute=True, host="github.com")
        parsed = urlsplit(self.url)
        if parsed.hostname != "github.com":
            raise ValueError("release artifact URL must use github.com")
        if parsed.query:
            raise ValueError("release artifact URL must not contain a query")
        expected_media_type = _media_type_for_platform(self.platform)
        if self.media_type != expected_media_type:
            raise ValueError(
                f"{self.platform} artifacts must use {expected_media_type!r}"
            )
        return self


class ReleaseSource(BaseModel):
    """GitHub release provenance recorded with a promoted version."""

    model_config = _MODEL_CONFIG

    repository: StrictString
    release_tag: StrictString
    release_id: int = Field(gt=0)

    @field_validator("release_id", mode="before")
    @classmethod
    def reject_bool_release_id(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("release_id must be an integer")
        return value


class ReleaseRecord(BaseModel):
    """Append-only, complete stable Gel CLI release record."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    product: Literal["gel-cli"]
    channel: Literal["stable"]
    version: StrictString
    source: ReleaseSource
    promoted_at: datetime
    artifacts: tuple[Artifact, ...]

    @field_validator("artifacts")
    @classmethod
    def sort_artifacts(cls, value: tuple[Artifact, ...]) -> tuple[Artifact, ...]:
        order = {platform: index for index, platform in enumerate(CLI_PLATFORMS)}
        return tuple(
            sorted(
                value,
                key=lambda artifact: (
                    order[artifact.platform],
                    0 if artifact.encoding == "identity" else 1,
                ),
            )
        )

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if value.startswith("v"):
            raise ValueError("release version must not have a leading v")
        match = _SEMVER.fullmatch(value)
        if match is None:
            raise ValueError("release version must be a SemVer")
        prerelease = match.group(4)
        if prerelease is not None:
            for identifier in prerelease.split("."):
                if (
                    identifier.isdigit()
                    and len(identifier) > 1
                    and identifier[0] == "0"
                ):
                    raise ValueError(
                        "numeric prerelease identifiers must not have leading zeroes"
                    )
        return value

    @field_validator("promoted_at")
    @classmethod
    def validate_promoted_time(cls, value: datetime) -> datetime:
        return _validate_utc(value)

    @model_validator(mode="after")
    def validate_release_contract(self) -> ReleaseRecord:
        expected_repository = PRODUCT_REPOSITORIES[self.product]
        if self.source.repository != expected_repository:
            raise ValueError("release source repository is not allowlisted")
        if self.source.release_tag != f"v{self.version}":
            raise ValueError("release tag must exactly be v<version>")
        expected = {
            (platform, encoding)
            for platform in CLI_PLATFORMS
            for encoding in ("identity", "zstd")
        }
        actual = {(artifact.platform, artifact.encoding) for artifact in self.artifacts}
        if actual != expected or len(actual) != len(self.artifacts):
            raise ValueError("release must contain exactly ten CLI installrefs")
        return self


def _media_type_for_platform(platform: str) -> str:
    if platform.endswith("-apple-darwin"):
        return "application/x-mach-binary"
    if platform.endswith("-windows-msvc"):
        return "application/x-dosexec"
    return "application/x-pie-executable"


class RootIndex(BaseModel):
    """A channel/platform index reference in a root manifest."""

    model_config = _MODEL_CONFIG

    channel: StrictString
    platform: StrictString
    url: StrictString

    @model_validator(mode="after")
    def validate_root_index(self) -> RootIndex:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        if self.platform not in LEGACY_PLATFORMS:
            raise ValueError(f"unknown platform {self.platform!r}")
        parsed = urlsplit(self.url)
        if (
            parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("root index URL must not contain fragment or userinfo")
        if parsed.scheme or parsed.netloc:
            raise ValueError("root index URL must be relative")
        return self

    @property
    def ref(self) -> str:
        """Compatibility alias for callers that call index references ``ref``."""

        return self.url


class RootManifest(BaseModel):
    """A root document listing exactly the indexes in one snapshot."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1] = 1
    indexes: tuple[RootIndex, ...]

    @field_validator("indexes")
    @classmethod
    def sort_indexes(cls, value: tuple[RootIndex, ...]) -> tuple[RootIndex, ...]:
        order = {
            (channel, platform): index
            for index, (channel, platform) in enumerate(
                (channel, platform)
                for channel in CHANNELS
                for platform in LEGACY_PLATFORMS
            )
        }
        return tuple(
            sorted(value, key=lambda item: order[(item.channel, item.platform)])
        )

    @model_validator(mode="after")
    def validate_unique_indexes(self) -> RootManifest:
        identities = [(item.channel, item.platform) for item in self.indexes]
        if len(identities) != len(set(identities)):
            raise ValueError("root manifest contains duplicate indexes")
        return self


class Pointer(BaseModel):
    """The sole mutable source pointer selecting a published snapshot."""

    model_config = _MODEL_CONFIG

    snapshot: SnapshotId


class SnapshotListing(BaseModel):
    """Published listing of every immutable snapshot and the selected one."""

    model_config = _MODEL_CONFIG

    latest: SnapshotId
    snapshots: tuple[SnapshotId, ...]

    @field_validator("snapshots")
    @classmethod
    def sort_snapshots(cls, value: tuple[SnapshotId, ...]) -> tuple[SnapshotId, ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_snapshot_listing(self) -> SnapshotListing:
        if len(self.snapshots) != len(set(self.snapshots)):
            raise ValueError("snapshot listing contains duplicates")
        if self.latest not in self.snapshots:
            raise ValueError("latest snapshot must be present in the listing")
        return self


# The source and renderer use both names in prose; retain the explicit alias
# without defining a second model or changing the serialized contract.
ManifestIndex = RootIndex

__all__ = [
    "Artifact",
    "CaptureEntry",
    "CaptureManifest",
    "InstallRef",
    "JsonValue",
    "ManifestIndex",
    "PackageEntry",
    "PackageIndex",
    "Pointer",
    "ReleaseRecord",
    "ReleaseSource",
    "RootIndex",
    "RootManifest",
    "SnapshotListing",
    "Verification",
]
