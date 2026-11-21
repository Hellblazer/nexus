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
* **Unreadable**: the row read itself failed (``resolve_collection`` says so; a
  service error, or any other read failure but a ``TypeError``, ``AttributeError`` or
  ``KeyError``, which are bugs and raise). A taxonomy run never fails on that, so it answers
  as NoRow does; the failure is logged by :func:`nexus.catalog.rows.resolve.resolve_collection`.

This module owns the one table; :func:`nexus.corpus.default_projection_threshold`
(the NoRow name derivation) reads it through :func:`threshold_for_content_type`.

The table (RDR-077 Phase 4a; calibration in
``docs/exploration/taxonomy-projection-tuning.md``): knowledge 0.50, docs and
rdr 0.55, code and anything else 0.70. Unknown falls to 0.70, the safer
under-match bias.
"""
from __future__ import annotations

from typing import NamedTuple

_THRESHOLD_BY_CONTENT_TYPE: dict[str, float] = {
    "knowledge": 0.50,
    "docs": 0.55,
    "rdr": 0.55,
}
_FALLBACK_THRESHOLD = 0.70


class ProjectionThreshold(NamedTuple):
    """A threshold and where it came from: ``"row"``, ``"no_row"`` or ``"unreadable"``."""

    value: float
    source: str


def threshold_for_content_type(content_type: str) -> float:
    """The projection threshold of a catalog ``content_type``."""
    return _THRESHOLD_BY_CONTENT_TYPE.get(content_type, _FALLBACK_THRESHOLD)


def resolve_projection_threshold(collection_name: str) -> ProjectionThreshold:
    """The default projection threshold for *collection_name* and its source: the
    catalog row's ``content_type`` when a row backs it, else the name derivation
    (``no_row``, or ``unreadable`` when the row read failed).

    Tenant-blind like :func:`nexus.corpus.collection_content_type`: the row
    comes from ``nexus.mcp_infra``'s process-wide cache, the ambient tenant's.
    It reads no version or model from the row, so a row with a blank
    ``model_version`` is a Row here.
    """
    from nexus.catalog.rows.resolve import Row, Unreadable, resolve_collection  # noqa: PLC0415 — circular-dep avoidance (resolve imports corpus lazily; corpus imports this module)
    from nexus.corpus import default_projection_threshold  # noqa: PLC0415 — circular-dep avoidance (corpus)

    out = resolve_collection(collection_name, need_version=False, strict=True)
    if isinstance(out, Row):
        return ProjectionThreshold(threshold_for_content_type(out.attributes.content_type), "row")
    return ProjectionThreshold(
        default_projection_threshold(collection_name),
        "unreadable" if isinstance(out, Unreadable) else "no_row",
    )


def projection_threshold(collection_name: str) -> float:
    """The default projection threshold for *collection_name*: the value of
    :func:`resolve_projection_threshold`."""
    return resolve_projection_threshold(collection_name).value
