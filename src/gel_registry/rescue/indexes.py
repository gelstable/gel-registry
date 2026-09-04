"""Atomic capture of the legacy package-index matrix from packages.edgedb.com."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import httpx
from pydantic import ValidationError, field_validator, model_validator

from .. import storage
from ..capture import CaptureError, _fetch
from ..constants import (
    CAPTURE_ID,
    RESCUE_KNOWN_ABSENT_INDEXES,
    RESCUE_ORIGIN,
    capture_urls,
)
from ..contracts import PackageIndex
from ..contracts.common import validate_utc
from ..digest import canonical_json
from .models import RescueAbsentIndex, RescueIndexCapture, _CanonicalModel

_RESCUE_HOST = "packages.edgedb.com"


class RescueCaptureManifest(_CanonicalModel):
    """Canonical record of every exact legacy-index response."""

    schema_version: Literal[1] = 1
    capture: str
    captured_at: datetime
    indexes: tuple[RescueIndexCapture, ...]
    absent: tuple[RescueAbsentIndex, ...] = ()

    @field_validator("captured_at")
    @classmethod
    def validate_capture_time(cls, value: datetime) -> datetime:
        return validate_utc(value)

    @model_validator(mode="after")
    def validate_matrix(self) -> Self:
        if self.capture != CAPTURE_ID:
            raise ValueError(f"rescue capture must be {CAPTURE_ID!r}")
        expected = capture_urls(origin=RESCUE_ORIGIN)
        retrieved = tuple(
            (entry.channel, entry.platform, entry.source_url) for entry in self.indexes
        )
        missing = tuple(
            (entry.channel, entry.platform, entry.source_url) for entry in self.absent
        )
        covered = sorted(retrieved + missing, key=expected.index)
        if tuple(covered) != expected:
            raise ValueError("rescue capture must cover the exact ordered index matrix")
        if retrieved != tuple(
            cell for cell in expected if cell not in frozenset(missing)
        ):
            raise ValueError("retrieved rescue indexes must keep the matrix order")
        if missing != tuple(cell for cell in expected if cell in frozenset(missing)):
            raise ValueError("absent rescue indexes must keep the matrix order")
        unexpected = tuple(
            f"{channel}/{platform}"
            for channel, platform, _url in missing
            if (channel, platform) not in RESCUE_KNOWN_ABSENT_INDEXES
        )
        if unexpected:
            raise ValueError(
                "rescue capture recorded an absence that is not known upstream: "
                + ", ".join(unexpected)
            )
        stamps = tuple(entry.captured_at for entry in self.indexes) + tuple(
            entry.captured_at for entry in self.absent
        )
        if any(stamp != self.captured_at for stamp in stamps):
            raise ValueError("rescue index capture timestamps must match the manifest")
        return self

    @classmethod
    def create(
        cls,
        captured_at: datetime,
        indexes: tuple[RescueIndexCapture, ...],
        absent: tuple[RescueAbsentIndex, ...] = (),
    ) -> RescueCaptureManifest:
        return cls(
            capture=CAPTURE_ID,
            captured_at=validate_utc(captured_at),
            indexes=indexes,
            absent=absent,
        )


def rescue_capture_root(repo: Path) -> Path:
    """Return the immutable destination for the fixed rescue capture ID."""

    return repo / "upstream" / _RESCUE_HOST / CAPTURE_ID


def capture_rescue_indexes(
    client: httpx.Client, repo: Path, captured_at: datetime
) -> RescueCaptureManifest:
    """Fetch and validate every legacy index before atomically publishing it."""

    destination = rescue_capture_root(repo)
    try:
        if b"\x00" in os.fsencode(str(destination)):
            raise ValueError("capture path contains an embedded NUL byte")
    except ValueError as exc:
        raise CaptureError(str(exc)) from exc
    if destination.exists() or destination.is_symlink():
        raise CaptureError(f"capture destination already exists: {destination}")

    try:
        storage.ensure_directory_chain(destination.parent)
    except (OSError, ValueError) as exc:
        raise CaptureError(f"capture destination is not usable: {exc}") from exc
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    )
    committed = False
    try:
        (temporary_root / "indexes").mkdir()
        indexes: list[RescueIndexCapture] = []
        absent: list[RescueAbsentIndex] = []
        for channel, platform, url in capture_urls(origin=RESCUE_ORIGIN):
            relative_path = f"indexes/{channel}-{platform}.json"
            path = temporary_root / relative_path
            observed = _fetch(
                client,
                url,
                output_path=path,
                origin_url=RESCUE_ORIGIN,
                reject_redirects=True,
            )
            if observed.status != 200 or observed.digests is None:
                if (
                    observed.status == 404
                    and (channel, platform) in RESCUE_KNOWN_ABSENT_INDEXES
                ):
                    # Upstream never published this cell. Record that we
                    # looked and what we saw; keep no file, since there are
                    # no bytes to preserve.
                    path.unlink(missing_ok=True)
                    absent.append(
                        RescueAbsentIndex(
                            source_url=url,
                            channel=channel,
                            platform=platform,
                            captured_at=captured_at,
                            observed_status=observed.status,
                        )
                    )
                    continue
                raise CaptureError(
                    f"{channel}/{platform}: expected status 200, "
                    f"observed {observed.status}"
                )
            try:
                PackageIndex.model_validate_json(path.read_bytes())
            except (ValidationError, ValueError, OSError) as exc:
                raise CaptureError(
                    f"{channel}/{platform}: successful body is not a package index: "
                    f"{exc}"
                ) from exc
            indexes.append(
                RescueIndexCapture(
                    source_url=url,
                    channel=channel,
                    platform=platform,
                    captured_at=captured_at,
                    byte_size=observed.digests.size,
                    sha256=observed.digests.sha256,
                )
            )

        manifest = RescueCaptureManifest.create(
            captured_at, tuple(indexes), tuple(absent)
        )
        (temporary_root / "capture.json").write_bytes(canonical_json(manifest))
        if destination.exists() or destination.is_symlink():
            raise CaptureError(f"capture destination already exists: {destination}")
        storage.rename_noreplace(temporary_root, destination)
        committed = True
        return manifest
    except CaptureError:
        raise
    except (OSError, ValidationError, ValueError) as exc:
        raise CaptureError(f"capture failed: {exc}") from exc
    finally:
        if not committed:
            shutil.rmtree(temporary_root, ignore_errors=True)


__all__ = [
    "RESCUE_ORIGIN",
    "RescueCaptureManifest",
    "capture_rescue_indexes",
    "rescue_capture_root",
]
