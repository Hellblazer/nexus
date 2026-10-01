# SPDX-License-Identifier: AGPL-3.0-or-later
"""Age seeded chunks past reapable(c)'s grace window, with substrate SQL.

RDR-192 Step 8 (nexus-wbfpw.16): ``gc_quarantine_orphans`` and its bounded twin
select candidates with ``nexus.chunk_is_reapable``, which honours a 30 day grace
window on ``nexus.chunks.last_written_at``. A test that seeds an orphan and then
expects a gc pass to quarantine it has to say the orphan is old. A seeded row
carries ``last_written_at = now()``, so :func:`age_chunks_past_grace` pushes it
back, the way ``tests/_chunk_seed.py`` builds the rows in the first place: by
substrate SQL, never a write route (a route would restamp it).

Only ``last_written_at`` moves. ``created_at`` is write-once and some tests pin it.
"""
from __future__ import annotations

from tests._chunk_seed import _lit, _pg_state, _psql_superuser, ambient_tenant

#: Comfortably past the engine's 30 day default grace window.
PAST_GRACE_DAYS = 40


def age_chunks_past_grace(collection: str, *, tenant: str | None = None,
                          days: int = PAST_GRACE_DAYS) -> int:
    """Push ``last_written_at`` of every chunk of *collection* back *days* days.

    Returns the number of rows aged, and raises when that is zero: a call that
    aged nothing would leave the gc pass it precedes a vacuous no-op, which the
    test would then misread as the grace window working.
    """
    tenant = tenant or ambient_tenant()
    out = _psql_superuser(
        _pg_state(),
        "WITH u AS (UPDATE nexus.chunks SET last_written_at = now() - "
        f"interval '{int(days)} days' WHERE tenant_id = {_lit(tenant)} "
        f"AND collection = {_lit(collection)} RETURNING 1) SELECT count(*) FROM u",
    )
    aged = int(out)
    if aged == 0:
        raise AssertionError(
            f"age_chunks_past_grace: no chunk of {collection!r} for tenant {tenant!r}; "
            "seed the chunks first")
    return aged
