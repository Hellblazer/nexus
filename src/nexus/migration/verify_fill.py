# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-178 wave-2 (nexus-s3dd4): verify-fill (delta) migration mode.

P2 (nexus-s3dd4.2) — the outer count-diff loop. A ``migrate --verify-fill``
re-run should not re-send an entire store to patch a handful of missing
rows (the 2026-07-01 incident: a 270-row hole in a 138,327-row catalog
manifest patched by re-sending ~158k rows). The outer loop is the cheap
pre-check that makes that possible: diff each table's already-known
source (SQLite) row count against the target's count via the
ALREADY-DEPLOYED ``relation_counts`` REST surface
(:class:`~nexus.migration.orchestrator.CountSource`,
``HttpCatalogClient.relation_counts`` — see
``POST /v1/catalog/verify/relation-counts``). NO new engine endpoint.

A table diffed as count-parity is the caller's signal to SKIP the
(later-bead) inner identity-diff + fill loop entirely — a clean store
verifies in one batched HTTP call per store, never a row-by-row identity
fetch. A table diffed as divergent is the caller's signal to run the inner
loop. A table that cannot be diffed (unmapped, or the count source is
unreachable / omits the relation) is ``indeterminate`` — NEVER a silent
pass (nexus-r0esi): the caller must treat it the same as ``divergent`` for
safety (or surface it as an operator warning), never as ``parity``.

This module deliberately reuses — never duplicates — the
:class:`~nexus.migration.orchestrator.CountSource` Protocol, the
``(store, table) -> relation`` mapping (``_VERIFY_TABLES``), and the
plans-convergence dedup guard (``_VERIFY_TABLES_DEDUP``) from
``nexus.migration.orchestrator``. Those constants are the single source of
truth for what a "row" means per relation; a second copy here would drift.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import structlog

from nexus.migration.orchestrator import _VERIFY_TABLES, _VERIFY_TABLES_DEDUP

if TYPE_CHECKING:
    from nexus.migration.orchestrator import CountSource

_log = structlog.get_logger(__name__)

#: Per-table verdict status. Never a fourth value — the vocabulary is
#: strictly ``parity`` | ``divergent`` | ``indeterminate`` (an ``indeterminate``
#: table is never a silent pass; see module docstring / nexus-r0esi).
_STATUS = frozenset({"parity", "divergent", "indeterminate"})


class TableVerdict(TypedDict):
    """One table's outer count-diff result."""

    source_count: int
    #: ``None`` when the target count could not be obtained (unmapped
    #: table, unreachable count source, or the source omitted this
    #: relation from its response).
    target_count: int | None
    status: str  # one of _STATUS


def verify_store_counts(
    store: str,
    count_source: "CountSource",
    source_counts: dict[str, int],
) -> dict[str, TableVerdict]:
    """Diff *store*'s per-table source counts against the target.

    *source_counts* is ``{table: source_row_count}`` as already computed by
    the store's own ``count_source_rows`` (each ETL module owns its own
    per-table SQLite counting; this function only diffs, it never re-derives
    a source count). Returns ``{table: TableVerdict}`` for every table key
    present in *source_counts* — including tables this outer loop cannot map
    to a relation (``indeterminate``, ``target_count=None``), so the caller
    can iterate the full input without a second lookup.

    Batching: every table that DOES map to a relation is queried in exactly
    ONE ``count_source.counts(...)`` call (deduplicated, sorted for
    determinism) — a clean store's outer verify is one HTTP round trip per
    store, matching the design's "no-op verify = one HTTP call" goal.

    Status semantics:

    - **parity** — ``target_count >= source_count`` for a normal relation,
      or (for a dedup relation) ``source_count == 0`` (trivial pass) or
      ``0 < target_count <= source_count`` (exact match or convergence
      collapse — see the dedup branch below). The caller SKIPS the inner
      fill loop for a parity table.
    - **divergent** — the target under-counts a normal relation, or (for a
      dedup relation) ``target_count == 0`` with a non-zero write
      (nothing landed) or ``target_count > source_count`` (impossible in
      steady state under ``ON CONFLICT DO UPDATE``, treated defensively).
      The caller runs the inner loop.
    - **indeterminate** — the table has no ``_VERIFY_TABLES`` mapping, the
      count source returned ``None`` (unreachable), or its response
      omitted the relation. NEVER treated as a pass.

    Dedup guard (audit correction 2, nexus-s3dd4 comment 2026-07-02):
    relations in ``_VERIFY_TABLES_DEDUP`` (currently only ``nexus.plans``,
    keyed ``UNIQUE(tenant_id, project, query)`` with ``ON CONFLICT DO
    UPDATE``) converge source duplicates onto one target row server-side.
    A landed count below the written count there is convergence-by-design,
    not a hole — this mirrors ``orchestrator.verify_counts``'s dedup branch
    (orchestrator.py) so both surfaces agree on what "parity" means for a
    dedup relation.
    """
    if not source_counts:
        return {}

    table_relations: dict[str, str] = {
        table: relation
        for table in source_counts
        if (relation := _VERIFY_TABLES.get((store, table))) is not None
    }

    relations = sorted(set(table_relations.values()))
    target_counts = count_source.counts(relations) if relations else None

    result: dict[str, TableVerdict] = {}
    for table, source_count in source_counts.items():
        relation = table_relations.get(table)
        if relation is None:
            _log.debug(
                "verify_fill.table_unmapped", store=store, table=table,
            )
            result[table] = _verdict(source_count, None, "indeterminate")
            continue

        if target_counts is None or relation not in target_counts:
            _log.warning(
                "verify_fill.table_indeterminate",
                store=store, table=table, relation=relation,
                reason="count source unreachable" if target_counts is None
                else "relation omitted from response",
            )
            result[table] = _verdict(source_count, None, "indeterminate")
            continue

        target_count = int(target_counts[relation])
        status = _table_status(relation, source_count, target_count)
        if status == "divergent":
            _log.info(
                "verify_fill.table_divergent",
                store=store, table=table, relation=relation,
                source_count=source_count, target_count=target_count,
            )
        result[table] = _verdict(source_count, target_count, status)

    return result


def _table_status(relation: str, source_count: int, target_count: int) -> str:
    """Parity/divergent verdict for one already-resolved relation count."""
    if relation in _VERIFY_TABLES_DEDUP:
        # Mirrors orchestrator.verify_counts's convergence-aware dedup
        # branch: written=0 is a trivial pass; 0 < target <= source is an
        # exact match or a by-design convergence collapse; target == 0 from
        # a non-zero write, or target > source, is a real divergence.
        if source_count == 0:
            return "parity"
        if 0 < target_count <= source_count:
            return "parity"
        return "divergent"
    return "parity" if target_count >= source_count else "divergent"


def _verdict(
    source_count: int, target_count: int | None, status: str,
) -> TableVerdict:
    return {
        "source_count": source_count,
        "target_count": target_count,
        "status": status,
    }
