"""Composition of registry inputs into immutable, selectable snapshots.

Rendering deliberately has two phases.  :func:`build_snapshot` only consumes
the append-only bootstrap and release inputs and installs (or verifies) a
content-addressed tree.  :func:`select_snapshot` only consumes the internal
pointer and materializes the two moving public documents from an existing
pinned tree.
"""

from __future__ import annotations

from .errors import ContestedIdentityError, RenderError
from .schemas import render_schemas
from .selection import select_snapshot
from .snapshots import build_snapshot, load_pinned_snapshot

__all__ = [
    "ContestedIdentityError",
    "RenderError",
    "build_snapshot",
    "load_pinned_snapshot",
    "render_schemas",
    "select_snapshot",
]
