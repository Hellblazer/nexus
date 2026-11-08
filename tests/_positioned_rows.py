# SPDX-License-Identifier: AGPL-3.0-or-later
"""A test helper for the manifest row producer (RDR-236 Phase 4 Step 0, nexus-6pfgb.65)."""
from __future__ import annotations


def rows_as_positioned(pairs) -> "list[tuple[int, dict, dict]]":
    """``(index, row)`` pairs as the triples ``nexus.mcp_infra._manifest_chunk_rows`` takes, with the
    positions read off the row, as a caller whose chunk metadata is a stored row does.

    Tests only: no production caller reads positions off a row it just wrote.
    """
    from nexus.metadata_schema import positions_of_row  # noqa: PLC0415 — circular-dep avoidance (nexus.metadata_schema)

    return [(i, m, positions_of_row(m)) for i, m in pairs]
