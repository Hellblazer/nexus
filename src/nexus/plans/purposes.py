# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Purpose registry resolver — RDR-078 P3 (nexus-05i.5).

A "purpose" is a human-named alias for a list of catalog link types.
Plan templates declare ``purpose: <name>`` instead of an explicit
``link_types: [...]`` so the same template stays correct as new link
types ship in the catalog.

The resolver:

  * Loads :data:`PURPOSES_YML` once on first call and caches it.
  * Filters resolved link types against :func:`_known_link_types`
    (currently the static set declared in ``catalog/tumbler.py:166``).
    Unknown types are dropped with a structured warning
    ``purpose_unknown_link_type`` so the traversal continues with
    whatever survives.

Test injection points (``_registry_override`` /
``_known_link_types_override``) are kwargs only — production callers
omit them.

SC-17.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

__all__ = ["PURPOSES_YML", "resolve_purpose"]

_log = logging.getLogger(__name__)

#: Path to the shipped purposes registry. Resolved once at import time.
PURPOSES_YML: Path = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "nx" / "plans" / "purposes.yml"
)

#: Known catalog link types. Source: ``catalog/tumbler.py:166`` —
#: ``cites, supersedes, quotes, relates, comments, implements,
#: implements-heuristic``. Hard-coded here to avoid importing the
#: catalog (which would create a load-time dependency cycle), and
#: changes infrequently. Kept in sync via
#: ``test_purpose_resolve_hits_known_catalog_link_types``.
_KNOWN_LINK_TYPES: frozenset[str] = frozenset({
    "cites", "supersedes", "quotes", "relates", "comments",
    "implements", "implements-heuristic",
})

_registry_cache: dict[str, dict[str, Any]] | None = None
_registry_lock = threading.Lock()


def _load_registry() -> dict[str, dict[str, Any]]:
    """Lazy-load + cache the purposes registry from disk."""
    global _registry_cache
    if _registry_cache is None:
        with _registry_lock:
            if _registry_cache is None:
                if not PURPOSES_YML.exists():
                    _registry_cache = {}
                else:
                    raw = yaml.safe_load(PURPOSES_YML.read_text()) or {}
                    _registry_cache = (
                        raw if isinstance(raw, dict) else {}
                    )
    return _registry_cache


def resolve_purpose(
    name: str,
    *,
    _registry_override: dict[str, dict[str, Any]] | None = None,
    _known_link_types_override: set[str] | None = None,
) -> list[str]:
    """Return the catalog link types associated with purpose *name*.

    Unknown purpose → empty list. Unknown link types within a
    known purpose → dropped with a structured warning, valid
    subset returned.
    """
    registry = (
        _registry_override
        if _registry_override is not None
        else _load_registry()
    )
    entry = registry.get(name)
    if not entry:
        return []
    raw_link_types = entry.get("link_types") or []
    if not isinstance(raw_link_types, list):
        return []

    known = (
        _known_link_types_override
        if _known_link_types_override is not None
        else _KNOWN_LINK_TYPES
    )

    out: list[str] = []
    for lt in raw_link_types:
        if not isinstance(lt, str):
            continue
        if lt in known:
            out.append(lt)
        else:
            _log.warning(
                "purpose_unknown_link_type: purpose=%r dropped link_type=%r "
                "(not in catalog known set; valid subset returned)",
                name, lt,
            )
    return out
