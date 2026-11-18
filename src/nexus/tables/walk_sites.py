# SPDX-License-Identifier: AGPL-3.0-or-later
"""The walk-site table, read from ``walk-sites.toml`` (RDR-241 Phase 1, nexus-uwggq.1.1).

One row per function in ``src/nexus`` that calls ``all_documents``, keyed
``<path under src/nexus>::<qualified function>``: the class that says why the
call exists, how many calls the function holds, one sentence on what it does
with the rows, and, for a site that is going away, the bead that removes or
lowers it. ``tests/test_walk_site_ratchet.py`` holds the source and this table
to an exact match, so the number of whole-catalog walks cannot rise without an
edit to this table; the test's docstring states what the scan does not see. This module loads the table through the house loader and closes the
class vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

TABLE_RESOURCE: Final = "walk-sites.toml"

#: AGGREGATE: the caller wants a derived answer and a route should give it.
#: BULK JOB: the caller must visit every document and stays a walk.
#: BOUNDED: the call reads one bounded page, or only a bounded mode of it.
CLASSES: Final[tuple[str, ...]] = ("AGGREGATE", "BULK JOB", "BOUNDED")


@dataclass(frozen=True)
class WalkSite:
    key: str
    cls: str
    calls: int
    reason: str
    #: the bead that removes or lowers the row; "" when nothing is planned
    next: str = ""


@lru_cache(maxsize=1)
def load_table() -> Table:
    return load_packaged_table(TABLE_RESOURCE)


def sites_from_table(table: Table) -> tuple[WalkSite, ...]:
    """Every site row of *table*, in file order; a malformed row is refused."""
    out: list[WalkSite] = []
    for row in table.rows:
        emit = row.outcome
        if not isinstance(emit, dict):
            raise ValueError(f"{TABLE_RESOURCE}: row {row.id!r} has no emit table")
        key = str(row.match["site"])
        missing = [f for f in ("class", "calls", "reason") if f not in emit]
        if missing:
            raise ValueError(f"{TABLE_RESOURCE}: site {key!r} lacks emit field(s) {missing}")
        cls, calls, reason, nxt = emit["class"], emit["calls"], emit["reason"], emit.get("next", "")
        if cls not in CLASSES:
            raise ValueError(f"{TABLE_RESOURCE}: site {key!r} has class {cls!r}, not one of {CLASSES}")
        if not isinstance(calls, int) or isinstance(calls, bool) or calls < 1:
            raise ValueError(f"{TABLE_RESOURCE}: site {key!r} has calls {calls!r}, not an integer of at least 1")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{TABLE_RESOURCE}: site {key!r} has no reason")
        if key.count("::") != 1 or not key.split("::")[0].endswith(".py"):
            raise ValueError(f"{TABLE_RESOURCE}: site {key!r} is not '<path>.py::<qualified function>'")
        out.append(WalkSite(key=key, cls=cls, calls=calls, reason=reason, next=str(nxt)))
    return tuple(out)


@lru_cache(maxsize=1)
def sites() -> tuple[WalkSite, ...]:
    return sites_from_table(load_table())
