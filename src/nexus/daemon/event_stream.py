# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daemon-side EventStream RPC implementation, RDR-112 P1.3 (nexus-m4gm).

Provides ``handle_event_stream``, the asyncio coroutine that handles the
``event_stream.subscribe`` op.  When called, it:

1. Sends a ``{"subscribed": True, "cursor": <since_cursor>}`` ack frame.
2. Runs a backfill phase: queries ``events WHERE subspace GLOB ? AND
   rowid > since_cursor ORDER BY rowid LIMIT 1000`` in a loop until caught up.
3. Switches to live mode: polls ``PRAGMA data_version`` every 10 ms; on each
   commit fetches new ``rowid > last_emitted`` rows and pushes them.
4. Exits when the client closes the connection (detected via reader EOF) or
   the daemon is stopping.

Wire frames
-----------
Subscribe request (from client)::

    {"op": "event_stream.subscribe",
     "args": {"subspace_prefix": "tuples/foo",
              "since_cursor": 42,
              "where": {"category": "timeout"}}}

Ack (first frame from daemon)::

    {"subscribed": True, "cursor": 42}

Event frames::

    {"event": {"cursor": 101, "subspace": "tuples/foo/bar",
               "op": "out", "tuple_id": "abc123",
               "payload_summary": "...", "category": "data", "ts": 1234.5}}

Connection close terminates the subscription with no explicit frame.

Polling cadence
---------------
- 10 ms sleep between data_version polls while the subscription is open.
- All SQLite I/O runs via ``asyncio.to_thread`` to avoid blocking the event loop.

Backfill cap
------------
Each backfill SELECT is capped at ``BACKFILL_BURST_LIMIT`` rows (1 000) to
avoid OOM on large catch-ups.  The loop repeats until no more rows are
returned, then transitions to live mode.

Category filter
---------------
An optional ``where: {category: <str>}`` arg in the subscribe request causes
the daemon to filter events by ``category`` column in the SELECT WHERE clause.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: Each backfill SELECT burst fetches at most this many rows.
BACKFILL_BURST_LIMIT: int = 1_000

#: Poll interval (seconds) while a subscriber is connected.
_POLL_ACTIVE: float = 0.010  # 10 ms

#: Allowed character set for the non-wildcard part of subspace_prefix. The
#: subspace name is path-shaped (e.g. ``tuples/tasks/coordinator``) so we
#: accept alphanumerics, slash, dash, underscore, and dot. The only allowed
#: GLOB metacharacter is a single trailing ``*``.
_SUBSPACE_PREFIX_BODY = re.compile(r"^[A-Za-z0-9_./-]+$")


def _validate_subspace_prefix(prefix: str) -> str | None:
    """Return None if *prefix* is safe to expand to a GLOB pattern, else an error string.

    Rejects:
    - GLOB metacharacters other than a single trailing ``*`` (``?`` matches any
      single character; ``[...]`` is a character class).
    - ``*`` appearing anywhere except as the final character.
    - Characters outside the path-safe set.
    """
    if "?" in prefix:
        return "subspace_prefix may not contain '?' (GLOB single-char wildcard)"
    if "[" in prefix or "]" in prefix:
        return "subspace_prefix may not contain '[' or ']' (GLOB bracket-class)"
    star_count = prefix.count("*")
    if star_count > 1:
        return "subspace_prefix may contain at most one '*' (as trailing wildcard)"
    if star_count == 1 and not prefix.endswith("*"):
        return "subspace_prefix '*' is only allowed as the trailing character"
    body = prefix[:-1] if prefix.endswith("*") else prefix
    if not _SUBSPACE_PREFIX_BODY.match(body):
        return "subspace_prefix contains characters outside [A-Za-z0-9_./-]"
    return None

