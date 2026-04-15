# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan schema helpers for RDR-078.

``canonical_dimensions_json`` is the load-bearing primitive: it produces
a stable string representation of a dimensional identity map used as the
dedup key by the partial ``UNIQUE (project, dimensions)`` index on the
``plans`` table. Any caller that persists a plan MUST route its
dimension map through this function so byte-identical identities
collapse to byte-identical keys.

Rules:
  * Keys are sorted and lowercased.
  * String values are lowercased; non-string values (int/bool) are
    preserved as-is so dimensions like ``depth: 3`` stay typed.
  * JSON output has no whitespace (``separators=(",", ":")``).

SC-18.
"""
from __future__ import annotations

import json
from typing import Any


def canonical_dimensions_json(dimensions: dict[str, Any]) -> str:
    """Serialise a dimensional identity map to canonical JSON.

    ``{"verb":"r","scope":"g"}`` and ``{"scope":"g","verb":"r"}`` both
    produce ``'{"scope":"g","verb":"r"}'`` — same bytes, same dedup key.
    """
    normalised: dict[str, Any] = {}
    for key, value in dimensions.items():
        norm_key = key.lower()
        norm_value = value.lower() if isinstance(value, str) else value
        normalised[norm_key] = norm_value
    return json.dumps(normalised, sort_keys=True, separators=(",", ":"))
