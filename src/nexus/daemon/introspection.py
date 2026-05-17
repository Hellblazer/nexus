# SPDX-License-Identifier: AGPL-3.0-or-later
"""Introspection RPC surface for the T2 daemon, RDR-112 P1.6 (nexus-08i1).

Provides ``IntrospectionService`` with five verbs:

- ``exec_raw``  -- arbitrary read-only SQL via ``sqlite3 URI mode=ro``.
- ``schema``    -- sqlite_master enumeration with optional filters.
- ``peek``      -- paged SELECT * with limit clamped to MAX_QUERY_RESULTS=300.
- ``stats``     -- row counts per table, DB file size, WAL size.
- ``export``    -- daemon-side streaming write (jsonl, csv, or sqlite backup).

Security model
--------------
``exec_raw`` and ``export`` are admin-only (UDS-gated via ``_ADMIN_OPS`` in
``t2_daemon.py``). ``schema``, ``peek``, and ``stats`` are read-only metadata
and safe over TCP.

``exec_raw`` opens a ``sqlite3.connect("file:<path>?mode=ro", uri=True)``
connection per call. The read-only constraint is enforced by SQLite's URI
flag, NOT by SQL pattern matching (which is unreliable).

Audit logging
-------------
Every ``exec_raw`` invocation emits a ``daemon/t2/lifecycle`` log event with
``op=raw-exec`` and ``sql_hash=sha256(sql)[:16]``. The full SQL is never
logged (may contain PII).
"""
from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any, Generator

import structlog

from nexus.db.chroma_quotas import QUOTAS

_log = structlog.get_logger(__name__)

# Maximum rows per peek call (hard cap from chroma quota constant for consistency).
_PEEK_MAX: int = QUOTAS.MAX_QUERY_RESULTS  # 300

#: Hard cap for ``exec_raw`` result rows. Above this the daemon raises rather
#: than materialising the result set into a Python list. Admin-only op but a
#: misbehaving caller should not be able to crash the daemon on a large table.
_EXEC_RAW_MAX_ROWS: int = 50_000