# ---------------------------------------------------------------------------
# SQLite helpers (run in thread pool via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _fetch_events(
    conn: sqlite3.Connection,
    subspace_prefix: str,
    after_rowid: int,
    category: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fetch events matching subspace_prefix and rowid > after_rowid.

    Runs synchronously; caller must dispatch via asyncio.to_thread.

    Args:
        conn: Open SQLite connection to tuples.db.
        subspace_prefix: SQLite GLOB pattern (e.g. ``"tuples/foo*"``).
        after_rowid: Return only rows with ``rowid > after_rowid``.
        category: Optional category filter; if set, adds ``category = ?``.
        limit: Maximum rows to return per call.

    Returns:
        List of event dicts with keys: cursor, subspace, op, tuple_id,
        payload_summary, category, ts.
    """
    if category is not None:
        sql = (
            "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
            "FROM events "
            "WHERE subspace GLOB ? AND rowid > ? AND category = ? "
            "ORDER BY rowid LIMIT ?"
        )
        rows = conn.execute(sql, (subspace_prefix, after_rowid, category, limit)).fetchall()
    else:
        sql = (
            "SELECT rowid, subspace, op, tuple_id, payload_summary, category, ts "
            "FROM events "
            "WHERE subspace GLOB ? AND rowid > ? "
            "ORDER BY rowid LIMIT ?"
        )
        rows = conn.execute(sql, (subspace_prefix, after_rowid, limit)).fetchall()

    return [
        {
            "cursor": row[0],
            "subspace": row[1],
            "op": row[2],
            "tuple_id": row[3],
            "payload_summary": row[4],
            "category": row[5],
            "ts": row[6],
        }
        for row in rows
    ]


def _get_data_version(conn: sqlite3.Connection) -> int:
    """Read PRAGMA data_version (fast; runs synchronously)."""
    row = conn.execute("PRAGMA data_version").fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def handle_event_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    tuples_db_path: Path,
    args: dict[str, Any],
    stopping_fn: Any,
) -> None:
    """Handle an ``event_stream.subscribe`` connection in server-push mode.

    This coroutine takes ownership of *writer* for the lifetime of the
    subscription.  It returns when the client closes the connection or when
    the daemon is stopping (``stopping_fn()`` returns True).

    Args:
        reader: asyncio StreamReader for the connection.
        writer: asyncio StreamWriter for the connection.
        tuples_db_path: Filesystem path to tuples.db.
        args: Parsed ``args`` dict from the subscribe request. Keys:
            - ``subspace_prefix`` (str, required)
            - ``since_cursor`` (int, default 0)
            - ``where`` (dict, optional; only ``{"category": <str>}`` honoured)
        stopping_fn: Zero-arg callable returning bool; True when the daemon
            is stopping.
    """
    from nexus.daemon.t2_daemon import write_frame  # avoid circular at module level

    # --- Validate args ---
    # Error frames use the same ``{error: {type, message}}`` shape as the
    # dispatch-layer error frames so clients can consume them with a single
    # decoder. See ``t2_client._reraise_remote_error`` for the consumer side.
    subspace_prefix = args.get("subspace_prefix")
    if not subspace_prefix:
        write_frame(writer, {"error": {
            "type": "InvalidArgument",
            "message": "event_stream.subscribe: subspace_prefix is required",
        }})
        await writer.drain()
        return

    # nexus-pce1.5: reject GLOB metacharacters other than a single trailing '*'.
    # Without this, "tuples/foo?" would expand to "tuples/foo?*" where '?' is a
    # GLOB wildcard matching any single character, broader than the caller
    # likely intended. Also reject bracket-classes and '*' in non-terminal
    # position. Allowed character set is the path-safe subset.
    validation_error = _validate_subspace_prefix(subspace_prefix)
    if validation_error is not None:
        write_frame(writer, {"error": {
            "type": "InvalidArgument",
            "message": f"event_stream.subscribe: {validation_error}",
        }})
        await writer.drain()
        return

    since_cursor: int = int(args.get("since_cursor") or 0)
    where: dict[str, Any] = args.get("where") or {}
    category_filter: str | None = where.get("category") or None

    # Convert subspace_prefix to SQLite GLOB pattern: "tuples/foo" -> "tuples/foo*"
    glob_pattern = subspace_prefix if subspace_prefix.endswith("*") else subspace_prefix + "*"

    _log.info(
        "event_stream_subscribe",
        glob=glob_pattern,
        since_cursor=since_cursor,
        category_filter=category_filter,
    )

    # --- Open a dedicated read connection to tuples.db ---
    try:
        # storage-boundary-allow: daemon-owned tuples.db connection for EventStream
        conn = sqlite3.connect(str(tuples_db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA query_only=ON")  # read-only guard
    except Exception as exc:
        _log.error("event_stream_db_open_failed", error=str(exc))
        write_frame(writer, {"error": {
            "type": exc.__class__.__name__,
            "message": f"event_stream: failed to open tuples.db: {exc}",
        }})
        await writer.drain()
        return

    try:
        # --- Ack ---
        write_frame(writer, {"subscribed": True, "cursor": since_cursor})
        await writer.drain()

        last_emitted: int = since_cursor

        # --- Phase 1: Backfill ---
        while not stopping_fn():
            # Check client hasn't disconnected
            if _client_closed(reader):
                return

            batch = await asyncio.to_thread(
                _fetch_events, conn, glob_pattern, last_emitted, category_filter, BACKFILL_BURST_LIMIT
            )
            if not batch:
                break  # caught up; switch to live mode
            for event in batch:
                write_frame(writer, {"event": event})
                last_emitted = event["cursor"]
            await writer.drain()
            _log.debug("event_stream_backfill_batch", count=len(batch), last_cursor=last_emitted)

        # --- Phase 2: Live mode ---
        last_data_version: int = await asyncio.to_thread(_get_data_version, conn)

        while not stopping_fn():
            # Check client disconnect (non-blocking)
            if _client_closed(reader):
                return

            await asyncio.sleep(_POLL_ACTIVE)

            current_dv: int = await asyncio.to_thread(_get_data_version, conn)
            if current_dv == last_data_version:
                continue  # no commit since last poll

            # Drain ALL new events for this data_version bump in bursts of
            # BACKFILL_BURST_LIMIT. A single transaction can produce arbitrarily
            # many trigger-fired rows; only advance last_data_version once an
            # empty fetch confirms we're caught up. Otherwise a >cap write batch
            # would silently strand rows beyond the cap until another unrelated
            # write happened to bump data_version again.
            while True:
                batch = await asyncio.to_thread(
                    _fetch_events, conn, glob_pattern, last_emitted, category_filter, BACKFILL_BURST_LIMIT
                )
                if not batch:
                    last_data_version = current_dv  # caught up to this dv
                    break
                for event in batch:
                    write_frame(writer, {"event": event})
                    last_emitted = event["cursor"]
                try:
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    _log.debug("event_stream_client_gone_on_drain")
                    return
                if len(batch) < BACKFILL_BURST_LIMIT:
                    last_data_version = current_dv  # fewer than cap means we're caught up
                    break
                # Full burst: more rows may exist for this dv; loop without sleep
                # to drain the rest before checking the wake signal again.

        # Daemon stopping: notify client
        if stopping_fn():
            try:
                write_frame(writer, {"error": {
                    "type": "DaemonShuttingDown",
                    "message": "daemon is shutting down",
                }})
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    except (BrokenPipeError, ConnectionResetError, OSError) as exc:
        _log.debug("event_stream_connection_lost", error=str(exc))
    except Exception as exc:
        _log.warning("event_stream_error", error=str(exc), exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        _log.info("event_stream_closed", last_cursor=last_emitted)


def _client_closed(reader: asyncio.StreamReader) -> bool:
    """Non-blocking check: return True if the client has closed the connection.

    Uses ``StreamReader.at_eof()`` which is a synchronous, non-blocking check
    on the internal buffer state. Returns True when the feed-data callback has
    delivered an EOF (i.e., the client closed the connection) without consuming
    any buffered bytes.
    """
    return reader.at_eof()
