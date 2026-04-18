# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``topic_assignments.source_collection`` backfill — RDR-087 Phase 4.1.

Legacy rows (pre-RDR-077 projection path) left ``source_collection``
``NULL`` because clustering happens per-collection and the column was
only introduced once cross-collection projection started caring about
the source.

Invariants that make a deterministic backfill safe:

- **hdbscan / centroid** rows were produced by a single-collection
  clustering pass. Every assignment is from a chunk in that
  collection, and the topic it points at lives in the same
  collection. Therefore ``source_collection == topics.collection``
  holds by construction.
- **projection** rows already have the field populated (and usually
  disagree with ``topics.collection`` — that's the whole point of
  cross-collection projection). Do not touch them.
- **auto-matched** rows are the ambiguous case: a chunk from
  collection X may have auto-matched to a topic in collection Y.
  Leaving them NULL is the honest default; a later pass could do a
  best-effort T3 walk but that's out of scope here.

Writes are transaction-wrapped with explicit ``commit`` / ``rollback``
and run only when ``apply=True``. Dry-run is the default both here
and at the CLI layer.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy


_ELIGIBLE_ASSIGNED_BY: tuple[str, ...] = ("hdbscan", "centroid")


@dataclass(frozen=True)
class BackfillReport:
    dry_run: bool
    total_rows: int
    non_null_before: int
    eligible_rows: int
    updated_rows: int  # 0 on dry-run
    eligible_by_category: dict[str, int] = field(default_factory=dict)

    @property
    def coverage_before(self) -> float:
        return self.non_null_before / self.total_rows if self.total_rows else 0.0

    @property
    def coverage_projected(self) -> float:
        if not self.total_rows:
            return 0.0
        return (self.non_null_before + self.eligible_rows) / self.total_rows

    @property
    def coverage_after(self) -> float:
        if not self.total_rows:
            return 0.0
        return (self.non_null_before + self.updated_rows) / self.total_rows


def backfill_source_collection(
    taxonomy_or_conn: "CatalogTaxonomy | sqlite3.Connection",
    *,
    apply: bool,
) -> BackfillReport:
    """Backfill ``topic_assignments.source_collection`` from ``topics.collection``.

    Args:
        taxonomy_or_conn: preferred — a ``CatalogTaxonomy`` store whose
            ``_lock`` will be held for the entire read + UPDATE sequence,
            matching the contract every other writer on this connection
            observes. A raw ``sqlite3.Connection`` is also accepted for
            backward compatibility with existing tests that manipulate
            the connection directly; the caller must guarantee no
            concurrent writers are touching the same connection.
        apply: when ``False`` (default at the CLI), only reports what
            would be written. When ``True``, runs the UPDATE inside an
            explicit ``BEGIN IMMEDIATE`` / ``COMMIT`` pair with rollback
            on error, so no prior implicit transaction held on the
            connection can interleave with this write.

    Returns a :class:`BackfillReport` with before / projected / after
    coverage numbers and per-``assigned_by`` eligible counts.

    Concurrency (review gate C-1): the prior shape accepted a raw
    ``conn`` and bypassed ``CatalogTaxonomy._lock``, racing any
    concurrent writer on the same connection. The new signature
    threads the lock through when a taxonomy store is passed.
    """
    # Resolve (conn, optional lock) so the rest of the function is a
    # single path. Both branches fall through to the same logic under
    # the conditional ``_hold_lock`` context manager.
    from contextlib import nullcontext

    if hasattr(taxonomy_or_conn, "_lock") and hasattr(taxonomy_or_conn, "conn"):
        conn = taxonomy_or_conn.conn
        _hold_lock = taxonomy_or_conn._lock
    elif isinstance(taxonomy_or_conn, sqlite3.Connection):
        conn = taxonomy_or_conn
        _hold_lock = nullcontext()
    else:
        raise TypeError(
            "backfill_source_collection expects a CatalogTaxonomy or "
            f"sqlite3.Connection; got {type(taxonomy_or_conn).__name__}"
        )

    with _hold_lock:
        total, non_null = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN source_collection IS NULL THEN 0 ELSE 1 END) "
            "FROM topic_assignments"
        ).fetchone()
        total = int(total or 0)
        non_null = int(non_null or 0)

        eligible_rows_by_category: dict[str, int] = {}
        placeholders = ",".join("?" * len(_ELIGIBLE_ASSIGNED_BY))
        for cat, count in conn.execute(
            f"SELECT assigned_by, COUNT(*) FROM topic_assignments "
            f"WHERE source_collection IS NULL "
            f"AND assigned_by IN ({placeholders}) "
            "GROUP BY assigned_by",
            _ELIGIBLE_ASSIGNED_BY,
        ).fetchall():
            if count:
                eligible_rows_by_category[cat] = int(count)
        eligible_rows = sum(eligible_rows_by_category.values())

        updated_rows = 0
        if apply and eligible_rows:
            # Explicit transaction boundary — avoids committing any
            # prior implicit DEFERRED transaction the caller may have
            # left open on this connection (review I-2 hazard).
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    f"UPDATE topic_assignments "
                    f"SET source_collection = ("
                    f"    SELECT collection FROM topics "
                    f"    WHERE topics.id = topic_assignments.topic_id"
                    f") "
                    f"WHERE source_collection IS NULL "
                    f"AND assigned_by IN ({placeholders})",
                    _ELIGIBLE_ASSIGNED_BY,
                )
                updated_rows = cur.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    return BackfillReport(
        dry_run=not apply,
        total_rows=total,
        non_null_before=non_null,
        eligible_rows=eligible_rows,
        updated_rows=updated_rows,
        eligible_by_category=eligible_rows_by_category,
    )
