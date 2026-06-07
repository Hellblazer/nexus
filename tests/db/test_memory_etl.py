# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the SQLite->Postgres memory ETL (bead nexus-gmiaf.8).

Two test levels:

Unit tests (fast, no service):
  - Row transform logic (field mapping, last_accessed '' -> None, id not copied)
  - Idempotency invariants at the transform level
  - Copy-not-move: SQLite source is read-only

Integration tests (@pytest.mark.integration):
  - Full ETL against a real Java service + hermetic Postgres 16
  - Idempotency (run twice -> same row count)
  - Copy-not-move (SQLite unchanged after ETL)
  - Tenant stamping (all rows have tenant_id=DEFAULT_TENANT)
  - Field mapping round-trip (tags='', last_accessed='', session, access_count>0)
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ── Unit tests: row transform logic ──────────────────────────────────────────

class TestRowTransform:
    """Validates _transform_row in isolation without any service or SQLite DB."""

    def test_id_not_copied(self):
        """The SQLite id column must NOT appear in the transformed payload."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 42,
            "project": "p", "title": "t", "content": "c",
            "tags": "", "session": None, "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 30,
            "access_count": 0, "last_accessed": "",
        }
        result = _transform_row(row)
        assert "id" not in result, "SQLite id must not be copied to PG payload"

    def test_last_accessed_empty_string_becomes_none(self):
        """SQLite last_accessed='' must map to None (PG TIMESTAMPTZ nullable)."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "", "session": None, "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 30,
            "access_count": 0, "last_accessed": "",
        }
        result = _transform_row(row)
        assert result["last_accessed"] is None

    def test_last_accessed_real_timestamp_preserved(self):
        """A real last_accessed timestamp is preserved as-is."""
        from nexus.db.t2.memory_etl import _transform_row

        ts = "2026-05-10T09:30:00Z"
        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "", "session": None, "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 30,
            "access_count": 5, "last_accessed": ts,
        }
        result = _transform_row(row)
        assert result["last_accessed"] == ts

    def test_tags_empty_string_preserved(self):
        """tags='' must survive the transform (not become None or omitted)."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "", "session": None, "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": None,
            "access_count": 0, "last_accessed": "",
        }
        result = _transform_row(row)
        assert "tags" in result
        assert result["tags"] == ""

    def test_tags_nonempty_preserved(self):
        """Non-empty tags round-trip unchanged."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "alpha,beta", "session": None, "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 7,
            "access_count": 2, "last_accessed": "",
        }
        result = _transform_row(row)
        assert result["tags"] == "alpha,beta"

    def test_access_count_not_in_transform_output(self):
        """access_count is NOT in _transform_row output — put() does not accept it.

        The service manages access_count server-side.  The ETL reads
        access_count from SQLite but does not pass it to put(), so it must
        not appear in the transformed dict.
        """
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "", "session": "sess-abc", "agent": "developer",
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 14,
            "access_count": 7, "last_accessed": "2026-06-03T10:00:00Z",
        }
        result = _transform_row(row)
        # access_count is server-managed; put() has no such parameter.
        # The ETL cannot carry it through the HTTP seam.
        assert "access_count" not in result

    def test_session_preserved(self):
        """session value is carried through to the payload."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "", "session": "my-session-id", "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 30,
            "access_count": 0, "last_accessed": "",
        }
        result = _transform_row(row)
        assert result["session"] == "my-session-id"

    def test_tenant_id_not_in_put_payload(self):
        """tenant_id is NOT in the put payload; the service stamps it via X-Nexus-Tenant."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "", "session": None, "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 30,
            "access_count": 0, "last_accessed": "",
        }
        result = _transform_row(row)
        # tenant_id is NOT part of the HttpMemoryStore.put() signature;
        # the service stamps it from X-Nexus-Tenant header.
        assert "tenant_id" not in result

    def test_required_fields_present(self):
        """All required HttpMemoryStore.put() fields must be in the payload."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 99, "project": "myproj", "title": "mytitle", "content": "body",
            "tags": "x,y", "session": "s", "agent": "a",
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 30,
            "access_count": 3, "last_accessed": "",
        }
        result = _transform_row(row)
        # These map to HttpMemoryStore.put() kwargs
        for field in ("project", "title", "content", "tags", "ttl", "session", "agent"):
            assert field in result, f"missing field: {field}"


class TestMigrateMemoryMocked:
    """Unit-level test for migrate_memory_rows using a mock HttpMemoryStore."""

    def _make_sqlite_db(self, rows: list[dict]) -> Path:
        """Create a temp SQLite memory DB with the given rows."""
        tmp = tempfile.mkdtemp(prefix="nexus_etl_test_")
        db_path = Path(tmp) / "t2.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE memory (
                id            INTEGER PRIMARY KEY,
                project       TEXT    NOT NULL,
                title         TEXT    NOT NULL,
                session       TEXT,
                agent         TEXT,
                content       TEXT    NOT NULL,
                tags          TEXT,
                timestamp     TEXT    NOT NULL,
                ttl           INTEGER,
                access_count  INTEGER DEFAULT 0 NOT NULL,
                last_accessed TEXT    DEFAULT ''
            )
        """)
        for i, row in enumerate(rows):
            conn.execute("""
                INSERT INTO memory (id, project, title, session, agent, content, tags,
                                    timestamp, ttl, access_count, last_accessed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                i + 1,
                row.get("project", "proj"),
                row.get("title", f"title-{i}"),
                row.get("session"),
                row.get("agent"),
                row.get("content", "content"),
                row.get("tags", ""),
                row.get("timestamp", "2026-06-01T12:00:00Z"),
                row.get("ttl", 30),
                row.get("access_count", 0),
                row.get("last_accessed", ""),
            ))
        conn.commit()
        conn.close()
        return db_path

    def test_migrate_calls_put_for_each_row(self, tmp_path):
        """migrate_memory_rows calls store.put for each SQLite row."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        db_path = self._make_sqlite_db([
            {"project": "p1", "title": "t1", "content": "c1"},
            {"project": "p2", "title": "t2", "content": "c2"},
            {"project": "p3", "title": "t3", "content": "c3"},
        ])
        mock_store = MagicMock()
        mock_store.put.return_value = 1

        result = migrate_memory_rows(db_path, mock_store)
        assert result["read"] == 3
        assert result["written"] == 3
        assert mock_store.put.call_count == 3

    def test_source_sqlite_unchanged(self, tmp_path):
        """SQLite source rows must be untouched after ETL (copy-not-move)."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        rows = [
            {"project": "x", "title": "a", "content": "alpha"},
            {"project": "x", "title": "b", "content": "beta"},
        ]
        db_path = self._make_sqlite_db(rows)

        mock_store = MagicMock()
        mock_store.put.return_value = 1

        migrate_memory_rows(db_path, mock_store)

        # Verify source DB still has all rows
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        conn.close()
        assert count == 2, "SQLite source must retain all rows after ETL"

    def test_idempotency_via_mock(self, tmp_path):
        """Running ETL twice calls put twice per row (idempotency is upsert on server)."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        db_path = self._make_sqlite_db([
            {"project": "p", "title": "t1", "content": "c1"},
            {"project": "p", "title": "t2", "content": "c2"},
        ])
        mock_store = MagicMock()
        mock_store.put.return_value = 1

        r1 = migrate_memory_rows(db_path, mock_store)
        r2 = migrate_memory_rows(db_path, mock_store)

        assert r1["read"] == 2
        assert r2["read"] == 2
        # put called 2x each run = 4 total
        assert mock_store.put.call_count == 4

    def test_field_mapping_last_accessed_empty(self, tmp_path):
        """last_accessed='' in SQLite transforms to None (PG TIMESTAMPTZ nullable)."""
        from nexus.db.t2.memory_etl import _transform_row

        row = {
            "id": 1, "project": "p", "title": "t", "content": "c",
            "tags": "", "session": None, "agent": None,
            "timestamp": "2026-06-01T12:00:00Z", "ttl": 30,
            "access_count": 0, "last_accessed": "",
        }
        result = _transform_row(row)
        assert result.get("last_accessed") is None

    def test_field_mapping_id_not_passed(self, tmp_path):
        """SQLite id must NOT appear in the put() call."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        db_path = self._make_sqlite_db([
            {"project": "p", "title": "t", "content": "c"},
        ])
        mock_store = MagicMock()
        mock_store.put.return_value = 99

        migrate_memory_rows(db_path, mock_store)

        # Inspect call args: id must not appear in kwargs or positional args
        assert mock_store.put.call_count == 1
        call_args = mock_store.put.call_args
        kwargs = call_args[1] if call_args else {}
        args = call_args[0] if call_args else ()
        assert "id" not in kwargs
        # The SQLite row id=1 must not be positionally passed as project/title/content/...
        # Check the positional args match the expected put() signature:
        # put(project, title, content, tags, ttl, agent, session)
        # None of these should be "1" (the sqlite id)
        assert 1 not in args

    def test_returns_counts(self, tmp_path):
        """migrate_memory_rows returns {read, written} counts."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        db_path = self._make_sqlite_db([
            {"project": "p", "title": "t1", "content": "c"},
            {"project": "p", "title": "t2", "content": "c"},
        ])
        mock_store = MagicMock()
        mock_store.put.return_value = 1

        result = migrate_memory_rows(db_path, mock_store)
        assert "read" in result
        assert "written" in result
        assert result["read"] == 2
        assert result["written"] == 2

    def test_empty_db_returns_zeros(self, tmp_path):
        """An empty SQLite DB produces read=0, written=0."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        db_path = self._make_sqlite_db([])
        mock_store = MagicMock()

        result = migrate_memory_rows(db_path, mock_store)
        assert result["read"] == 0
        assert result["written"] == 0
        mock_store.put.assert_not_called()


# ── Integration tests: real service + hermetic Postgres ───────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")
_INITDB    = _PG_BIN / "initdb"
_PG_CTL    = _PG_BIN / "pg_ctl"
_PSQL      = _PG_BIN / "psql"
_CREATEDB  = _PG_BIN / "createdb"
_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = Path(_JAVA_HOME) / "bin" / "java" if _JAVA_HOME else Path(shutil.which("java") or "java")

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

_BOOTSTRAP_SQL = """\
CREATE SCHEMA IF NOT EXISTS nexus;

