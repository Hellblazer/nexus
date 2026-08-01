# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tombstone (nexus-i711w terminal deletion): the nexus-8y1tm two-sided
shape-parity harness — ``shape()``, the 70+-entry ``Parity`` REGISTRY, the
EXCLUSIONS ledger, and the mechanized completeness gate — retired with the
local SQLite ``Catalog``. The harness asserted ``shape(local) == shape(http)``
per shared public method; with the local arm deleted there is no second side
to compare, and the service catalog's caller-facing surface is pinned by
``test_catalog_conformance.py`` / ``test_catalog_protocol_fidelity.py``.

What survives is the one HTTP-side-only pin: ``_to_entry`` must populate
every ``CatalogEntry`` field from the wire dict (the h8rf6.3 class one layer
down — a silently defaulted new field is invisible to consumer tests).
"""
from __future__ import annotations

import inspect


def test_to_entry_covers_every_catalog_entry_field() -> None:
    """Review suggestion (2026-07-04): shape() collapses dataclasses to
    their class name, and _to_entry's ``d.get(field) or default`` pattern
    means a NEW CatalogEntry field with a default that _to_entry forgets
    stays silently defaulted on the HTTP side — the h8rf6.3 class one
    layer down, invisible to the parity harness. Pin _to_entry's source
    against the CatalogEntry field set by reflection."""
    import dataclasses as _dc

    from nexus.catalog import http_catalog_client as _hcc
    from nexus.catalog.types import CatalogEntry

    src = inspect.getsource(_hcc._to_entry)
    missing = [
        f.name for f in _dc.fields(CatalogEntry)
        if f"{f.name}=" not in src
    ]
    assert not missing, (
        f"CatalogEntry fields not populated by HttpCatalogClient._to_entry: "
        f"{missing} — every field must be mapped from the wire dict (or the "
        f"omission documented here)"
    )
