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
    conn: sqlite3.Connection,
    *,
    apply: bool,
) -> BackfillReport:
    """Backfill ``topic_assignments.source_collection`` from ``topics.collection``.

    Args:
        conn: live sqlite3 connection for a ``T2Database`` that has both
            ``topic_assignments`` and ``topics`` tables.
        apply: when ``False`` (default at the CLI), only reports what
            would be written. When ``True``, runs the UPDATE inside a
            single transaction with rollback on error.

    Returns a :class:`BackfillReport` with before / projected / after
    coverage numbers and per-``assigned_by`` eligible counts.
    """
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
        try:
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