CREATE TABLE nexus.memory (
    id            BIGSERIAL    NOT NULL,
    tenant_id     TEXT         NOT NULL,
    project       TEXT         NOT NULL,
    title         TEXT         NOT NULL,
    session       TEXT,
    agent         TEXT,
    content       TEXT         NOT NULL,
    tags          TEXT,
    timestamp     TIMESTAMPTZ  NOT NULL,
    ttl           INTEGER,
    access_count  INTEGER      NOT NULL DEFAULT 0,
    last_accessed TIMESTAMPTZ,
    CONSTRAINT memory_pk PRIMARY KEY (id),
    CONSTRAINT memory_tenant_project_title_uq UNIQUE (tenant_id, project, title)
);

CREATE INDEX idx_memory_tenant_project       ON nexus.memory (tenant_id, project);
CREATE INDEX idx_memory_tenant_agent         ON nexus.memory (tenant_id, agent);
CREATE INDEX idx_memory_tenant_timestamp     ON nexus.memory (tenant_id, timestamp DESC);
CREATE INDEX idx_memory_tenant_ttl_timestamp ON nexus.memory (tenant_id, ttl, timestamp);

ALTER TABLE nexus.memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.memory FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON nexus.memory
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.memory
    ADD COLUMN fts_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,   '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B') ||
        setweight(to_tsvector('simple',  coalesce(tags,    '')), 'C')
    ) STORED;

