# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language integration test for HttpMemoryStore against the real Java service.

Requires:
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} on PATH
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (mvn -f service/pom.xml package -DskipTests)
  - NX_STORAGE_BACKEND env unset (default SQLite path must stay UNCHANGED)

Marked @pytest.mark.integration — skipped when the jar or pg binaries are absent.

Run with:
    uv run pytest -m integration tests/db/test_http_memory_store_integration.py -v

What is exercised (per critic issue #7):
  a) Real FTS: put → search using a stem form ("running" finds "run"), NOT a fake server
  b) Tags round-trip: untagged entry has tags=""
  c) Timestamp format: UTC second-precision Z
  d) Cross-tenant RLS negative: tenant A cannot see tenant B's rows
  e) put_or_merge server-side: two puts with overlapping content → merge on second call
  f) Access count: get() twice → access_count increases
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# ── Prerequisites ─────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent.parent
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN = Path("/opt/homebrew/opt/postgresql@16/bin")

_INITDB  = _PG_BIN / "initdb"
_PG_CTL  = _PG_BIN / "pg_ctl"
_PSQL    = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ALL_PREREQS, reason="jar or pg binaries absent"),
]

# ── Schema DDL (extracted from memory-001-baseline.xml) ──────────────────────

_BOOTSTRAP_SQL = """
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

-- Service role: svc_inttest (hermetic — not nexus_svc)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_inttest') THEN
    CREATE ROLE svc_inttest LOGIN PASSWORD 'svc_inttest_pass';
  END IF;
END $$;

GRANT USAGE ON SCHEMA nexus TO svc_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO svc_inttest;
GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO svc_inttest;
ALTER ROLE svc_inttest SET search_path TO nexus, public;
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} on {host} not ready after {timeout}s")


# ── Session-scoped fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Spin up a hermetic initdb/pg_ctl Postgres instance.  Torn down after module."""
    pgdata = tempfile.mkdtemp(prefix="nexus_inttest_pg_")
    pglog  = os.path.join(pgdata, "pg.log")
    pg_port = _free_port()

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8",
             "--auth-local=trust", "--auth-host=md5"],
            check=True, capture_output=True,
        )
        # Override port
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog, "start"],
            check=True, capture_output=True,
        )
        # Wait for pg to be ready
        _wait_for_port("127.0.0.1", pg_port, timeout=20.0)

        # Create test database
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", os.environ.get("USER", "postgres"), "nexustest"],
            check=True, capture_output=True,
        )

        # Bootstrap schema
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", os.environ.get("USER", "postgres"), "-d", "nexustest",
             "-c", _BOOTSTRAP_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"psql bootstrap failed:\n{proc.stderr}")

        yield {"port": pg_port, "dbname": "nexustest"}
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Launch the shaded JAR against the hermetic PG.  Returns (base_url, token)."""
    svc_port = _free_port()
    token = "inttest-token-secret-xyz"
    pg_user = os.environ.get("USER", "postgres")

    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "svc_inttest",
        "NX_DB_PASS": "svc_inttest_pass",
        "NX_POOL_SIZE": "3",
    }

    proc = subprocess.Popen(
        ["java", "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_for_port("127.0.0.1", svc_port, timeout=30.0)
        yield f"http://127.0.0.1:{svc_port}", token, proc
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


@pytest.fixture(scope="module")
def store(service):
    """HttpMemoryStore connected to the real Java service."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    base_url, token, _ = service
    return HttpMemoryStore(base_url=base_url, token=token, tenant="default")


