# SPDX-License-Identifier: AGPL-3.0-or-later
"""The default taxonomy projection threshold of a collection (RDR-229 P2.4,
nexus-p2m1x.9).

RDR-229 Technical Design item 1 gives a collection's attributes three
outcomes, and this reads them in order:

* **Row**: the catalog row's ``content_type`` picks the threshold, even when
  the name's prefix says otherwise (a ``code__``-named collection whose row
  says ``knowledge`` gets 0.50).
* **NoRow**: the collection has no catalog row (unregistered, or registered
  but holding no chunk). The legacy name is then the only signal, and
  :func:`nexus.corpus.default_projection_threshold` derives it from the name.
* **Unreadable**: the row listing itself raised. A taxonomy run never fails on
  that, so it answers as NoRow does and logs once per process (a backfill asks
  once per collection; a line per ask would bury the run).

An explicit ``--threshold`` never reaches this module: callers pass it through
unchanged and call here only when it is absent.

The table (RDR-077 Phase 4a; calibration in
``docs/exploration/taxonomy-projection-tuning.md``): knowledge 0.50, docs and
rdr 0.55, code and anything else 0.70. Unknown falls to 0.70, the safer
under-match bias.
"""
from __future__ import annotations

import structlog

_log = structlog.get_logger(__name__)

_THRESHOLD_BY_CONTENT_TYPE: dict[str, float] = {
    "knowledge": 0.50,
    "docs": 0.55,
    "rdr": 0.55,
}
_FALLBACK_THRESHOLD = 0.70

#: Set after the first unreadable-listing warning; tests reset it.
_unreadable_logged = False


def threshold_for_content_type(content_type: str) -> float:
    """The projection threshold of a catalog ``content_type``."""
    return _THRESHOLD_BY_CONTENT_TYPE.get(content_type, _FALLBACK_THRESHOLD)


def projection_threshold(collection_name: str) -> float:
    """The default projection threshold for *collection_name*: its catalog
    row's ``content_type`` when a row backs it, else the name derivation.

    Tenant-blind like :func:`nexus.corpus.collection_content_type`: the row
    comes from ``nexus.mcp_infra``'s process-wide cache, the ambient tenant's.
    """
    global _unreadable_logged  # noqa: PLW0603 — once-per-process log latch
    from nexus.corpus import default_projection_threshold  # noqa: PLC0415 — circular-dep avoidance (corpus)
    from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)

    try:
        row = get_collection_row(collection_name, strict=True)
    except Exception:  # noqa: BLE001 — Unreadable must never fail a taxonomy run (RDR-229 item 1); it answers as NoRow and is logged once
        if not _unreadable_logged:
            _unreadable_logged = True
            _log.warning(
                "projection_threshold_row_unreadable",
                collection=collection_name, exc_info=True,
            )
        row = None
    if row is not None:
        return threshold_for_content_type(row["content_type"])
    return default_projection_threshold(collection_name)