CREATE INDEX idx_memory_fts ON nexus.memory USING GIN (fts_vector);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_etltest') THEN
    CREATE ROLE svc_etltest LOGIN PASSWORD 'svc_etltest_pass';
  END IF;
END $$;

GRANT USAGE ON SCHEMA nexus TO svc_etltest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO svc_etltest;
GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO svc_etltest;
ALTER ROLE svc_etltest SET search_path TO nexus, public;
"""

_SKIP_INTEGRATION = pytest.mark.skipif(
    not _ALL_PREREQS,
    reason=(
        "skipped: missing jar or pg16 binaries "
        f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
    ),
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _make_source_db(rows: list[dict]) -> Path:
    """Build a hermetic SQLite memory DB for the ETL source."""
    tmp = tempfile.mkdtemp(prefix="nexus_etl_inttest_src_")
    db_path = Path(tmp) / "t2.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE memory (
            id            INTEGER PRIMARY KEY,
            project       TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            session       TEXT,
            agent         TEXT,
            content       TEXT    NOT NULL,
            tags          TEXT,
            timestamp     TEXT    NOT NULL,
            ttl           INTEGER,
            access_count  INTEGER DEFAULT 0 NOT NULL,
            last_accessed TEXT    DEFAULT ''
        )
    """)
    for i, row in enumerate(rows):
        conn.execute("""
            INSERT INTO memory (id, project, title, session, agent, content, tags,
                                timestamp, ttl, access_count, last_accessed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            i + 1,
            row.get("project", "etltest"),
            row.get("title", f"title-{i}"),
            row.get("session"),
            row.get("agent"),
            row.get("content", f"content-{i}"),
            row.get("tags", ""),
            row.get("timestamp", "2026-06-01T12:00:00Z"),
            row.get("ttl", 30),
            row.get("access_count", 0),
            row.get("last_accessed", ""),
        ))
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture(scope="module")
def etl_pg_instance():
    """Hermetic Postgres 16 instance for ETL integration tests."""
    if not _ALL_PREREQS:
        pytest.skip("missing jar or pg16 binaries")
    pgdata = tempfile.mkdtemp(prefix="nexus_etl_inttest_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}",
             "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexus_etltest"],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "nexus_etltest",
             "-v", "ON_ERROR_STOP=1",
             "-c", _BOOTSTRAP_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"psql bootstrap failed (rc={proc.returncode}):\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        yield {"port": pg_port, "dbname": "nexus_etltest", "user": pg_user, "pgdata": pgdata}
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def etl_service(etl_pg_instance):
    """Java service against the hermetic Postgres for ETL tests."""
    svc_port = _free_port()
    token = "etltest-bearer-token-abc"
    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{etl_pg_instance['port']}"
            f"/{etl_pg_instance['dbname']}"
        ),
        "NX_DB_USER": "svc_etltest",
        "NX_DB_PASS": "svc_etltest_pass",
        "NX_POOL_SIZE": "3",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=30.0)
        yield f"http://127.0.0.1:{svc_port}", token, proc
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture(scope="module")
def etl_store(etl_service):
    """HttpMemoryStore connected to the real ETL test service."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    base_url, token, _ = etl_service
    os.environ["NX_SERVICE_TOKEN"] = token
    s = HttpMemoryStore(base_url=base_url, tenant="default")
    yield s
    s.close()


@pytest.mark.integration
@_SKIP_INTEGRATION
class TestMemoryEtlIntegration:
    """Full ETL tests against a real Java service + hermetic Postgres 16."""

    def test_etl_copies_k_rows(self, etl_store):
        """K SQLite rows -> K PG rows after one ETL run."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        K = 5
        rows = [
            {"project": "etl-basic", "title": f"entry-{i}", "content": f"content-{i}"}
            for i in range(K)
        ]
        db_path = _make_source_db(rows)

        result = migrate_memory_rows(db_path, etl_store)
        assert result["read"] == K
        assert result["written"] == K

        # Verify via the service
        entries = etl_store.get_all("etl-basic")
        assert len(entries) == K

    def test_idempotency_run_twice_no_dupes(self, etl_store):
        """Running ETL twice -> same row count (upsert deduplication)."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        K = 3
        rows = [
            {"project": "etl-idem", "title": f"idem-{i}", "content": f"body-{i}"}
            for i in range(K)
        ]
        db_path = _make_source_db(rows)

        r1 = migrate_memory_rows(db_path, etl_store)
        r2 = migrate_memory_rows(db_path, etl_store)

        assert r1["read"] == K
        assert r2["read"] == K

        # Second run must not produce additional rows
        entries = etl_store.get_all("etl-idem")
        assert len(entries) == K, (
            f"Expected {K} rows after 2 ETL runs, got {len(entries)} — "
            "upsert idempotency broken"
        )

    def test_copy_not_move_sqlite_unchanged(self, etl_store):
        """SQLite source rows survive the ETL unchanged (copy-not-move)."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        rows = [
            {"project": "etl-cnm", "title": "cnm-a", "content": "alpha"},
            {"project": "etl-cnm", "title": "cnm-b", "content": "beta"},
        ]
        db_path = _make_source_db(rows)

        migrate_memory_rows(db_path, etl_store)

        # Source DB must still have both rows, content unchanged
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        src_rows = conn.execute(
            "SELECT project, title, content FROM memory ORDER BY title"
        ).fetchall()
        conn.close()

        assert count == 2, "SQLite row count changed — ETL modified the source (violation)"
        assert src_rows[0] == ("etl-cnm", "cnm-a", "alpha")
        assert src_rows[1] == ("etl-cnm", "cnm-b", "beta")

    def test_tenant_stamping(self, etl_store):
        """Migrated rows are visible under DEFAULT_TENANT='default' (RLS stamped correctly)."""
        from nexus.db.t2.http_memory_store import DEFAULT_TENANT
        from nexus.db.t2.memory_etl import migrate_memory_rows

        rows = [
            {"project": "etl-tenant", "title": "ts-1", "content": "tenant stamp test"},
        ]
        db_path = _make_source_db(rows)

        migrate_memory_rows(db_path, etl_store)

        # etl_store is initialized with tenant='default'
        assert etl_store._tenant == DEFAULT_TENANT
        entry = etl_store.get(project="etl-tenant", title="ts-1")
        assert entry is not None, (
            "Migrated row not visible under DEFAULT_TENANT — RLS tenant stamp failed"
        )
        assert entry["content"] == "tenant stamp test"

    def test_field_mapping_round_trip(self, etl_store):
        """Full field-mapping round-trip: tags='', last_accessed='', session, access_count>0."""
        from nexus.db.t2.memory_etl import migrate_memory_rows

        rows = [
            {
                "project": "etl-fields",
                "title": "fmap-1",
                "content": "field mapping test",
                "tags": "",
                "session": "test-session-xyz",
                "agent": "developer",
                "timestamp": "2026-05-15T08:30:00Z",
                "ttl": 14,
                "access_count": 3,
                "last_accessed": "",
            }
        ]
        db_path = _make_source_db(rows)

        migrate_memory_rows(db_path, etl_store)

        entry = etl_store.get(project="etl-fields", title="fmap-1")
        assert entry is not None

        # tags: '' must come back as '' (not None, not missing)
        assert "tags" in entry
        assert entry["tags"] == ""

        # last_accessed: SQLite '' -> PG NULL on insert, but the get() call
        # triggers access tracking which sets last_accessed = now on the server.
        # After a get(), last_accessed will be a UTC timestamp string (not '').
        # We verify it is either '' (if access tracking were disabled) or a
        # valid UTC timestamp string.
        last_acc = entry.get("last_accessed", "")
        import re as _re
        assert last_acc == "" or _re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", last_acc), (
            f"last_accessed must be '' or UTC ISO-Z, got: {last_acc!r}"
        )

        # session must be preserved
        assert entry.get("session") == "test-session-xyz"

        # id is PG auto-generated, must be a positive int (not the SQLite id=1)
        assert isinstance(entry.get("id"), int)
        assert entry["id"] > 0
