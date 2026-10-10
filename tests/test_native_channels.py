"""Version channel derivation and lock validation."""

import pytest
from pydantic import ValidationError

from gel_registry.native.models import LockEntry


@pytest.mark.parametrize(
    "version,channel",
    [("1:7.2~rc.1-1", "testing"), ("1:7.2-1", "stable"), ("1:7.2-1~foo", "stable")],
)
def test_lock_rejects_channel_inconsistent_with_upstream_version(
    version: str, channel: str
) -> None:
    data = dict(
        format="deb",
        name="gel-7",
        version=version,
        arch="amd64",
        sha256="a" * 64,
        size=1,
        path="pool/gel/tag/gel.deb",
        channel=channel,
    )
    assert LockEntry.model_validate(data).channel == channel
    data["channel"] = "testing" if channel == "stable" else "stable"
    with pytest.raises(ValidationError, match="channel"):
        LockEntry.model_validate(data)
