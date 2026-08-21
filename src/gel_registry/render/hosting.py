"""Rendering of the hosting configuration from the selected snapshot.

Vercel reads ``vercel.json`` from the repository root, so the one policy that
has to name a snapshot -- the legacy ``GEL_PKG_ROOT`` compatibility rewrites --
cannot live under ``public/`` with the rest of the published tree.  The file is
therefore rendered in full from the selected snapshot rather than hand
maintained, which keeps the rewrite targets and ``public/registry.json``
pointing at the same pinned tree.

Nothing here makes the deployment dynamic.  A rewrite is resolved by the CDN
against the same static tree the request would otherwise have addressed
directly; no code runs in production.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..contracts import RootManifest
from .errors import RenderError

MOVING_CACHE_CONTROL = (
    "public, max-age=0, s-maxage=60, stale-while-revalidate=600, stale-if-error=86400"
)
PINNED_CACHE_CONTROL = "public, max-age=31536000, s-maxage=31536000, immutable"

_CACHE_POLICY = (
    ("/i/(.*)", PINNED_CACHE_CONTROL),
    ("/s/(.*)", PINNED_CACHE_CONTROL),
    ("/registry.json", MOVING_CACHE_CONTROL),
    ("/v1/(.*)", MOVING_CACHE_CONTROL),
)

#: Legacy index paths carry the channel as a filename suffix: ``stable`` has
#: none, the other channels append their name before ``.json``.
_LEGACY_CHANNEL_SUFFIXES = {
    "stable": "",
    "nightly": ".nightly",
    "testing": ".testing",
}

_LEGACY_INDEX_ROOT = "/archive/.jsonindexes"

#: Accepts either manifest's URL form -- the moving root's ``i/<id>.json`` and
#: the pinned root's ``../../i/<id>.json`` -- and yields the blob ID.
_BLOB_URL = re.compile(r"^(?:\.\./\.\./)?i/([0-9a-f]{32})\.json$")

#: Vercel caps a deployment's routes.  The rewrite count now scales with the
#: published platform matrix (23 entries today), so a matrix that outgrew the
#: cap would fail at deploy time rather than in review.  This ceiling is far
#: below Vercel's own limit and exists to make that failure local and legible.
MAX_ROUTES = 256


def legacy_rewrites(manifest: RootManifest) -> tuple[dict[str, str], ...]:
    """Map the legacy ``GEL_PKG_ROOT`` index layout onto the published blobs.

    One literal rule per published index, generated from the manifest rather
    than from ``CHANNELS x LEGACY_PLATFORMS``: not every combination has an
    index (``testing-aarch64-pc-windows-msvc`` does not), and iterating the
    constants would emit a rule pointing at a blob that does not exist.  An
    absent rule 404s, which is what the legacy layout did anyway.

    Literal sources also retire the old ``:platform([^/.]+)`` capture and its
    rule-ordering hazard: nothing here can read ``...darwin.nightly`` as a
    platform name, so manifest order is the only order that matters.

    Destinations are root-absolute because a Vercel rewrite destination is a
    server path.  That is deliberately unlike the document-relative URLs the
    manifests carry, and the two must not be unified.
    """

    rules: list[dict[str, str]] = []
    for reference in manifest.indexes:
        suffix = _LEGACY_CHANNEL_SUFFIXES.get(reference.channel)
        if suffix is None:
            raise RenderError(f"no legacy path for channel {reference.channel!r}")
        matched = _BLOB_URL.fullmatch(reference.url)
        if matched is None:
            raise RenderError(
                f"manifest index is not a blob reference: {reference.url}"
            )
        rules.append(
            {
                "source": f"{_LEGACY_INDEX_ROOT}/{reference.platform}{suffix}.json",
                "destination": f"/i/{matched.group(1)}.json",
            }
        )
    return tuple(rules)


def hosting_config(manifest: RootManifest) -> bytes:
    """Render the complete hosting configuration for one selected snapshot."""

    rewrites = legacy_rewrites(manifest)
    total = len(_CACHE_POLICY) + len(rewrites)
    if total > MAX_ROUTES:
        raise RenderError(
            f"hosting configuration declares {total} routes, "
            f"above the {MAX_ROUTES} ceiling"
        )
    config: dict[str, Any] = {
        "version": 2,
        "outputDirectory": "public",
        "headers": [
            {"source": source, "headers": [{"key": "Cache-Control", "value": value}]}
            for source, value in _CACHE_POLICY
        ],
        "rewrites": list(rewrites),
    }
    return (json.dumps(config, indent=2) + "\n").encode("utf-8")


__all__ = ["MAX_ROUTES", "hosting_config", "legacy_rewrites"]