class IntrospectionService:
    """Read-only introspection over the T2 SQLite stores.

    Owns no persistent connections. ``exec_raw``, ``schema``, ``peek``,
    and ``stats`` each open a fresh ``mode=ro`` connection and close it in
    a ``finally`` block. ``export`` streams rows via a generator to avoid
    materialising the full result set in memory.

    Constructor injection only, no global state.

    Args:
        memory_db_path: Path to the T2 memory/plans/taxonomy/etc. SQLite DB.
        tuples_db_path: Path to the tuples.db (watcher_state, events, etc.).
    """

    def __init__(
        self,
        memory_db_path: Path,
        tuples_db_path: Path,
        export_root: Path | None = None,
    ) -> None:
        self._memory_db_path: Path = memory_db_path
        self._tuples_db_path: Path = tuples_db_path
        # Export destinations must resolve inside this root. Default is the
        # config dir parent (the db files' parent dir) so a fresh container
        # mount works without extra wiring. Operators with a dedicated export
        # volume should pass export_root explicitly.
        self._export_root: Path = (
            export_root.resolve()
            if export_root is not None
            else memory_db_path.parent.resolve()
        )

    def _validate_export_path(self, dest_path: str) -> Path:
        """Resolve ``dest_path`` and reject anything outside ``_export_root``.

        Defends against path-traversal (``../../etc/cron.d/...``) under an
        admin caller. Admin gate restricts who can call ``export``; this
        restricts where they can write to.
        """
        try:
            dest = Path(dest_path).resolve()
        except OSError as exc:
            raise ValueError(f"export dest_path could not be resolved: {exc}") from exc
        root = self._export_root
        try:
            dest.relative_to(root)
        except ValueError:
            raise ValueError(
                f"export dest_path {str(dest)!r} is outside the configured "
                f"export root {str(root)!r}. Daemon writes are restricted to "
                "the export root to prevent path-traversal."
            ) from None
        return dest

    # ------------------------------------------------------------------
    # exec_raw
    # ------------------------------------------------------------------

    def exec_raw(self, sql: str) -> list[dict[str, Any]]:
        """Execute ``sql`` against the memory DB and return rows as dicts.

        Opens a fresh ``sqlite3.connect("file:<path>?mode=ro", uri=True)``
        connection per call. The read-only constraint is enforced by SQLite's
        URI ``mode=ro`` flag, NOT by SQL pattern matching.

        Audit: emits ``daemon/t2/lifecycle`` with ``op=raw-exec`` and
        ``sql_hash=sha256(sql)[:16]``. The full SQL is never logged.

        Args:
            sql: Arbitrary SQL string. Must be read-only or SQLite will raise
                ``sqlite3.OperationalError`` (attempt to write a readonly db).

        Returns:
            List of dicts keyed by column name.

        Raises:
            sqlite3.OperationalError: If the SQL attempts a write, or on any
                other SQLite error.
        """
        sql_hash = hashlib.sha256(sql.encode()).hexdigest()[:16]

        uri = f"file:{self._memory_db_path}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql)
            # Cap result size to avoid OOM on unbounded SELECTs over large
            # tables. Admin-only op, but a misbehaving admin should not be
            # able to crash the daemon process by querying a 50M-row table.
            rows: list[dict[str, Any]] = []
            for row in cursor:
                if len(rows) >= _EXEC_RAW_MAX_ROWS:
                    raise ValueError(
                        f"exec_raw result exceeded {_EXEC_RAW_MAX_ROWS} rows; "
                        "add a LIMIT clause or use export for full table dumps"
                    )
                rows.append(dict(row))
            # Audit after successful execution, pre-execution logging would
            # record failed/rejected attempts as DoS noise.
            _log.info(
                "daemon/t2/lifecycle",
                op="raw-exec",
                sql_hash=sql_hash,
                row_count=len(rows),
            )
            return rows
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------

    def schema(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return schema information from ``sqlite_master`` for one or both DBs.

        New behaviour (when ``filters`` contains no legacy section keys OR
        contains a ``"db"`` key): returns a two-key result::

            {
                "memory": {"tables": [...], "indexes": [...], "fts": [...]},
                "tuples": {"tables": [...], "indexes": [...], "fts": [...]},
            }

        Pass ``db="memory"`` or ``db="tuples"`` to restrict to a single DB.

        Legacy behaviour (backward-compat): when ``filters`` is a dict that
        contains any of the legacy section-filter keys (``"tables"``,
        ``"indexes"``, ``"fts"``), the call is treated as a memory-only
        request and the result is the flat shape::

            {"tables": [...], "indexes": [...], "fts": [...]}

        .. deprecated::
            Pass a ``db`` key or omit ``filters`` entirely to get both DBs.
            The flat legacy shape is preserved for existing callers (e.g.
            ``nx doctor``) that only know about memory.db.

        Args:
            filters: Optional dict. Recognised keys:
                - ``"db"`` (str): ``"memory"`` or ``"tuples"`` -- restrict to one DB.
                - ``"tables"`` (str | True): legacy -- include table list.
                - ``"indexes"`` (bool): legacy -- include index list.
                - ``"fts"`` (bool): legacy -- include FTS5 virtual tables.
                When ``filters`` is None, all sections for both DBs are returned.

        Returns:
            Two-key dict ``{"memory": ..., "tuples": ...}`` (new shape), or
            flat ``{"tables": ..., "indexes": ..., "fts": ...}`` (legacy shape
            when legacy section-filter keys are detected without a ``"db"`` key).
        """
        f = filters or {}

        # Detect legacy shape: any of the old section-filter keys are present
        # and no new "db" key is present.
        _legacy_keys = {"tables", "indexes", "fts"}
        is_legacy = bool(f.keys() & _legacy_keys) and "db" not in f

        if is_legacy:
            # Preserve the original single-DB flat behaviour for backward compat.
            return self._schema_single(self._memory_db_path, f)

        # New shape: one or both DBs.
        db_filter = f.get("db", None)
        if db_filter == "memory":
            return {"memory": self._schema_single(self._memory_db_path, {})}
        if db_filter == "tuples":
            return {"tuples": self._schema_single(self._tuples_db_path, {})}

        # Default: both DBs.
        return {
            "memory": self._schema_single(self._memory_db_path, {}),
            "tuples": self._schema_single(self._tuples_db_path, {}),
        }

    def _schema_single(
        self, db_path: Path, filters: dict[str, Any]
    ) -> dict[str, Any]:
        """Return schema info for a single SQLite DB at ``db_path``.

        Args:
            db_path: Path to the SQLite database file.
            filters: Section-filter dict (same semantics as legacy ``schema()``
                ``filters`` arg): keys ``"tables"``, ``"indexes"``, ``"fts"``.
                Empty dict means include all sections.

        Returns:
            Flat dict with keys ``"tables"``, ``"indexes"``, ``"fts"``.
        """
        f = filters
        include_tables = "tables" in f or not f
        include_indexes = "indexes" in f or not f
        include_fts = "fts" in f or not f

        table_name_filter: str | None = None
        if "tables" in f and isinstance(f["tables"], str):
            table_name_filter = f["tables"]

        if not db_path.exists():
            # DB file has not been created yet (e.g. tuples.db before first write).
            result: dict[str, Any] = {}
            if include_tables:
                result["tables"] = []
            if include_indexes:
                result["indexes"] = []
            if include_fts:
                result["fts"] = []
            return result

        uri = f"file:{db_path}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            result = {}

            if include_tables:
                if table_name_filter is not None:
                    rows = conn.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name=?",
                        (table_name_filter,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'",
                    ).fetchall()

                tables = []
                for name, ddl in rows:
                    cols = _get_columns(conn, name)
                    tables.append({"name": name, "ddl": ddl, "columns": cols})
                result["tables"] = tables

            if include_indexes:
                idx_rows = conn.execute(
                    "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%'",
                ).fetchall()
                result["indexes"] = [
                    {"name": n, "table": t, "ddl": s} for n, t, s in idx_rows
                ]

            if include_fts:
                fts_rows = conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='table' AND sql LIKE '%USING fts5%'",
                ).fetchall()
                result["fts"] = [{"name": n, "ddl": s} for n, s in fts_rows]

            return result
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # peek
    # ------------------------------------------------------------------

    def peek(
        self,
        table: str,
        offset: int = 0,
        limit: int = _PEEK_MAX,
    ) -> dict[str, Any]:
        """Return a paged slice of ``table``.

        ``limit`` is silently clamped to ``MAX_QUERY_RESULTS`` (300).
        ``limit <= 0`` raises ``ValueError`` (a zero-result silent-success is
        indistinguishable from an empty table and is a usability foot-gun).
        ``offset < 0`` likewise raises.

        Args:
            table: Table name to inspect (must be an existing table).
            offset: Number of rows to skip (default 0). Must be >= 0.
            limit: Maximum rows to return; must be > 0; clamped to 300.

        Returns:
            Dict with keys ``rows`` (list of dicts), ``total`` (int),
            ``offset`` (int), ``limit`` (int, the effective clamped value).
        """
        if limit <= 0:
            raise ValueError(f"peek limit must be positive, got {limit!r}")
        if offset < 0:
            raise ValueError(f"peek offset must be non-negative, got {offset!r}")
        effective_limit = min(limit, _PEEK_MAX)

        uri = f"file:{self._memory_db_path}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row

            total: int = conn.execute(
                f"SELECT COUNT(*) FROM \"{_quote_id(table)}\""  # noqa: S608
            ).fetchone()[0]

            cursor = conn.execute(
                f"SELECT * FROM \"{_quote_id(table)}\" LIMIT ? OFFSET ?",  # noqa: S608
                (effective_limit, offset),
            )
            rows = [dict(row) for row in cursor.fetchall()]

            return {
                "rows": rows,
                "total": total,
                "offset": offset,
                "limit": effective_limit,
            }
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return row counts per table plus DB file sizes.

        ``"tables"`` is now a two-key dict covering both databases::

            {
                "memory": {"<table>": <count>, ...},
                "tuples": {"<table>": <count>, ...},
            }

        The file-size keys (``memory_db_bytes``, ``memory_db_wal_bytes``,
        ``tuples_db_bytes``, ``tuples_db_wal_bytes``) are unchanged.

        Returns:
            Dict with keys:
            - ``"tables"``: ``{"memory": {table: count, ...}, "tuples": {table: count, ...}}``.
            - ``"memory_db_bytes"``: file size of memory_db_path (0 if missing).
            - ``"memory_db_wal_bytes"``: WAL file size (0 if missing).
            - ``"tuples_db_bytes"``: file size of tuples_db_path (0 if missing).
            - ``"tuples_db_wal_bytes"``: WAL file size (0 if missing).
        """
        return {
            "tables": {
                "memory": self._table_counts(self._memory_db_path),
                "tuples": self._table_counts(self._tuples_db_path),
            },
            "memory_db_bytes": _file_size(self._memory_db_path),
            "memory_db_wal_bytes": _file_size(
                self._memory_db_path.parent / (self._memory_db_path.name + "-wal")
            ),
            "tuples_db_bytes": _file_size(self._tuples_db_path),
            "tuples_db_wal_bytes": _file_size(
                self._tuples_db_path.parent / (self._tuples_db_path.name + "-wal")
            ),
        }

    def _table_counts(self, db_path: Path) -> dict[str, int]:
        """Return a mapping of table_name to row count for ``db_path``.

        Returns an empty dict if the database file does not yet exist (e.g.
        tuples.db before the first write).
        """
        if not db_path.exists():
            return {}
        uri = f"file:{db_path}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            table_names: list[str] = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            counts: dict[str, int] = {}
            for tname in table_names:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM \"{_quote_id(tname)}\""  # noqa: S608
                ).fetchone()
                counts[tname] = row[0] if row else 0
            return counts
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------

    def export(
        self,
        table: str | None,
        format: str,
        dest_path: str,
    ) -> dict[str, Any]:
        """Export table(s) to ``dest_path`` in the requested format.

        Streams rows via a generator; never materialises the full result in
        memory. For ``format="sqlite"`` uses the SQLite Backup API
        (``sqlite3.Connection.backup()``).

        Args:
            table: Table name to export, or ``None`` to export all tables.
            format: One of ``"jsonl"``, ``"csv"``, ``"sqlite"``.
            dest_path: Destination file path (daemon-side write). For
                ``format="jsonl"``/``"csv"`` with ``table=None``, this is
                treated as a directory and one file per table is created.

        Returns:
            Dict with keys ``"path"`` (str), ``"bytes_written"`` (int),
            ``"rows"`` (int).

        Raises:
            ValueError: If ``format`` is not one of the supported values.
        """
        if format not in {"jsonl", "csv", "sqlite"}:
            raise ValueError(f"Unsupported export format: {format!r}. Use jsonl, csv, or sqlite.")

        dest = self._validate_export_path(dest_path)

        if format == "sqlite":
            return self._export_sqlite(dest)

        if table is None:
            return self._export_all_tables(dest, format)

        return self._export_single_table(table, dest, format)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _export_sqlite(self, dest: Path) -> dict[str, Any]:
        """Export a full SQLite backup using the Backup API."""
        src_uri = f"file:{self._memory_db_path}?mode=ro"
        src_conn: sqlite3.Connection | None = None
        dst_conn: sqlite3.Connection | None = None
        try:
            src_conn = sqlite3.connect(src_uri, uri=True)
            dst_conn = sqlite3.connect(str(dest))
            src_conn.backup(dst_conn)
            dst_conn.close()
            dst_conn = None

            # Count total rows across all tables for reporting
            verify_conn = sqlite3.connect(str(dest))
            try:
                table_names = [
                    r[0]
                    for r in verify_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                ]
                total = sum(
                    verify_conn.execute(
                        f"SELECT COUNT(*) FROM \"{_quote_id(t)}\""  # noqa: S608
                    ).fetchone()[0]
                    for t in table_names
                )
            finally:
                verify_conn.close()

            return {
                "path": str(dest),
                "bytes_written": dest.stat().st_size,
                "rows": total,
            }
        finally:
            if src_conn is not None:
                src_conn.close()
            if dst_conn is not None:
                dst_conn.close()

    def _export_all_tables(self, dest: Path, format: str) -> dict[str, Any]:
        """Export all tables to a directory, one file per table."""
        dest.mkdir(parents=True, exist_ok=True)

        uri = f"file:{self._memory_db_path}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            table_names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
        finally:
            if conn is not None:
                conn.close()

        total_rows = 0
        total_bytes = 0
        for tname in table_names:
            ext = "jsonl" if format == "jsonl" else "csv"
            tfile = dest / f"{tname}.{ext}"
            result = self._export_single_table(tname, tfile, format)
            total_rows += result["rows"]
            total_bytes += result["bytes_written"]

        return {
            "path": str(dest),
            "bytes_written": total_bytes,
            "rows": total_rows,
        }

    def _export_single_table(
        self, table: str, dest: Path, format: str
    ) -> dict[str, Any]:
        """Export a single table to ``dest`` in the given format."""
        dest.parent.mkdir(parents=True, exist_ok=True)

        rows_written = 0
        bytes_written = 0

        if format == "jsonl":
            with dest.open("w", encoding="utf-8") as out:
                for row in self._stream_table(table):
                    line = json_encode_row(row) + "\n"
                    out.write(line)
                    bytes_written += len(line.encode("utf-8"))
                    rows_written += 1
        elif format == "csv":
            first = True
            writer_obj: csv.DictWriter | None = None
            with dest.open("w", encoding="utf-8", newline="") as out:
                for row in self._stream_table(table):
                    if first:
                        writer_obj = csv.DictWriter(out, fieldnames=list(row.keys()))
                        writer_obj.writeheader()
                        first = False
                    writer_obj.writerow(row)  # type: ignore[union-attr]
                    rows_written += 1
            bytes_written = dest.stat().st_size if dest.exists() else 0
        else:
            raise ValueError(f"Unsupported format: {format!r}")

        return {
            "path": str(dest),
            "bytes_written": bytes_written,
            "rows": rows_written,
        }

    def _stream_table(self, table: str) -> Generator[dict[str, Any], None, None]:
        """Yield rows from ``table`` one at a time (streaming, no full materialisation)."""
        uri = f"file:{self._memory_db_path}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"SELECT * FROM \"{_quote_id(table)}\""  # noqa: S608
            )
            while True:
                rows = cursor.fetchmany(256)
                if not rows:
                    break
                for row in rows:
                    yield dict(row)
        finally:
            if conn is not None:
                conn.close()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _quote_id(name: str) -> str:
    """Escape a SQLite identifier by doubling internal double-quotes."""
    return name.replace('"', '""')


def _file_size(path: Path) -> int:
    """Return file size in bytes, or 0 if the file does not exist."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _get_columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """Return column info for ``table`` via PRAGMA table_info."""
    rows = conn.execute(f"PRAGMA table_info(\"{_quote_id(table)}\")").fetchall()
    return [
        {
            "cid": r[0],
            "name": r[1],
            "type": r[2],
            "notnull": bool(r[3]),
            "default": r[4],
            "pk": bool(r[5]),
        }
        for r in rows
    ]


def json_encode_row(row: dict[str, Any]) -> str:
    """JSON-encode a row dict, handling bytes as base64 and non-serialisable types."""
    import base64
    import json as _json

    def _default(obj: Any) -> Any:
        if isinstance(obj, (bytes, bytearray)):
            return {"__bytes__": base64.b64encode(obj).decode()}
        raise TypeError(f"Not JSON serialisable: {type(obj)!r}")

    return _json.dumps(row, default=_default)
