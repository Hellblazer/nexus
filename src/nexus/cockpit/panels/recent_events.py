# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recent-events panel: newest-first slice of the ``events`` table.

The ``events`` table is the append-only projection of every committed
tuple op (RDR-112 §EventStream). Cursor (rowid) is strictly monotonic so
newest-first = ORDER BY rowid DESC.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Optional


@dataclasses.dataclass(frozen=True)
class EventRow:
    """One event-table row, surface-friendly."""

    cursor: int
    subspace: str
    op: str
    tuple_id: str
    ts: float
    payload_summary: Optional[str]
    category: Optional[str]


@dataclasses.dataclass(frozen=True)
class RecentEventsResult:
    rows: list[EventRow]


_SELECT_RECENT_EVENTS = """\
SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts
FROM events
ORDER BY rowid DESC
LIMIT ?
"""


def fetch_recent_events(
    *,
    conn: sqlite3.Connection,
    limit: int = 25,
) -> RecentEventsResult:
    """Return the newest ``limit`` rows from the events table."""
    if limit <= 0:
        return RecentEventsResult(rows=[])
    cur = conn.execute(_SELECT_RECENT_EVENTS, (int(limit),))
    rows: list[EventRow] = []
    for r in cur.fetchall():
        if isinstance(r, sqlite3.Row):
            rows.append(
                EventRow(
                    cursor=int(r["rowid"]),
                    subspace=r["subspace"],
                    op=r["op"],
                    tuple_id=r["tuple_id"],
                    ts=float(r["ts"]),
                    payload_summary=r["payload_summary"],
                    category=r["category"],
                )
            )
        else:
            rid, subspace, op, tid, payload, category, ts = r
            rows.append(
                EventRow(
                    cursor=int(rid),
                    subspace=subspace,
                    op=op,
                    tuple_id=tid,
                    ts=float(ts),
                    payload_summary=payload,
                    category=category,
                )
            )
    return RecentEventsResult(rows=rows)
