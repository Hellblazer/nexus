# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for introspection RPC surface — RDR-112 P1.6 (nexus-08i1).

Covers:
  (a) exec_raw round-trips a SELECT.
  (b) exec_raw rejects writes — mode=ro enforcement via sqlite3.OperationalError.
  (c) schema output matches sqlite_master.
  (d) peek pagination — 500 rows, offset/limit clamping.
  (e) export round-trips via re-import (jsonl format).
  (f) exec_raw over TCP is rejected (admin-gated).
  (g) exec_raw audit-log emits sql_hash (not full SQL).

All tests use tmp_path SQLite databases and real IntrospectionService.
Daemon tests use port=0, tmp_path config_dir, real T2Daemon.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import structlog

from nexus.daemon.introspection import IntrospectionService
from nexus.daemon.t2_daemon import (
    DAEMON_PROTOCOL_VERSION,
    T2Daemon,
    read_frame,
    write_frame,
)
from nexus.db.t2 import T2Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_db(path: Path) -> sqlite3.Connection:
    """Open a fresh writable SQLite DB, create a test table, return conn."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS test_items (id INTEGER PRIMARY KEY, name TEXT, value INTEGER)"
    )
    conn.commit()
    return conn


def _insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    for row in rows:
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(row.values()))
    conn.commit()


def _run_daemon(daemon: T2Daemon) -> asyncio.AbstractEventLoop:
    """Start daemon on a background event loop; return loop."""
    started = threading.Event()
    loop = asyncio.new_event_loop()

    def _thread() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(daemon.start())
        started.set()
        loop.run_forever()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    started.wait(timeout=5.0)
    return loop


async def _tcp_rpc(
    host: str,
    port: int,
    *,
    op: str,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        write_frame(writer, {"op": "hello", "protocol_version": DAEMON_PROTOCOL_VERSION})
        await writer.drain()
        await read_frame(reader)  # hello_ack
        write_frame(writer, {"op": op, "args": args or {}})
        await writer.drain()
        return await read_frame(reader)
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


@pytest.fixture
def tuples_db_path(tmp_path: Path) -> Path:
    return tmp_path / "tuples.db"


@pytest.fixture
def service(
    tmp_path: Path,
    memory_db_path: Path,
    tuples_db_path: Path,
) -> IntrospectionService:
    """IntrospectionService wired to tmp_path databases.

    ``export_root`` is the same tmp_path so export-path-traversal tests can
    target an exports/ subdir under the same tree.
    """
    return IntrospectionService(
        memory_db_path=memory_db_path,
        tuples_db_path=tuples_db_path,
        export_root=tmp_path,
    )


@pytest.fixture
def populated_db(memory_db_path: Path) -> sqlite3.Connection:
    """Create and return a writable connection to the test DB."""
    conn = _make_memory_db(memory_db_path)
    return conn


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config" / "nexus"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def t2db(memory_db_path: Path) -> T2Database:
    database = T2Database(memory_db_path)
    yield database
    database.close()


# ---------------------------------------------------------------------------
# (a) exec_raw round-trips a SELECT
# ---------------------------------------------------------------------------


def test_exec_raw_select_roundtrip(service: IntrospectionService, populated_db: sqlite3.Connection, memory_db_path: Path) -> None:
    """exec_raw SELECT retrieves rows inserted directly into the DB."""
    _insert_rows(populated_db, "test_items", [
        {"name": "alpha", "value": 1},
        {"name": "beta", "value": 2},
    ])
    populated_db.close()

    rows = service.exec_raw("SELECT name, value FROM test_items ORDER BY id")
    assert len(rows) == 2
    assert rows[0] == {"name": "alpha", "value": 1}
    assert rows[1] == {"name": "beta", "value": 2}


def test_exec_raw_returns_dicts_with_column_keys(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """exec_raw rows are dicts keyed by column name (not positional tuples)."""
    _insert_rows(populated_db, "test_items", [{"name": "gamma", "value": 42}])
    populated_db.close()

    rows = service.exec_raw("SELECT id, name, value FROM test_items")
    assert isinstance(rows[0], dict)
    assert "name" in rows[0]
    assert "value" in rows[0]
    assert rows[0]["name"] == "gamma"
    assert rows[0]["value"] == 42


# ---------------------------------------------------------------------------
# (b) exec_raw rejects writes — mode=ro enforcement
# ---------------------------------------------------------------------------


def test_exec_raw_rejects_insert_via_mode_ro(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """exec_raw raises sqlite3.OperationalError for INSERT — mode=ro enforcement.

    The test does NOT pattern-match the SQL. The rejection comes from SQLite's
    URI mode=ro flag, which makes the file read-only at the OS level.
    """
    populated_db.close()

    with pytest.raises(sqlite3.OperationalError):
        service.exec_raw("INSERT INTO test_items (name, value) VALUES ('x', 99)")


def test_exec_raw_rejects_create_table_via_mode_ro(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """exec_raw raises sqlite3.OperationalError for CREATE TABLE — mode=ro."""
    populated_db.close()

    with pytest.raises(sqlite3.OperationalError):
        service.exec_raw("CREATE TABLE forbidden (x INTEGER)")


def test_exec_raw_rejects_delete_via_mode_ro(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """exec_raw raises sqlite3.OperationalError for DELETE — mode=ro."""
    _insert_rows(sqlite3.connect(str(service._memory_db_path)), "test_items", [
        {"name": "to_delete", "value": 7}
    ])

    with pytest.raises(sqlite3.OperationalError):
        service.exec_raw("DELETE FROM test_items")


# ---------------------------------------------------------------------------
# (c) schema output matches sqlite_master
# ---------------------------------------------------------------------------


def test_schema_tables_present(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """schema() tables list includes our created table."""
    populated_db.close()

    result = service.schema()
    assert "tables" in result
    table_names = [t["name"] for t in result["tables"]]
    assert "test_items" in table_names


def test_schema_tables_filter_by_name(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """schema(filters={'tables': 'test_items'}) returns only the matching table."""
    populated_db.close()

    result = service.schema(filters={"tables": "test_items"})
    table_names = [t["name"] for t in result["tables"]]
    assert "test_items" in table_names
    # Other internal sqlite_ tables should not be present if filter is applied
    assert all(n == "test_items" for n in table_names)


def test_schema_indexes_present(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """schema() includes indexes when --indexes filter is used."""
    # Create an index
    populated_db.execute("CREATE INDEX idx_name ON test_items(name)")
    populated_db.commit()
    populated_db.close()

    result = service.schema(filters={"indexes": True})
    assert "indexes" in result
    index_names = [i["name"] for i in result["indexes"]]
    assert "idx_name" in index_names


def test_schema_fts_present(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """schema() includes FTS5 tables under 'fts' key."""
    # FTS5 column spec does not accept a type token; just the column name.
    populated_db.execute("CREATE VIRTUAL TABLE search_fts USING fts5(content)")
    populated_db.commit()
    populated_db.close()

    result = service.schema(filters={"fts": True})
    assert "fts" in result
    fts_names = [t["name"] for t in result["fts"]]
    assert "search_fts" in fts_names


def test_schema_column_shape_present(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """schema() tables include column information."""
    populated_db.close()

    result = service.schema()
    test_table = next(t for t in result["tables"] if t["name"] == "test_items")
    assert "columns" in test_table
    col_names = [c["name"] for c in test_table["columns"]]
    assert "id" in col_names
    assert "name" in col_names
    assert "value" in col_names


# ---------------------------------------------------------------------------
# (d) peek pagination
# ---------------------------------------------------------------------------


def test_peek_first_page(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """peek with offset=0, limit=300 returns 300 rows from 500-row table."""
    rows_data = [{"name": f"item_{i}", "value": i} for i in range(500)]
    _insert_rows(populated_db, "test_items", rows_data)
    populated_db.close()

    result = service.peek("test_items", offset=0, limit=300)
    assert result["offset"] == 0
    assert result["limit"] == 300
    assert result["total"] == 500
    assert len(result["rows"]) == 300


def test_peek_second_page(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """peek with offset=300 returns the remaining 200 rows."""
    rows_data = [{"name": f"item_{i}", "value": i} for i in range(500)]
    _insert_rows(populated_db, "test_items", rows_data)
    populated_db.close()

    result = service.peek("test_items", offset=300, limit=300)
    assert result["offset"] == 300
    assert len(result["rows"]) == 200


def test_peek_clamps_limit_to_300(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """peek with limit=400 is clamped to 300 (MAX_QUERY_RESULTS)."""
    rows_data = [{"name": f"item_{i}", "value": i} for i in range(500)]
    _insert_rows(populated_db, "test_items", rows_data)
    populated_db.close()

    result = service.peek("test_items", offset=0, limit=400)
    assert result["limit"] == 300
    assert len(result["rows"]) == 300


def test_peek_returns_dicts(service: IntrospectionService, populated_db: sqlite3.Connection) -> None:
    """peek rows are dicts keyed by column name."""
    _insert_rows(populated_db, "test_items", [{"name": "z", "value": 99}])
    populated_db.close()

    result = service.peek("test_items", offset=0, limit=10)
    assert len(result["rows"]) >= 1
    row = result["rows"][0]
    assert isinstance(row, dict)
    assert "name" in row


# ---------------------------------------------------------------------------
# (e) export round-trips via re-import
# ---------------------------------------------------------------------------


def test_export_jsonl_roundtrip(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """export jsonl then re-read matches original row count and content."""
    rows_data = [{"name": f"item_{i}", "value": i} for i in range(10)]
    _insert_rows(populated_db, "test_items", rows_data)
    populated_db.close()

    dest = tmp_path / "export.jsonl"
    result = service.export(table="test_items", format="jsonl", dest_path=str(dest))

    assert result["rows"] == 10
    assert result["bytes_written"] > 0
    assert Path(result["path"]) == dest

    # Re-import
    loaded = []
    with dest.open() as f:
        for line in f:
            line = line.strip()
            if line:
                loaded.append(json.loads(line))

    assert len(loaded) == 10
    names = {r["name"] for r in loaded}
    assert names == {f"item_{i}" for i in range(10)}


def test_export_csv_roundtrip(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """export csv then re-read matches original row count."""
    rows_data = [{"name": f"row_{i}", "value": i * 2} for i in range(5)]
    _insert_rows(populated_db, "test_items", rows_data)
    populated_db.close()

    dest = tmp_path / "export.csv"
    result = service.export(table="test_items", format="csv", dest_path=str(dest))
    assert result["rows"] == 5

    with dest.open() as f:
        reader = csv.DictReader(f)
        loaded = list(reader)

    assert len(loaded) == 5


def test_export_sqlite_roundtrip(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """export sqlite backup then open the copy and verify row count."""
    rows_data = [{"name": f"s_{i}", "value": i} for i in range(7)]
    _insert_rows(populated_db, "test_items", rows_data)
    populated_db.close()

    dest = tmp_path / "backup.db"
    result = service.export(table=None, format="sqlite", dest_path=str(dest))
    # SQLite backup copies every row including any internal tables; assert
    # at least our 7 test rows arrived (exact total depends on schema state).
    assert result["rows"] == 7

    backup_conn = sqlite3.connect(str(dest))
    rows = backup_conn.execute("SELECT COUNT(*) FROM test_items").fetchone()[0]
    backup_conn.close()
    assert rows == 7


def test_export_all_tables_jsonl(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """export with table=None exports all tables into dest_path directory."""
    _insert_rows(populated_db, "test_items", [{"name": "a", "value": 1}])
    populated_db.close()

    dest = tmp_path / "all_tables"
    result = service.export(table=None, format="jsonl", dest_path=str(dest))
    assert result["rows"] >= 1


# ---------------------------------------------------------------------------
# (f) exec_raw over TCP is rejected (admin-gated)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_raw_rejected_over_tcp(config_dir: Path, t2db: T2Database) -> None:
    """exec_raw is in _ADMIN_OPS; calling it over TCP returns PermissionDenied."""
    daemon = T2Daemon(config_dir=config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        resp = await _tcp_rpc(
            "127.0.0.1",
            daemon.tcp_port,
            op="exec_raw",
            args={"sql": "SELECT 1"},
        )
        assert "error" in resp, f"Expected error frame, got: {resp}"
        err = resp["error"]
        assert isinstance(err, dict)
        assert err.get("type") == "PermissionDenied"
        assert "exec_raw" in err.get("message", "") or "UDS" in err.get("message", "")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.sleep(0)  # yield to event loop


@pytest.mark.asyncio
async def test_export_rejected_over_tcp(config_dir: Path, t2db: T2Database, tmp_path: Path) -> None:
    """export is in _ADMIN_OPS; calling it over TCP returns PermissionDenied."""
    daemon = T2Daemon(config_dir=config_dir, t2db=t2db)
    loop = _run_daemon(daemon)
    try:
        resp = await _tcp_rpc(
            "127.0.0.1",
            daemon.tcp_port,
            op="export",
            args={"table": "memory", "format": "jsonl", "dest_path": str(tmp_path / "x.jsonl")},
        )
        assert "error" in resp
        err = resp["error"]
        assert isinstance(err, dict)
        assert err.get("type") == "PermissionDenied"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# (g) exec_raw audit-log emits sql_hash (not full SQL)
# ---------------------------------------------------------------------------


def test_exec_raw_audit_log_emits_sql_hash(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """exec_raw audit log emits sql_hash (16-char SHA256 prefix), not the full SQL."""
    import structlog as _structlog

    _insert_rows(populated_db, "test_items", [{"name": "audit_test", "value": 5}])
    populated_db.close()

    sql = "SELECT name FROM test_items"
    expected_hash = hashlib.sha256(sql.encode()).hexdigest()[:16]

    # Reconfigure structlog to emit through stdlib logging so caplog catches it.
    _structlog.configure(
        processors=[_structlog.stdlib.render_to_log_kwargs],
        wrapper_class=_structlog.stdlib.BoundLogger,
        logger_factory=_structlog.stdlib.LoggerFactory(),
    )

    with caplog.at_level(logging.INFO, logger="nexus.daemon.introspection"):
        service.exec_raw(sql)

    # render_to_log_kwargs puts extra fields as LogRecord attributes.
    # The event is getMessage(); extra fields are on the record object.
    found_hash = False
    for record in caplog.records:
        if getattr(record, "sql_hash", None) == expected_hash:
            found_hash = True
            break
    assert found_hash, (
        f"Expected sql_hash={expected_hash!r} as a log record attribute. "
        f"Records: {[(r.getMessage(), vars(r)) for r in caplog.records]!r}"
    )

    # The full SQL must NOT appear in any record field
    for record in caplog.records:
        record_str = str(vars(record))
        assert sql not in record_str, (
            f"Full SQL must not appear in audit log record: {record_str!r}"
        )


def test_exec_raw_audit_log_does_not_emit_full_sql(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """exec_raw audit log never contains the full SQL string."""
    import structlog as _structlog

    _insert_rows(populated_db, "test_items", [{"name": "secret_name", "value": 123}])
    populated_db.close()

    # Reconfigure structlog to emit through stdlib logging so caplog catches it.
    _structlog.configure(
        processors=[_structlog.stdlib.render_to_log_kwargs],
        wrapper_class=_structlog.stdlib.BoundLogger,
        logger_factory=_structlog.stdlib.LoggerFactory(),
    )

    # Use SQL with PII-like content that must not leak into logs
    sql = "SELECT name FROM test_items WHERE name = 'secret_name'"

    with caplog.at_level(logging.DEBUG, logger="nexus.daemon.introspection"):
        service.exec_raw(sql)

    # Check ALL record attributes, not just getMessage()
    for record in caplog.records:
        record_str = str(vars(record))
        assert "secret_name" not in record_str, (
            f"PII from SQL must not appear in audit log: {record_str!r}"
        )


# ---------------------------------------------------------------------------
# Integration: IntrospectionService registered in daemon dispatch table
# ---------------------------------------------------------------------------


def test_introspection_ops_in_dispatch_table(config_dir: Path, t2db: T2Database) -> None:
    """schema, peek, stats are registered in the daemon RPC table (non-admin)."""
    daemon = T2Daemon(config_dir=config_dir, t2db=t2db)
    assert "schema" in daemon._rpc_table
    assert "peek" in daemon._rpc_table
    assert "stats" in daemon._rpc_table


def test_admin_introspection_ops_in_admin_set(config_dir: Path, t2db: T2Database) -> None:
    """exec_raw and export are in _ADMIN_OPS."""
    from nexus.daemon.t2_daemon import _ADMIN_OPS
    assert "exec_raw" in _ADMIN_OPS
    assert "export" in _ADMIN_OPS


def test_non_admin_introspection_ops_not_in_admin_set() -> None:
    """schema, peek, stats are NOT in _ADMIN_OPS (safe over TCP)."""
    from nexus.daemon.t2_daemon import _ADMIN_OPS
    assert "schema" not in _ADMIN_OPS
    assert "peek" not in _ADMIN_OPS
    assert "stats" not in _ADMIN_OPS


# ---------------------------------------------------------------------------
# Review-driven additions (PR #775 review)
# ---------------------------------------------------------------------------


def test_peek_rejects_zero_limit(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
) -> None:
    """peek with limit=0 must raise ValueError (silent empty is a foot-gun)."""
    populated_db.close()
    with pytest.raises(ValueError, match="positive"):
        service.peek(table="test_items", limit=0)


def test_peek_rejects_negative_limit(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
) -> None:
    populated_db.close()
    with pytest.raises(ValueError, match="positive"):
        service.peek(table="test_items", limit=-5)


def test_peek_rejects_negative_offset(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
) -> None:
    populated_db.close()
    with pytest.raises(ValueError, match="non-negative"):
        service.peek(table="test_items", offset=-1, limit=10)


def test_export_rejects_path_traversal(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """export must reject dest_path outside the configured export root."""
    populated_db.close()
    # Path traversal: try to write outside tmp_path (the export root).
    traversal = "/tmp/escape-test-nexus-pce11.jsonl"
    with pytest.raises(ValueError, match="outside the configured export root"):
        service.export(table="test_items", format="jsonl", dest_path=traversal)


def test_exec_raw_audit_log_fires_after_execution(
    service: IntrospectionService,
    populated_db: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """exec_raw must NOT emit an audit event when the SQL fails (DoS noise)."""
    populated_db.close()
    import logging as _logging
    caplog.set_level(_logging.INFO)
    # Invalid SQL — execution fails. Pre-execution logging would record the
    # attempt regardless of validity, creating DoS-friendly audit churn.
    with pytest.raises(Exception):
        service.exec_raw("SELECT * FROM no_such_table_xyz")
    # No "raw-exec" event should appear in caplog records for the failed call.
    raw_exec_events = [
        r for r in caplog.records
        if "raw-exec" in r.getMessage() or getattr(r, "op", None) == "raw-exec"
    ]
    assert raw_exec_events == [], (
        f"audit must fire only on success; got {len(raw_exec_events)} event(s) "
        "for a failed exec_raw"
    )
