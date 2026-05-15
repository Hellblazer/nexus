# SPDX-License-Identifier: AGPL-3.0-or-later
"""Active-claims panel: in-flight claims grouped by subspace.

Reads the ``tuples`` table directly (claim state lives there per RDR-110
store.py refactor). A row is "active" iff ``claim_state = 'claimed'`` and
the claim has not expired (``claim_expires_at`` is NULL or in the future).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from collections import defaultdict
from typing import Optional


@dataclasses.dataclass(frozen=True)
class ClaimRow:
    """One in-flight claim row from the panel."""

    subspace: str
    tuple_id: str
    claim_id: str
    claimant: str
    ttl_remaining_seconds: Optional[float]
    claimed_subspace: str = ""  # alias retained for forward compat; unused

    def __post_init__(self) -> None:
        # Silence the unused alias field warning under strict linters.
        pass


@dataclasses.dataclass(frozen=True)
class ActiveClaimsResult:
    """Panel payload: rows + group helper."""

    rows: list[ClaimRow]

    @property
    def total(self) -> int:
        return len(self.rows)

    def groups_by_subspace(self) -> dict[str, list[ClaimRow]]:
        """Return rows grouped by subspace, stable ordering."""
        out: dict[str, list[ClaimRow]] = defaultdict(list)
        for r in self.rows:
            out[r.subspace].append(r)
        return dict(out)


_SELECT_ACTIVE_CLAIMS = """\
SELECT
    subspace,
    id            AS tuple_id,
    claim_id,
    claimant,
    claim_expires_at
FROM tuples
WHERE claim_state = 'claimed'
  AND consumed_at IS NULL
  AND (claim_expires_at IS NULL OR claim_expires_at > ?)
ORDER BY subspace, claim_expires_at
"""


def fetch_active_claims(
    *,
    conn: sqlite3.Connection,
    now: Optional[float] = None,
) -> ActiveClaimsResult:
    """Return all currently-active claims as a structured panel payload.

    Pure read; no side effects, no writes. Safe under WAL with a daemon
    writer present.
    """
    ts = time.time() if now is None else now
    rows: list[ClaimRow] = []
    cur = conn.execute(_SELECT_ACTIVE_CLAIMS, (ts,))
    for r in cur.fetchall():
        # Support both Row and tuple cursors.
        if isinstance(r, sqlite3.Row):
            expires = r["claim_expires_at"]
            row = ClaimRow(
                subspace=r["subspace"],
                tuple_id=r["tuple_id"],
                claim_id=r["claim_id"] or "",
                claimant=r["claimant"] or "",
                ttl_remaining_seconds=(
                    None if expires is None else max(0.0, float(expires) - ts)
                ),
            )
        else:
            subspace, tuple_id, claim_id, claimant, expires = r
            row = ClaimRow(
                subspace=subspace,
                tuple_id=tuple_id,
                claim_id=claim_id or "",
                claimant=claimant or "",
                ttl_remaining_seconds=(
                    None if expires is None else max(0.0, float(expires) - ts)
                ),
            )
        rows.append(row)
    return ActiveClaimsResult(rows=rows)
