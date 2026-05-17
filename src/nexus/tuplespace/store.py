# SPDX-License-Identifier: Apache-2.0
"""SQLite schema for tuples.db, the T2 claim ledger for RDR-110.

New database `~/.config/nexus/tuples.db` (separate from ``memory.db``
for operational isolation). Contains:

- ``tuples``, body store + claim state. Single-table-with-state-column
  pattern (honker RF-9). Atomicity via ``UPDATE … RETURNING`` under
  SQLite's single-writer lock. Tombstone columns follow RDR-106/107 style.
- ``tuple_claim_log``, append-only audit trail for every state transition
  (claim, ack, nack, expire). Never updated; never deleted except by the
  30-day retention sweep. Includes ``failure_category`` for nack demux.
- ``events``, append-only projection of every committed tuple operation,
  populated by AFTER INSERT triggers on ``tuples`` (op='out') and
  ``tuple_claim_log`` (op=transition). Provides monotonic ``rowid``
  cursors for the EventStream RPC (RDR-112 P1.3, nexus-m4gm).

Migration coordination with RDR-112 daemon (nexus-w0et)
--------------------------------------------------------
Per RDR-112 §9, the daemon is the sole migration runner for tuples.db.
This module exposes:

- ``TUPLES_SCHEMA_DDL``, the idempotent DDL string; the daemon's
  manifest (bead nexus-w0et) should import and execute this directly.
- ``apply_tuples_schema(conn)``, idempotent direct-mode applier. Used
  by the direct-mode path (``NX_STORAGE_MODE=direct``) and by unit tests
  that need a fresh in-process database.
- ``open_tuples_db(path)``, opens the database file, enables WAL, and
  calls ``apply_tuples_schema``. The daemon calls this once at startup;
  direct-mode callers use it per-process.

nexus-w0et integration note: import ``TUPLES_SCHEMA_DDL`` and call
``conn.executescript(TUPLES_SCHEMA_DDL)`` inside the daemon's migration
manifest function. ``apply_tuples_schema`` is a thin wrapper around
exactly that, the daemon may call either form.

EventStream (nexus-m4gm, RDR-112 P1.3)
---------------------------------------
The ``events`` table is the cursor source for the EventStream RPC.
Two triggers maintain it automatically:

- ``trg_tuples_out``: fires on every INSERT into ``tuples``, emitting an
  'out' event with the new tuple's subspace and id.
- ``trg_claim_log_event``: fires on every INSERT into ``tuple_claim_log``,
  emitting the transition (claim/ack/nack/expire) as the op. For 'nack'
  transitions, the ``failure_category`` column is propagated.

The ``events`` table is read by the daemon's ``event_stream.subscribe``
handler via ``SELECT … WHERE subspace GLOB ? AND rowid > ? ORDER BY rowid
LIMIT 1000``. Consumers (RDR-111 binding-watcher) persist ``last_cursor``
and reconnect with ``since_cursor=last_cursor`` for at-least-once delivery.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

TUPLES_DB_NAME: str = "tuples.db"
"""Filename for the tuples database (relative to the nexus config dir)."""

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

TUPLES_SCHEMA_DDL: str = """\
-- Body store + claim state; the source of truth.
-- Chroma is a derived index over (id, embed_text, dimensions_json) per RDR-108.
CREATE TABLE IF NOT EXISTS tuples (
    id              TEXT PRIMARY KEY,
    subspace        TEXT NOT NULL,
    template_name   TEXT NOT NULL,
    content         TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    embed_text      TEXT NOT NULL,
    match_text      TEXT,                           -- caller-supplied override for embedding source; participates in id
    created_at      REAL NOT NULL,
    expires_at      REAL,                           -- TTL; NULL = no expiry
    -- Claim state (formerly tuple_claims). Atomic via UPDATE ... RETURNING.
    claim_state     TEXT,                           -- NULL = available; 'claimed' = in-flight
    claimant        TEXT,                           -- set with claim_state
    claim_id        TEXT,                           -- the value returned by take()
    claim_expires_at REAL,                          -- lease expiry; NULL when claim_state IS NULL
    -- Tombstone state (RDR-106 / RDR-107 style).
    consumed_at     REAL,                           -- NULL = available
    consumed_by     TEXT
);
-- Working-set partial index per honker's pattern (RF-9).
-- Tombstones don't slow the claim path because they're excluded.
-- NOTE: claim_expires_at < unixepoch() is intentionally OMITTED from this
-- predicate because SQLite prohibits non-deterministic functions in partial
-- index definitions when the index is referenced by UPDATE/DELETE statements
-- (sqlite3.OperationalError: non-deterministic use of unixepoch() in an index).
-- The expiry guard appears inline in every query's WHERE clause instead.
CREATE INDEX IF NOT EXISTS idx_tuples_avail
    ON tuples (subspace, expires_at)
    WHERE consumed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tuples_claimed
    ON tuples (claim_id) WHERE claim_state = 'claimed';
