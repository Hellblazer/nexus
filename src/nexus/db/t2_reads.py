# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Narrow read helpers over T2, for consumers outside the facade boundary.

RDR-215 bead nexus-q02nx.21 ported ``conexus/hooks/scripts/rdr_hook.py``
into ``nexus.hooks.rdr_verb``. The plugin copy opened its own
``T2Database`` handle, which was invisible to the RDR-120 storage-boundary
lint because the lint scans ``src/`` and the plugin is not in it. Moving
the code into the wheel moved it into the lint's domain, and a direct
construction outside ``src/nexus/db/`` is a violation there.

Sam's ruling of 2026-09-19 was to respect the boundary rather than grow
:data:`nexus.storage_boundary_lint.T2DATABASE_CONSTRUCTION_ALLOWLIST`:
the read moves to the side of the line that is allowed to open a handle,
and the caller asks for rows.

Kept deliberately thin. These are reads a hook makes on a budget, not a
place to accumulate query logic — anything that needs more than a single
``get_all`` belongs behind the facade's own API, not here.
"""

from __future__ import annotations

from typing import Any

__all__ = ["rdr_rows"]


def rdr_rows(project: str) -> list[dict[str, Any]]:
    """Every T2 row in *project*, or an empty list if T2 is unreachable.

    One ``get_all`` per call. ``nexus.hooks.rdr_verb`` caches the result
    for the life of a hook run and shares it between the status and gate
    loaders — code review [24883] finding 5 measured two full fetches of a
    1000-row project inside a 10s SessionStart budget.

    Never raises. A hook must not fail because T2 is down, and an
    unreachable store degrades to an empty status map, which the callers
    already treat as "no recorded status" rather than as an error.
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred: only a real T2 lookup pays for this
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred: same reason

    try:
        with T2Database(default_db_path()) as db:
            return list(db.get_all(project=project))
    except Exception:  # noqa: BLE001 — the hook must never fail; see the docstring
        return []
