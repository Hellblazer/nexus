# SPDX-License-Identifier: AGPL-3.0-or-later
"""Substrate-agnostic T2 fixture manipulation for the unit suite (nexus-aqbrk).

RDR-158/155 substrate port, bucket 1. ~164 test sites reach into a store's
raw ``.conn`` to set up or read back state that the public store API does
not expose directly. Under the engine substrate those stores are the
``Http*Store`` variants, which have no ``.conn`` at all — they raise a
fail-loud ``AttributeError`` naming the fix (``_raw_handle_guard``).

The established per-file idiom (test_taxonomy_rebuild_link_cleanup.py,
test_nexus_lub_collection_delete_cascade.py) is an inline
``has_raw_access(store)`` branch: raw SQL on the SQLite arm, the store's
public ``import_*`` method on the service arm. That is correct but does not
scale to 164 sites — the HttpMemoryStore cluster alone is 51 of them, all
performing the same three operations. This module hoists those operations
into named helpers so each call site becomes substrate-agnostic and the
branch exists exactly once per operation.

WHY THIS SHRINKS RATHER THAN GROWS: when nexus-i711w deletes the SQLite
stores, every ``has_raw_access`` branch here collapses to its service arm
and the helpers stay. Inline branches at 164 sites would each need the
same collapse by hand.

READS ARE NOT BRANCHED. ``MemoryStore.get_all`` / ``HttpMemoryStore.get_all``
both return the full column set (``access_count``, ``last_accessed``,
``timestamp``) and — unlike ``get()`` and ``search()`` — neither tracks
access, so a read-back needs no raw cursor and no branch on either
substrate. Tests that previously read a column via ``SELECT ... FROM memory``
should call :func:`memory_row`, which is a true non-mutating read on both.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nexus.db.storage_mode import has_raw_access
from nexus.db.t2 import T2Database

__all__ = [
    "backdate_memory",
    "memory_row",
    "rewrite_memory_row",
    "seed_relevance",
    "set_memory_access_count",
    "utc_stamp",
]


def utc_stamp(*, days: float = 0, seconds: float = 0) -> str:
    """Return an ISO-8601 UTC stamp *days*/*seconds* in the past.

    Second precision with a trailing ``Z``, matching the format the Java
    service emits and the SQLite schema stores.
    """
    moment = datetime.now(UTC) - timedelta(days=days, seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def memory_row(db: T2Database, project: str, title: str) -> dict[str, Any] | None:
    """Return the full memory row for *(project, title)*, or ``None``.

    NON-MUTATING on both substrates — this is the point. ``db.get()`` and
    ``db.search()`` both increment ``access_count`` as a documented
    side-effect, so a test verifying access tracking cannot use them to
    observe the counter without perturbing it. ``get_all()`` performs no
    access tracking on either the SQLite or the service arm.
    """
    for row in db.memory.get_all(project):
        if row["title"] == title:
            return row
    return None


def rewrite_memory_row(
    db: T2Database,
    project: str,
    title: str,
    *,
    timestamp: str | None = None,
    access_count: int | None = None,
    last_accessed: str | None = None,
) -> None:
    """Overwrite tracking/event-time columns on an existing memory row.

    Fields left as ``None`` keep their current values. The row must already
    exist; this is fixture manipulation, not an insert path.

    SQLite arm: a targeted ``UPDATE``. Service arm: ``import_entry``, whose
    whole purpose is writing ``timestamp`` / ``access_count`` /
    ``last_accessed`` verbatim rather than letting the service stamp them
    (``POST /v1/memory/import``, upsert on ``(tenant_id, project, title)``,
    so row identity is preserved).

    Raises:
        LookupError: if no row matches *(project, title)*.
    """
    if timestamp is None and access_count is None and last_accessed is None:
        raise ValueError("rewrite_memory_row: nothing to rewrite")

    store = db.memory

    if has_raw_access(store):
        assignments: list[str] = []
        params: list[Any] = []
        if timestamp is not None:
            assignments.append("timestamp = ?")
            params.append(timestamp)
        if access_count is not None:
            assignments.append("access_count = ?")
            params.append(access_count)
        if last_accessed is not None:
            assignments.append("last_accessed = ?")
            params.append(last_accessed)
        params.extend((project, title))
        cur = store.conn.execute(
            f"UPDATE memory SET {', '.join(assignments)} "
            "WHERE project = ? AND title = ?",
            params,
        )
        store.conn.commit()
        if cur.rowcount == 0:
            raise LookupError(f"no memory row {project!r}/{title!r} to rewrite")
        return

    row = memory_row(db, project, title)
    if row is None:
        raise LookupError(f"no memory row {project!r}/{title!r} to rewrite")

    # import_entry treats last_accessed=None as "never accessed" (SQL NULL);
    # the stores normalise a NULL read back to "". Round-trip "" as None so
    # an untouched never-accessed row stays never-accessed.
    current_last_accessed = row.get("last_accessed") or None

    store.import_entry(
        project=project,
        title=title,
        content=row["content"],
        timestamp=timestamp if timestamp is not None else row["timestamp"],
        tags=row.get("tags") or "",
        ttl=row.get("ttl"),
        agent=row.get("agent"),
        session=row.get("session"),
        access_count=(
            access_count if access_count is not None
            else int(row.get("access_count") or 0)
        ),
        last_accessed=(
            last_accessed if last_accessed is not None else current_last_accessed
        ),
    )


def backdate_memory(
    db: T2Database,
    project: str,
    title: str,
    *,
    days: float = 0,
    seconds: float = 0,
) -> None:
    """Move a memory row's ``timestamp`` *days*/*seconds* into the past.

    The TTL-expiry fixture primitive: ``expire()`` compares ``timestamp``
    against the row's effective TTL on both substrates, so backdating is how
    a test ages an entry without a fake clock.
    """
    rewrite_memory_row(
        db, project, title, timestamp=utc_stamp(days=days, seconds=seconds)
    )


def set_memory_access_count(
    db: T2Database, project: str, title: str, count: int
) -> None:
    """Force a memory row's ``access_count`` to *count*.

    Prefer driving the counter through real reads (``db.get()`` N times)
    where the test's subject IS access tracking. This exists for the
    heat-weighted-expiry tests, where the counter is an input to the
    behaviour under test and N reads would be pure ceremony.
    """
    rewrite_memory_row(db, project, title, access_count=count)


def seed_relevance(
    db: T2Database,
    *,
    query: str,
    chunk_id: str,
    action: str = "stored",
    session_id: str = "",
    collection: str = "",
    age_days: float = 0,
) -> None:
    """Insert one ``relevance_log`` row aged *age_days* into the past.

    The retention-purge fixture primitive. ``log_relevance`` stamps
    ``timestamp=now()`` on both substrates, so a test exercising
    ``expire_relevance_log`` has to place the row in the past some other
    way: raw ``UPDATE`` on the SQLite arm, ``import_relevance_row`` (whose
    contract is a verbatim timestamp) on the service arm.

    Both arms write the same second-precision UTC stamp, which compares
    correctly against ``expire_relevance_log``'s ``isoformat()`` cutoff on
    either side.
    """
    stamp = utc_stamp(days=age_days)
    store = db.telemetry

    if has_raw_access(store):
        db.log_relevance(
            query, chunk_id, action, session_id=session_id, collection=collection
        )
        store.conn.execute(
            "UPDATE relevance_log SET timestamp = ? WHERE chunk_id = ?",
            (stamp, chunk_id),
        )
        store.conn.commit()
        return

    store.import_relevance_row(
        query=query,
        chunk_id=chunk_id,
        collection=collection,
        action=action,
        session_id=session_id,
        timestamp=stamp,
    )
