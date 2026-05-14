# SPDX-License-Identifier: Apache-2.0
"""SQLite schema for tuples.db — the T2 claim ledger for RDR-110.

New database `~/.config/nexus/tuples.db` (separate from ``memory.db``
for operational isolation). Contains two tables:

- ``tuples`` — body store + claim state. Single-table-with-state-column
  pattern (honker RF-9). Atomicity via ``UPDATE … RETURNING`` under
  SQLite's single-writer lock. Tombstone columns follow RDR-106/107 style.
- ``tuple_claim_log`` — append-only audit trail for every state transition
  (claim, ack, nack, expire). Never updated; never deleted except by the
  30-day retention sweep.

Migration coordination with RDR-112 daemon (nexus-w0et)
--------------------------------------------------------
Per RDR-112 §9, the daemon is the sole migration runner for tuples.db.
This module exposes:

- ``TUPLES_SCHEMA_DDL`` — the idempotent DDL string; the daemon's
  manifest (bead nexus-w0et) should import and execute this directly.
- ``apply_tuples_schema(conn)`` — idempotent direct-mode applier. Used
  by the direct-mode path (``NX_STORAGE_MODE=direct``) and by unit tests
  that need a fresh in-process database.
- ``open_tuples_db(path)`` — opens the database file, enables WAL, and
  calls ``apply_tuples_schema``. The daemon calls this once at startup;
  direct-mode callers use it per-process.

nexus-w0et integration note: import ``TUPLES_SCHEMA_DDL`` and call
``conn.executescript(TUPLES_SCHEMA_DDL)`` inside the daemon's migration
manifest function. ``apply_tuples_schema`` is a thin wrapper around
exactly that — the daemon may call either form.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS tuple_claim_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tuple_id        TEXT NOT NULL,
    claim_id        TEXT NOT NULL,
    claimant        TEXT NOT NULL,
    transition      TEXT NOT NULL,                  -- 'claim' | 'ack' | 'nack' | 'expire'
    at              REAL NOT NULL
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