@pytest.fixture(scope="module")
def other_store(service):
    """HttpMemoryStore for cross-tenant RLS test."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    base_url, token, _ = service
    return HttpMemoryStore(base_url=base_url, token=token, tenant="other-tenant")


# ── Integration Tests ─────────────────────────────────────────────────────────

class TestRealService:
    """Exercises HttpMemoryStore against the real Java service + real PG."""

    def test_put_get_round_trip(self, store):
        """Basic put → get round-trip through the real service."""
        row_id = store.put("inttest", "rt-1", "round trip content", ttl=1)
        assert isinstance(row_id, int) and row_id > 0

        entry = store.get(project="inttest", title="rt-1")
        assert entry is not None
        assert entry["content"] == "round trip content"
        assert entry["project"] == "inttest"
        assert entry["title"] == "rt-1"

    def test_tags_always_string_not_null(self, store):
        """Critical #2: untagged entry must have tags='' not None/missing."""
        store.put("inttest", "tags-none", "no tags here", ttl=1)
        entry = store.get(project="inttest", title="tags-none")
        assert entry is not None
        assert "tags" in entry, "tags key must always be present"
        assert entry["tags"] == "", f"expected tags='', got {entry['tags']!r}"

    def test_timestamp_format_utc_second_precision(self, store):
        """Significant #4: timestamp must be UTC second-precision ISO-Z format."""
        store.put("inttest", "ts-fmt", "timestamp format check", ttl=1)
        entry = store.get(project="inttest", title="ts-fmt")
        assert entry is not None
        ts = entry["timestamp"]
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts), (
            f"timestamp must match yyyy-MM-dd'T'HH:mm:ss'Z', got: {ts!r}"
        )

    def test_access_count_increments_on_get(self, store):
        """Significant #5: access_count increments on every GET call."""
        store.put("inttest", "ac-test", "access count test", ttl=1)
        e1 = store.get(project="inttest", title="ac-test")
        e2 = store.get(project="inttest", title="ac-test")
        assert e1 is not None and e2 is not None
        assert e2["access_count"] == e1["access_count"] + 1, (
            f"expected access_count to increment: {e1['access_count']} → {e2['access_count']}"
        )

    def test_fts_exact_match(self, store):
        """FTS returns a result for an exact content word match."""
        store.put("inttest", "fts-exact", "chromadb semantic search engine", ttl=1)
        results = store.search("chromadb", project="inttest")
        titles = [r["title"] for r in results]
        assert "fts-exact" in titles, f"FTS exact match failed, got titles: {titles}"

    def test_fts_stemming_probe(self, store):
        """Real FTS: 'running' must find content containing 'run' (english stemmer).

        This is the stemming probe that the fake server CANNOT reproduce
        (fake FTS is substring-only).
        """
        store.put("inttest", "fts-stem", "the daemon ran its event loop", ttl=1)
        # 'run' stems to 'run'; 'ran' is also normalized to 'run' by english config
        results = store.search("run", project="inttest")
        titles = [r["title"] for r in results]
        assert "fts-stem" in titles, (
            f"FTS stemming probe failed: search('run') did not find 'ran'. "
            f"Got titles: {titles}"
        )

    def test_put_or_merge_server_side(self, store):
        """Significant #6: put_or_merge endpoint merges overlapping content atomically."""
        # First insert
        rid1, action1 = store.put_or_merge(
            "inttest", "merge-test",
            "the daemon processes semantic events efficiently",
            ttl=1, min_similarity=0.3,
        )
        assert action1 == "inserted", f"expected 'inserted', got {action1!r}"
        assert isinstance(rid1, int) and rid1 > 0

        # Second put with overlapping words — should merge
        rid2, action2 = store.put_or_merge(
            "inttest", "merge-test",
            "the daemon handles semantic events with efficiency",
            ttl=1, min_similarity=0.3,
        )
        assert action2 == "merged", (
            f"expected 'merged' for overlapping content, got {action2!r}"
        )

    def test_delete_removes_entry(self, store):
        """put → delete → get returns None."""
        store.put("inttest", "del-test", "to be deleted", ttl=1)
        deleted = store.delete(project="inttest", title="del-test")
        assert deleted is True
        entry = store.get(project="inttest", title="del-test")
        assert entry is None

    def test_cross_tenant_rls_negative(self, store, other_store):
        """Critical: tenant A's rows are invisible to tenant B.

        This exercises real Postgres RLS FORCE — not possible with the fake server.
        """
        store.put("rls-proj", "rls-secret", "tenant A private content", ttl=1)

        # tenant B must NOT see tenant A's entry
        entry = other_store.get(project="rls-proj", title="rls-secret")
        assert entry is None, (
            f"RLS FAILED: tenant B can read tenant A's entry: {entry}"
        )

        # tenant A can still see it
        own_entry = store.get(project="rls-proj", title="rls-secret")
        assert own_entry is not None

    def test_cross_tenant_write_isolation(self, store, other_store):
        """tenant B cannot overwrite tenant A's row via upsert (RLS WITH CHECK)."""
        store.put("rls-proj", "rls-write", "tenant A write data", ttl=1)

        # tenant B tries to write to same (project, title)
        # RLS WITH CHECK should prevent cross-tenant overwrite:
        # the write lands in tenant B's namespace (different tenant_id), NOT overwriting A
        other_store.put("rls-proj", "rls-write", "tenant B attempted overwrite", ttl=1)

        # tenant A must still see its own original content
        a_entry = store.get(project="rls-proj", title="rls-write")
        assert a_entry is not None
        assert a_entry["content"] == "tenant A write data", (
            f"tenant A's content was overwritten! got: {a_entry['content']!r}"
        )

    def test_search_access_track_vs_silent(self, store):
        """search(access='silent') must NOT increment access_count."""
        store.put("inttest", "search-silent", "search tracking test content", ttl=1)
        # Get baseline access_count (get increments once)
        e_before = store.get(project="inttest", title="search-silent")
        count_before = e_before["access_count"]

        # search with access=silent — no tracking
        store.search("tracking", project="inttest", access="silent")

        e_after = store.get(project="inttest", title="search-silent")
        # get() itself incremented, but silent search must NOT add more
        # (e_after.access_count == count_before + 1 from the get() above)
        assert e_after["access_count"] == count_before + 1, (
            f"silent search must not increment access_count beyond the get() call: "
            f"{count_before} → {e_after['access_count']}"
        )