CREATE INDEX IF NOT EXISTS idx_tuples_expires
    ON tuples (expires_at) WHERE expires_at IS NOT NULL AND consumed_at IS NULL;

-- Append-only claim history for audit. Insert-only; never updated.
-- Records every state transition (claim, ack, nack, expiry-release).
-- failure_category is set on nack rows for EventStream demux (nexus-m4gm).
CREATE TABLE IF NOT EXISTS tuple_claim_log (
    log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tuple_id         TEXT NOT NULL,
    subspace         TEXT NOT NULL,                 -- denormalized from tuples.subspace (nexus-pce1.4)
    claim_id         TEXT NOT NULL,
    claimant         TEXT NOT NULL,
    transition       TEXT NOT NULL,                 -- 'claim' | 'ack' | 'nack' | 'expire'
    failure_category TEXT,                          -- set on nack; NULL for claim/ack/expire
    at               REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_log_tuple
    ON tuple_claim_log (tuple_id, at);
CREATE INDEX IF NOT EXISTS idx_claim_log_claimant
    ON tuple_claim_log (claimant, at);

-- Watcher cursor state per (subspace, profile) pair (RDR-111 Phase 2 Step 6).
-- Lives in tuples.db (not memory.db) so that cursor and tuple reads are in
-- the same database for genuine atomicity (RDR-111 lines 783-786).
-- Added by nexus-w0et (RDR-112 P1.4 daemon-startup migration runner).
CREATE TABLE IF NOT EXISTS watcher_state (
    subspace   TEXT NOT NULL,
    profile    TEXT NOT NULL,
    last_rowid INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (subspace, profile)
);

-- Append-only event projection for EventStream RPC (RDR-112 P1.3, nexus-m4gm).
-- Every committed tuple operation lands here via triggers; monotonic rowid
-- provides the cursor for at-least-once streaming delivery.
CREATE TABLE IF NOT EXISTS events (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    subspace        TEXT NOT NULL,
    op              TEXT NOT NULL,                  -- 'out' | 'claim' | 'ack' | 'nack' | 'expire'
    tuple_id        TEXT NOT NULL,
    payload_summary TEXT,                           -- substr of content for stream consumers
    category        TEXT,                           -- failure category for nack events
    ts              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_subspace_rowid
    ON events (subspace, rowid);

-- Trigger: emit 'out' event on every new tuple insertion.
CREATE TRIGGER IF NOT EXISTS trg_tuples_out
    AFTER INSERT ON tuples
BEGIN
    INSERT INTO events (subspace, op, tuple_id, payload_summary, category, ts)
    VALUES (
        NEW.subspace,
        'out',
        NEW.id,
        SUBSTR(NEW.content, 1, 256),
        'data',
        NEW.created_at
    );
END;

-- Trigger: emit a transition event on every tuple_claim_log insertion.
-- Propagates failure_category for nack rows; uses 'data' for all others.
-- Uses NEW.subspace directly (denormalized at insert time) so the event
-- survives even when the source tuple has been hard-deleted (e.g. by a
-- retention sweep that races a slow nack). nexus-pce1.4.
CREATE TRIGGER IF NOT EXISTS trg_claim_log_event
    AFTER INSERT ON tuple_claim_log
BEGIN
    INSERT INTO events (subspace, op, tuple_id, payload_summary, category, ts)
    VALUES (
        NEW.subspace,
        NEW.transition,
        NEW.tuple_id,
        NULL,
        CASE
            WHEN NEW.transition = 'nack' THEN COALESCE(NEW.failure_category, 'unknown')
            ELSE 'data'
        END,
        NEW.at
    );
END;

-- Admin registry for third-party subspace schemas (RDR-112 P1.5, nexus-x98k).
-- Persists YAML schemas submitted via the ``subspace_add`` admin RPC.
-- Lives in tuples.db alongside the tuple stores so future cross-table
-- queries (e.g. validating a tuple's subspace against registered schemas)
-- remain single-connection and atomic.
CREATE TABLE IF NOT EXISTS subspace_registry (
    name          TEXT    PRIMARY KEY,
    yaml          TEXT    NOT NULL,
    schema_digest TEXT    NOT NULL,
    added_at      REAL    NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_tuples_schema(conn: sqlite3.Connection) -> None:
    """Apply the tuples.db schema to *conn* (idempotent).

    Uses ``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS``
    throughout, so re-running on an already-initialised database is a
    guaranteed no-op.

    This is the direct-mode applier. In daemon mode the daemon's migration
    manifest (bead nexus-w0et) calls this function or executes
    ``TUPLES_SCHEMA_DDL`` directly as the canonical migration step.

    Args:
        conn: An open ``sqlite3.Connection`` to a tuples.db file.
    """
    conn.executescript(TUPLES_SCHEMA_DDL)
    conn.commit()
    _log.info("tuples_schema_applied", db=_db_path_hint(conn))


def open_tuples_db(path: Path) -> sqlite3.Connection:
    """Open (or create) the tuples database at *path*.

    Steps performed:
    1. Open the SQLite file (creates it if it does not exist).
    2. Enable WAL journal mode (project convention; required for
       multi-process concurrent access per RDR-112 §9).
    3. Apply the schema via ``apply_tuples_schema`` (idempotent).

    The caller is responsible for closing the returned connection.

    .. warning::

       Direct-mode upgrade gap. ``apply_tuples_schema`` only runs the
       baseline DDL via ``CREATE TABLE IF NOT EXISTS``; it does NOT run
       the column-addition migrations in ``nexus.db.migrations``
       (``migrate_tuples_failure_category``, ``migrate_tuples_claim_log_subspace``,
       etc.). Per RDR-112 §9 the daemon is the sole migration runner, so
       a pre-pce1.4 ``tuples.db`` opened in direct mode will still have
       ``tuple_claim_log`` without ``subspace`` / ``failure_category``
       and the next ``api.take()``/``ack()``/``nack()`` INSERT will
       fail with ``NOT NULL constraint failed: tuple_claim_log.subspace``.
       Direct mode is safe ONLY against freshly-created databases or
       databases that have been migrated by a daemon-mode start at least
       once. Operators upgrading should run ``nx daemon t2 start``
       before reverting to direct mode.

    Args:
        path: Filesystem path to the ``tuples.db`` file. Typically
            ``~/.config/nexus/tuples.db``.

    Returns:
        An open ``sqlite3.Connection`` with WAL mode enabled and
        schema applied.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    apply_tuples_schema(conn)
    _log.info("tuples_db_opened", path=str(path))
    return conn


# ---------------------------------------------------------------------------
# Retention sweep (nexus-kk9h, RDR-111)
# ---------------------------------------------------------------------------


def prune_expired_tuples(
    conn: sqlite3.Connection,
    *,
    index: Any = None,
    registry: Any = None,
    now: int | None = None,
) -> int:
    """Delete tuples whose ``expires_at`` has passed and their Chroma vectors.

    Rows with ``expires_at IS NULL`` are NEVER deleted: NULL means "no
    expiry" per the schema contract.

    nexus-qu6t: the partial index ``idx_tuples_expires`` is defined on
    ``(expires_at, consumed_at IS NULL)`` and therefore only covers
    rows that have not yet been consumed. The SELECT below fetches
    every expired row regardless of ``consumed_at`` (Chroma cleanup
    for consumed-and-expired rows is this sweep's responsibility:
    ``ack()`` deletes the SQLite row claim metadata but not the
    Chroma vector). Net effect: the partial index speeds up the
    fast path (available tuples that timed out without ever being
    claimed); consumed-but-expired rows fall through to a full
    table scan. Acceptable given typical TTLs are minutes-to-hours
    and the sweep runs every six hours.

    **Atomicity note (RDR-111).** Two-store atomicity between SQLite and
    Chroma is a separate concern (bead nexus-qmrr). This sweeper deletes
    from Chroma FIRST, then SQLite, so a crash mid-sweep leaves orphan
    SQLite rows (recoverable on the next sweep, since ``expires_at`` is
    still in the past) rather than orphan Chroma vectors (which would
    silently return stale ids in semantic queries).

    Args:
        conn: Open SQLite connection to tuples.db.
        index: Optional ``TupleIndex`` for the paired Chroma deletion.
            When ``None``, Chroma is not touched (SQLite-only prune; used
            by tests that don't care about the Chroma side).
        registry: Optional ``Registry``, accepted for symmetry with the
            rest of the tuplespace API, currently unused (template_name
            comes from the tuple row itself).
        now: Optional epoch seconds to compare ``expires_at`` against.
            Defaults to ``int(time.time())``. Tests use this to simulate
            future sweeps without sleeping.

    Returns:
        Number of SQLite rows deleted.
    """
    del registry  # Currently unused; accepted for future schema-aware sweeps.
    now_epoch = int(time.time()) if now is None else int(now)

    rows = conn.execute(
        "SELECT id, template_name FROM tuples "
        "WHERE expires_at IS NOT NULL AND expires_at < ?",
        (now_epoch,),
    ).fetchall()

    if not rows:
        return 0

    # Group ids by template_name so each collection.delete() is a single batch.
    by_template: dict[str, list[str]] = {}
    for row in rows:
        tid, template = row[0], row[1]
        by_template.setdefault(template, []).append(tid)

    # --- Chroma first, then SQLite (see atomicity note above) ---
    if index is not None:
        for template_name, ids in by_template.items():
            try:
                index.delete(template_name=template_name, tuple_ids=ids)
            except KeyError:
                # Template not registered in this index (e.g. schema removed).
                # Skip the Chroma half; SQLite row deletion proceeds below.
                _log.warning(
                    "prune_expired_chroma_template_missing",
                    template=template_name,
                    count=len(ids),
                )
            except Exception as exc:  # pragma: no cover, defensive
                _log.error(
                    "prune_expired_chroma_delete_failed",
                    template=template_name,
                    count=len(ids),
                    error=str(exc),
                )
                # Don't proceed to SQLite delete for this batch, leave the
                # rows so the next sweep retries.
                by_template[template_name] = []

    # Delete from SQLite. Chunk by SQLite parameter limit (max 999 by default
    # in CPython's stdlib; use 300 to match the Chroma write quota).
    deleted = 0
    all_ids: list[str] = [tid for ids in by_template.values() for tid in ids]
    BATCH = 300
    for i in range(0, len(all_ids), BATCH):
        chunk = all_ids[i : i + BATCH]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"DELETE FROM tuples WHERE id IN ({placeholders})",
            chunk,
        )
        deleted += cur.rowcount
    conn.commit()

    if deleted:
        _log.info(
            "tuples_retention_swept",
            deleted=deleted,
            templates=len(by_template),
            now_epoch=now_epoch,
        )
    return deleted


# Default events-table retention window. The events table is the cursor
# source for EventStream subscribers (RDR-112) and the cockpit binding
# watcher (RDR-111). Seven days lets a daemon recover from a multi-day
# subscriber outage while preventing unbounded growth on
# write-heavy systems (an active session emits hundreds of events per
# minute). nexus-anjo (RDR-112 A2 bundle C).
EVENTS_RETENTION_SECONDS_DEFAULT: int = 86400 * 7


def prune_old_events(
    conn: sqlite3.Connection,
    *,
    retention_seconds: int = EVENTS_RETENTION_SECONDS_DEFAULT,
    now: float | None = None,
) -> int:
    """Delete ``events`` rows older than ``retention_seconds``.

    nexus-anjo: the events table accumulates a row per tuple operation
    (out / claim / ack / nack / expire) and has no built-in cap. Without
    a periodic prune, the table grows linearly with write volume and
    eventually pessimises both the binding watcher's batch query and the
    EventStream RPC's backfill.

    Args:
        conn: Open SQLite connection to tuples.db.
        retention_seconds: Rows with ``ts < now - retention_seconds`` are
            deleted. Defaults to :data:`EVENTS_RETENTION_SECONDS_DEFAULT`.
        now: Optional epoch seconds. Defaults to ``time.time()``. Tests
            use this to simulate future sweeps without sleeping.

    Returns:
        Number of rows deleted.
    """
    now_epoch = time.time() if now is None else now
    cutoff = now_epoch - retention_seconds
    cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    if deleted:
        _log.info(
            "events_retention_swept",
            deleted=deleted,
            retention_seconds=retention_seconds,
            cutoff_epoch=cutoff,
        )
    return deleted


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _db_path_hint(conn: sqlite3.Connection) -> str:
    """Extract a path hint from the connection for logging.

    ``sqlite3.Connection`` does not expose the DB path directly; we use
    the ``PRAGMA database_list`` trick which returns (seq, name, file)
    rows. Falls back to ``"<unknown>"`` for in-memory or ephemeral DBs.
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row and row[2]:
            return row[2]
    except Exception:
        pass
    return "<unknown>"
