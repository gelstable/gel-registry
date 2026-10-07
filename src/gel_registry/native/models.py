"""Shared package identities, lock contracts and repository paths."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contracts.release import NativePackage, ReleaseRecord

SUPPORTED = {"deb": ("amd64", "arm64"), "rpm": ("x86_64", "aarch64")}
CHANNELS = ("stable", "testing")


class PackageIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format: Literal["deb", "rpm"]
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    arch: str

    @model_validator(mode="after")
    def supported_architecture(self) -> PackageIdentity:
        if self.arch not in SUPPORTED[self.format]:
            raise ValueError(f"unsupported native architecture: {self.arch}")
        return self


class LockEntry(PackageIdentity):
    channel: Literal["stable", "testing"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0)
    path: str = Field(pattern=r"^pool/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

    @model_validator(mode="after")
    def safe_components(self) -> LockEntry:
        if any(part in {".", ".."} for part in self.path.split("/")):
            raise ValueError("unsafe native pool path")
        return self


def pool_path(record: ReleaseRecord, package: NativePackage) -> str:
    asset = urlsplit(package.url).path.rsplit("/", 1)[1]
    return f"pool/{record.source.repository.split('/')[1]}/{record.source.tag}/{asset}"


def format_evr(epoch: str, version: str, release: str) -> str:
    """RPM EVR spelling; Debian versions are retained verbatim instead."""
    return (
        (f"{epoch}:" if epoch != "0" else "")
        + version
        + (f"-{release}" if release else "")
    )
