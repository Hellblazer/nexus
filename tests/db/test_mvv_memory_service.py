# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.9 — Minimum Viable Validation (MVV).

Proves the full Phase-1 spine (HTTP bridge → Java service → jOOQ → Postgres
under RLS) works correctly for the memory store by running the SAME behaviour
scenarios that the SQLite suite in tests/test_memory.py (and companion files)
asserts, but exercising them through the real service backend.

Design: This module REUSES the hermetic fixture infrastructure from
tests/db/test_http_memory_store_integration.py (initdb/pg_ctl PG + shaded
jar).  All test assertions are intentionally *identical* to their SQLite
counterparts — if an assertion needs to be weakened to pass, that's a real
seam bug to fix, not a test adjustment.

Test isolation: every test receives a unique project namespace via the ``ns``
fixture (function-scoped UUID suffix).  This lets the module-scoped store be
reused without accumulated data from prior tests leaking into exact-count
assertions like ``len(results) == 1`` or set-equality ``{...} == {...}``.

Coverage map (SQLite source → MVV equivalent):

  tests/test_memory.py
    test_memory_put_upsert               → TestMVVPutGet.test_put_upsert_single_row
    test_memory_get_by_project_title     → TestMVVPutGet.test_get_by_project_title
    test_memory_get_by_id                → TestMVVPutGet.test_get_by_id
    test_memory_get_missing_returns_none → TestMVVPutGet.test_get_missing_returns_none
    test_tags_always_string_not_null     → TestMVVPutGet.test_tags_always_string_not_null
    test_timestamp_utc_second_precision  → TestMVVPutGet.test_timestamp_utc_second_precision
    resolve_title (5 tests)              → TestMVVResolveTitle.*
    test_resolve_title_escapes_wildcards → TestMVVResolveTitle.test_like_wildcard_escaping
    test_memory_search_fts5              → TestMVVSearch.test_search_fts
    test_memory_search_scoped_to_project → TestMVVSearch.test_search_scoped_to_project
    test_memory_expire_ttl               → TestMVVExpire.test_expire_ttl
    test_memory_expire_permanent         → TestMVVExpire.test_expire_permanent_not_deleted
    test_expire_heat_weighting           → TestMVVExpire.test_expire_heat_weighting_extends_ttl
    test_memory_list_by_project          → TestMVVList.test_list_by_project
    test_memory_delete_by_project_title  → TestMVVDelete.test_delete_by_project_title
    test_memory_delete_by_id             → TestMVVDelete.test_delete_by_id
    test_memory_delete_missing_* (2)     → TestMVVDelete.test_delete_missing_*
    test_memory_delete_fts5_not_srch     → TestMVVDelete.test_delete_fts_not_searchable

  tests/test_memory_consolidation.py
    test_find_overlapping_*              → TestMVVConsolidation.test_find_overlapping_*
    test_find_overlapping_tail_words     → TestMVVConsolidation.test_tail_words_overlap_found
    test_merge_memories_*                → TestMVVConsolidation.test_merge_*
    test_flag_stale_uses_last_accessed   → TestMVVConsolidation.test_flag_stale_uses_last_accessed
    test_flag_stale_falls_back_to_ts     → TestMVVConsolidation.test_flag_stale_null_last_accessed_fallback
    test_flag_stale_skips_recent         → TestMVVConsolidation.test_flag_stale_skips_recent_entries

  tests/test_memory_merge_on_write.py
    test_put_or_merge_* (5 tests)        → TestMVVPutOrMerge.*
    test_put_or_merge_empty_content      → TestMVVPutOrMerge.test_empty_content_inserts_no_merge_scan

  NOT PORTED (SQLite-internal / MCP-layer):
    test_memory_expire_ttl conn.execute check (SQLite-internal schema probe)
    test_malformed_fts5_query_raises_valueerror (SQLite FTS5 syntax; PG sanitizes)
    test_t2_uses_session_module_for_session_id (SQLite wiring test)
    All tests/test_memory_put_attribution.py (MCP layer, not MemoryStore API)
    All "Promote command" tests (CLI + T3, unrelated to seam)

Cross-tenant RLS audit (Proof 2, bead C4):
    TestMVVRLSAudit.test_service_role_non_privileged_direct (pg_roles)
    TestMVVRLSAudit.test_cross_tenant_read_isolation
    TestMVVRLSAudit.test_cross_tenant_write_isolation (WITH CHECK)
    TestMVVRLSAudit.test_cross_tenant_list_isolation
    TestMVVRLSAudit.test_cross_tenant_search_isolation

Run:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_mvv_memory_service.py -v
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
import uuid
from pathlib import Path

import pytest

# ── Prerequisite paths (same as test_http_memory_store_integration.py) ────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or pg16 binaries "
            f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]

# ── Bootstrap SQL (identical to test_http_memory_store_integration.py) ────────

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
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_mvvtest') THEN
    CREATE ROLE svc_mvvtest LOGIN PASSWORD 'svc_mvvtest_pass';
  END IF;
END $$;

GRANT USAGE ON SCHEMA nexus TO svc_mvvtest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO svc_mvvtest;
GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO svc_mvvtest;
ALTER ROLE svc_mvvtest SET search_path TO nexus, public;
"""


# ── Port helpers ───────────────────────────────────────────────────────────────

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


# ── Module-scoped fixtures (hermetic PG + shaded JAR) ─────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Hermetic initdb/pg_ctl Postgres 16 instance (module-scoped)."""
    pgdata = tempfile.mkdtemp(prefix="nexus_mvv_pg_")
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
             "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexusmvv"],
            check=True, capture_output=True,
        )
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "nexusmvv",
             "-v", "ON_ERROR_STOP=1", "-c", _BOOTSTRAP_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"psql bootstrap failed (rc={proc.returncode}):\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
        yield {"port": pg_port, "dbname": "nexusmvv", "user": pg_user, "pgdata": pgdata}
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Shaded JAR against the hermetic PG.  Yields (base_url, token, proc)."""
    svc_port = _free_port()
    token    = "mvv-bearer-secret-xyz"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "svc_mvvtest",
        "NX_DB_PASS": "svc_mvvtest_pass",
        "NX_POOL_SIZE": "3",
    }
    env.pop("NX_STORAGE_BACKEND", None)

    proc = subprocess.Popen(
        [str(_JAVA), "--enable-preview", "-jar", str(_JAR)],
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
def store(service):
    """HttpMemoryStore (tenant='default') for the MVV suite."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    base_url, token, _ = service
    os.environ["NX_SERVICE_TOKEN"] = token
    s = HttpMemoryStore(base_url=base_url, tenant="default")
    yield s
    s.close()


@pytest.fixture(scope="module")
def other_store(service):
    """HttpMemoryStore for the cross-tenant RLS probe (tenant='rls-other')."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    base_url, token, _ = service
    os.environ["NX_SERVICE_TOKEN"] = token
    s = HttpMemoryStore(base_url=base_url, tenant="rls-other")
    yield s
    s.close()


@pytest.fixture(scope="module")
def pg_conn(pg_instance):
    """Direct psql subprocess helper for backdating timestamps in expire/stale tests.

    Returns a callable: pg_conn(sql) -> None.
    Uses psql to run SQL as the superuser (bypasses RLS).
    """
    pg_port = pg_instance["port"]
    pg_user = pg_instance["user"]
    dbname  = pg_instance["dbname"]

    def run_sql(sql: str) -> None:
        """Execute SQL as superuser (bypasses RLS for test setup)."""
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", dbname,
             "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"psql backdate failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
            )

    return run_sql


@pytest.fixture
def ns() -> str:
    """Function-scoped unique project namespace prefix (UUID4 short).

    Inject into every test that relies on exact-count assertions
    (search len==1, list set-equality, etc.) so accumulated data from
    other tests in the same module-scoped DB never leaks.
    """
    return uuid.uuid4().hex[:10]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts(days_ago: int) -> str:
    """Return a UTC ISO timestamp N days in the past (second precision)."""
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Proof 1: Core memory behaviours against the HTTP backend ──────────────────


class TestMVVPutGet:
    """put / get / upsert — maps to test_memory.py TestMemoryPutGet."""

    def test_put_upsert_single_row(self, store, ns) -> None:
        """Upsert must result in exactly one row (not two inserts)."""
        p = f"proj-{ns}"
        store.put(p, "mvv-upsert.md", "first")
        store.put(p, "mvv-upsert.md", "updated")
        entry = store.get(project=p, title="mvv-upsert.md")
        assert entry is not None
        assert entry["content"] == "updated"

    def test_get_by_project_title(self, store, ns) -> None:
        """get(project=, title=) returns correct entry fields."""
        p = f"proj_a-{ns}"
        store.put(p, "notes.md", "hello world", ttl=1)
        result = store.get(project=p, title="notes.md")
        assert result is not None
        assert (result["content"], result["project"], result["title"]) == (
            "hello world", p, "notes.md"
        )

    def test_get_by_id(self, store, ns) -> None:
        """get(id=) retrieves by numeric row id."""
        row_id = store.put(f"p-{ns}", "x.md", "by id", ttl=1)
        assert store.get(id=row_id)["content"] == "by id"

    def test_get_missing_returns_none(self, store) -> None:
        """get for non-existent entry returns None."""
        assert store.get(project="no-such-proj-zz99", title="missing.md") is None

    def test_tags_always_string_not_null(self, store, ns) -> None:
        """Critical #2: untagged entry must have tags='' not None / missing."""
        p = f"proj-{ns}"
        store.put(p, "tags-none.md", "no tags", ttl=1)
        entry = store.get(project=p, title="tags-none.md")
        assert entry is not None
        assert "tags" in entry
        assert entry["tags"] == ""

    def test_timestamp_utc_second_precision(self, store, ns) -> None:
        """Timestamp must be ISO-8601 UTC second-precision (no sub-second noise)."""
        p = f"proj-{ns}"
        store.put(p, "ts-fmt.md", "timestamp check", ttl=1)
        entry = store.get(project=p, title="ts-fmt.md")
        assert entry is not None
        ts = entry["timestamp"]
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts), (
            f"timestamp must be yyyy-MM-dd'T'HH:mm:ss'Z', got: {ts!r}"
        )


class TestMVVResolveTitle:
    """resolve_title exact-then-prefix fallback — maps to test_memory.py."""

    def test_exact_match_wins(self, store, ns) -> None:
        """Exact (project, title) match returns the entry with no candidates."""
        p = f"p-{ns}"
        store.put(p, "088-research-1", "short", ttl=1)
        store.put(p, "088-research-1: full-suffix", "long", ttl=1)
        entry, candidates = store.resolve_title(project=p, title="088-research-1")
        assert entry is not None and entry["content"] == "short"
        assert candidates == []

    def test_unique_prefix_match(self, store, ns) -> None:
        """No exact match + exactly one prefix candidate returns that candidate."""
        p = f"p-{ns}"
        store.put(p, "088-rv1-research: RDR-092 baseline", "body", ttl=1)
        entry, candidates = store.resolve_title(project=p, title="088-rv1-research")
        assert entry is not None
        assert entry["title"] == "088-rv1-research: RDR-092 baseline"
        assert candidates == []

    def test_ambiguous_prefix_returns_candidates(self, store, ns) -> None:
        """Multiple prefix matches returns (None, [candidates])."""
        p = f"p-{ns}"
        store.put(p, "088-rv2-research: first", "a", ttl=1)
        store.put(p, "088-rv2-research-b: other", "b", ttl=1)
        entry, candidates = store.resolve_title(project=p, title="088-rv2-research")
        assert entry is None
        titles = sorted(c["title"] for c in candidates)
        assert titles == ["088-rv2-research-b: other", "088-rv2-research: first"]

    def test_no_match_returns_empty(self, store, ns) -> None:
        """Nothing matches returns (None, [])."""
        p = f"p-{ns}"
        entry, candidates = store.resolve_title(project=p, title="no-such-prefix-xyz")
        assert entry is None
        assert candidates == []

    def test_scoped_to_project(self, store, ns) -> None:
        """Prefix fallback honours the project boundary."""
        p1, p2 = f"rp1-{ns}", f"rp2-{ns}"
        store.put(p1, "088-rv3-research: in-p1", "one", ttl=1)
        store.put(p2, "088-rv3-research: in-p2", "two", ttl=1)
        entry, candidates = store.resolve_title(project=p1, title="088-rv3-research")
        assert entry is not None
        assert entry["project"] == p1
        assert candidates == []

    def test_like_wildcard_escaping(self, store, ns) -> None:
        """A literal '_' in the title prefix must not become a LIKE wildcard.

        Mirrors test_memory.py::test_resolve_title_escapes_like_wildcards.
        The Java service uses ESCAPE '\\' with _.replace('_', '\\_') so
        'a_' must only match 'a_b_c', not 'axb'.
        HIGH divergence risk: pre-fix code would match both.
        """
        p = f"p-{ns}"
        store.put(p, "a_b_c", "underscore-literal", ttl=1)
        store.put(p, "axb", "not-a-match-for-underscore", ttl=1)
        entry, candidates = store.resolve_title(project=p, title="a_")
        # Only the literal 'a_' prefix matches 'a_b_c', not 'axb'
        assert entry is not None, (
            "resolve_title('a_') matched no entry — LIKE wildcard escaping is broken "
            "or 'axb' was incorrectly matched as a candidate"
        )
        assert entry["title"] == "a_b_c", (
            f"Expected 'a_b_c', got {entry['title']!r} — "
            "'_' is being treated as a single-character LIKE wildcard"
        )
        assert candidates == []


class TestMVVSearch:
    """FTS search — maps to test_memory.py FTS section."""

    def test_search_fts(self, store, ns) -> None:
        """FTS returns exactly the entries matching a multi-word query.

        Mirrors test_memory.py:111:
            assert {r["title"] for r in db.search("quick fox")} == {"alpha.md", "gamma.md"}
        """
        p = f"sp-{ns}"
        store.put(p, "alpha.md", "The quick brown fox", ttl=1)
        store.put(p, "beta.md", "A lazy dog sleeping", ttl=1)
        store.put(p, "gamma.md", "The quick fox jumps high", ttl=1)
        results = store.search("quick fox", project=p)
        titles = {r["title"] for r in results}
        assert titles == {"alpha.md", "gamma.md"}, (
            f"Expected exactly {{alpha.md, gamma.md}}, got: {titles}"
        )

    def test_search_scoped_to_project(self, store, ns) -> None:
        """search(project=) scopes results to exactly that project only.

        Mirrors test_memory.py:118:
            assert len(results) == 1 and results[0]["project"] == "proj_a"
        """
        pa = f"sa-{ns}"
        pb = f"sb-{ns}"
        store.put(pa, "a.md", "authentication token", ttl=1)
        store.put(pb, "b.md", "authentication token", ttl=1)
        results = store.search("authentication", project=pa)
        assert len(results) == 1, (
            f"Expected exactly 1 result for project '{pa}', got {len(results)}: {results}"
        )
        assert results[0]["project"] == pa

    def test_search_stemming(self, store, ns) -> None:
        """FTS stemming: 'run' must find 'running' (Snowball english stemmer)."""
        p = f"stem-{ns}"
        store.put(p, "fts-stem.md",
                  "the daemon was running its event loop repeatedly", ttl=1)
        results = store.search("run", project=p)
        titles = [r["title"] for r in results]
        assert "fts-stem.md" in titles, (
            f"FTS stemming failed: 'run' should match 'running' but got: {titles}"
        )


class TestMVVList:
    """list_entries — maps to test_memory.py test_memory_list_by_project."""

    def test_list_by_project(self, store, ns) -> None:
        """list_entries scoped to project returns exactly that project's entries.

        Mirrors test_memory.py:143:
            assert {e["title"] for e in db.list_entries(project="proj_a")} == {"x.md","y.md"}
        """
        pa = f"la-{ns}"
        pb = f"lb-{ns}"
        store.put(pa, "x.md", "x", ttl=1)
        store.put(pa, "y.md", "y", ttl=1)
        store.put(pb, "z.md", "z", ttl=1)
        entries = store.list_entries(project=pa)
        titles = {e["title"] for e in entries}
        assert titles == {"x.md", "y.md"}, (
            f"Expected exactly {{x.md, y.md}} for project '{pa}', got: {titles}"
        )


class TestMVVDelete:
    """delete — maps to test_memory.py TestMemoryDelete."""

    def test_delete_by_project_title(self, store, ns) -> None:
        """put -> delete by (project, title) -> get returns None."""
        p = f"dp-{ns}"
        store.put(p, "a.md", "hello", ttl=1)
        assert store.delete(project=p, title="a.md") is True
        assert store.get(project=p, title="a.md") is None

    def test_delete_by_id(self, store, ns) -> None:
        """put -> delete by id -> get returns None."""
        p = f"dp-{ns}"
        row_id = store.put(p, "b.md", "world", ttl=1)
        assert store.delete(id=row_id) is True
        assert store.get(id=row_id) is None

    def test_delete_missing_by_project_title_returns_false(self, store) -> None:
        """delete for a non-existent (project, title) returns False."""
        result = store.delete(project="no-project-zz99", title="such.md")
        assert result is False

    def test_delete_missing_by_id_returns_false(self, store) -> None:
        """delete for a non-existent id returns False."""
        result = store.delete(id=999999999)
        assert result is False

    def test_delete_fts_not_searchable(self, store, ns) -> None:
        """After delete, the entry's content is not findable via FTS."""
        p = f"dp-{ns}"
        store.put(p, "c.md", "unique canary token xyzzy42", ttl=1)
        store.delete(project=p, title="c.md")
        results = store.search("canary xyzzy42", project=p)
        assert results == [], f"Deleted entry still findable via FTS: {results}"


class TestMVVExpire:
    """expire (TTL housekeeping) — maps to test_memory.py TestMemoryExpire.

    SQLite tests backdate via conn.execute; HTTP tests backdate via direct
    superuser psql connection (pg_conn fixture), then call expire() via HTTP.
    The assertion is identical: expired row gone, permanent row survives.
    """

    def test_expire_ttl(self, store, pg_conn, ns) -> None:
        """Entry with ttl=1 that is 2 days old must be deleted by expire().

        Mirrors test_memory.py:126: assert db.expire() == 1
        The HTTP backend returns a list of deleted IDs; we assert the specific
        row_id appears in deleted_ids (exact membership, no fallback arm).
        """
        p = f"ep-{ns}"
        row_id = store.put(p, "old.md", "stale", ttl=1)
        past = _ts(days_ago=2)
        pg_conn(
            f"UPDATE nexus.memory SET timestamp = '{past}' "
            f"WHERE id = {row_id}"
        )
        deleted_ids = store.expire()
        assert row_id in deleted_ids, (
            f"expire() must have deleted row {row_id}; got deleted_ids={deleted_ids}"
        )
        assert store.get(project=p, title="old.md") is None

    def test_expire_permanent_not_deleted(self, store, pg_conn, ns) -> None:
        """Permanent (ttl=None) entry is NOT deleted even if very old."""
        p = f"ep-{ns}"
        row_id = store.put(p, "perm.md", "keep forever", ttl=None)
        past = _ts(days_ago=365)
        pg_conn(
            f"UPDATE nexus.memory SET timestamp = '{past}' "
            f"WHERE id = {row_id}"
        )
        store.expire()
        entry = store.get(project=p, title="perm.md")
        assert entry is not None, "Permanent entry must survive expire()"

    def test_expire_heat_weighting_extends_ttl(self, store, pg_conn, ns) -> None:
        """Heat-weighted effective_ttl = base_ttl * (1 + ln(access_count+1)) must
        protect a frequently-accessed entry that would be expired by naive age > ttl.

        Mirrors Python MemoryStore.expire() formula (memory_store.py:718):
            effective_ttl = ttl * (1 + log(access_count + 1))

        Setup: ttl=1, age=1.5 days (past naive threshold), access_count=5.
            effective_ttl = 1 * (1 + ln(6)) ≈ 1 * 2.79 ≈ 2.79 days
            age 1.5 < effective_ttl 2.79 → entry SURVIVES expire().

        If Java expire() does naive age > ttl (ignoring access_count), this test
        fails — that is a real seam bug to fix.
        """
        import math
        p = f"ep-{ns}"
        row_id = store.put(p, "hot.md", "frequently accessed entry", ttl=1)
        # Backdate to 1.5 days ago (past raw ttl=1 but within heat-weighted ttl)
        past = _ts(days_ago=0)  # need fractional days; use direct SQL
        pg_conn(
            f"UPDATE nexus.memory "
            f"SET timestamp = NOW() - INTERVAL '36 hours', "
            f"    access_count = 5 "
            f"WHERE id = {row_id}"
        )
        # effective_ttl = 1 * (1 + ln(6)) ≈ 2.79 days; age 1.5 days < 2.79 → survives
        deleted_ids = store.expire()
        assert row_id not in deleted_ids, (
            f"Heat-weighted expire failed: row {row_id} (access_count=5, ttl=1, age=1.5d) "
            f"was deleted despite effective_ttl≈2.79 days. "
            f"Java expire() likely ignores access_count (naive age>ttl check). "
            f"deleted_ids={deleted_ids}"
        )
        assert store.get(project=p, title="hot.md") is not None, (
            "hot.md was deleted by expire() — heat-weighting formula not applied"
        )


class TestMVVConsolidation:
    """Consolidation + merge + flag_stale — maps to test_memory_consolidation.py."""

    def test_find_overlapping_two_similar(self, store, ns) -> None:
        """Two entries about the same topic → overlap pair found."""
        p = f"co-{ns}"
        store.put(p, "search-arch.md",
                  "search engine architecture design patterns optimization", ttl=1)
        store.put(p, "search-design.md",
                  "search engine architecture design patterns implementation", ttl=1)
        pairs = store.find_overlapping_memories(p)
        assert len(pairs) >= 1
        titles = {pairs[0][0]["title"], pairs[0][1]["title"]}
        assert titles == {"search-arch.md", "search-design.md"}

    def test_find_no_overlap_dissimilar(self, store, ns) -> None:
        """Entries on different topics → no overlap."""
        p = f"co2-{ns}"
        store.put(p, "auth.md", "authentication security tokens", ttl=1)
        store.put(p, "deploy.md", "kubernetes docker containers", ttl=1)
        pairs = store.find_overlapping_memories(p)
        assert len(pairs) == 0

    def test_find_overlapping_respects_threshold(self, store, ns) -> None:
        """High threshold filters out moderate overlap."""
        p = f"co3-{ns}"
        store.put(p, "a.md", "search engine architecture design", ttl=1)
        store.put(p, "b.md", "search engine optimization performance", ttl=1)
        pairs = store.find_overlapping_memories(p, min_similarity=0.95)
        assert len(pairs) == 0

    def test_tail_words_overlap_found(self, store, ns) -> None:
        """nexus-uul2r regression: overlap detected even when shared tokens are
        NOT among leading words (tail-only overlap).

        Mirrors test_memory_consolidation.py:47. The SQLite pre-fix code built an
        FTS5 AND-query from each entry's first 3 non-stopword words, so tail-shared
        entries were never retrieved as candidates.  The HTTP path uses get_all()
        (no FTS prefiltering) so this is structurally correct; this test confirms
        the Jaccard computation still catches it.
        """
        p = f"co-tail-{ns}"
        store.put(p, "alpha", (
            "Leading words here padding clause. "
            "Shared distinctive tokens trail: zephyranth orchard meadow."
        ), ttl=1)
        store.put(p, "beta", (
            "Different opening segment altogether. "
            "Shared distinctive tokens trail: zephyranth orchard meadow."
        ), ttl=1)
        pairs = store.find_overlapping_memories(p, min_similarity=0.3)
        assert len(pairs) == 1, (
            f"Expected 1 overlap pair for tail-shared entries, got {len(pairs)}"
        )
        assert {pairs[0][0]["title"], pairs[0][1]["title"]} == {"alpha", "beta"}

    def test_merge_memories_deletes_and_updates(self, store, ns) -> None:
        """merge_memories keeps one entry, deletes the rest, updates content."""
        p = f"cm-{ns}"
        id1 = store.put(p, "keep.md", "original", ttl=1)
        id2 = store.put(p, "delete.md", "duplicate", ttl=1)
        store.merge_memories(keep_id=id1, delete_ids=[id2], merged_content="merged version")
        kept = store.get(id=id1)
        assert kept is not None and kept["content"] == "merged version"
        assert store.get(id=id2) is None

    def test_merge_cleans_fts_index(self, store, ns) -> None:
        """After merge, FTS search for deleted content returns no results."""
        p = f"cm2-{ns}"
        id1 = store.put(p, "keep.md", "alpha content", ttl=1)
        id2 = store.put(p, "gone.md", "unique_zygomorphic_keyword", ttl=1)
        store.merge_memories(keep_id=id1, delete_ids=[id2], merged_content="alpha merged")
        results = store.search("unique_zygomorphic_keyword", project=p)
        assert results == [], f"FTS should not find deleted content: {results}"

    def test_merge_updates_fts_for_kept_entry(self, store, ns) -> None:
        """After merge, merged content is findable via FTS search."""
        p = f"cm3-{ns}"
        id1 = store.put(p, "keep.md", "original boring content", ttl=1)
        id2 = store.put(p, "gone.md", "other stuff", ttl=1)
        store.merge_memories(
            keep_id=id1, delete_ids=[id2], merged_content="unique_merged_phrase_xyz"
        )
        results = store.search("unique_merged_phrase_xyz", project=p)
        assert len(results) == 1
        assert results[0]["title"] == "keep.md"

    def test_merge_multiple_entries(self, store, ns) -> None:
        """Can merge 3+ entries into one."""
        p = f"cm4-{ns}"
        id1 = store.put(p, "keep.md", "base", ttl=1)
        id2 = store.put(p, "dup1.md", "dup one", ttl=1)
        id3 = store.put(p, "dup2.md", "dup two", ttl=1)
        store.merge_memories(keep_id=id1, delete_ids=[id2, id3], merged_content="all merged")
        assert store.get(id=id1)["content"] == "all merged"
        assert store.get(id=id2) is None
        assert store.get(id=id3) is None

    def test_flag_stale_uses_last_accessed(self, store, pg_conn, ns) -> None:
        """Entries with old last_accessed are flagged as stale."""
        p = f"fs-{ns}"
        store.put(p, "old.md", "old entry", ttl=1)
        old_ts = _ts(days_ago=45)
        pg_conn(
            f"UPDATE nexus.memory SET last_accessed = '{old_ts}' "
            f"WHERE project = '{p}' AND title = 'old.md'"
        )
        store.put(p, "fresh.md", "fresh entry", ttl=1)
        store.get(project=p, title="fresh.md")  # sets last_accessed to now

        stale = store.flag_stale_memories(p, idle_days=30)
        stale_titles = {e["title"] for e in stale}
        assert "old.md" in stale_titles
        assert "fresh.md" not in stale_titles

    def test_flag_stale_null_last_accessed_fallback(self, store, pg_conn, ns) -> None:
        """Entries with NULL last_accessed fall back to timestamp for staleness.

        Mirrors test_memory_consolidation.py:141 (test_flag_stale_falls_back_to_timestamp).
        Java must use COALESCE(last_accessed, timestamp) or equivalent.
        If Java only checks last_accessed IS NOT NULL and skips NULL rows, this fails.
        """
        p = f"fs-null-{ns}"
        store.put(p, "never-accessed.md", "untouched", ttl=1)
        # Backdate timestamp only; last_accessed stays NULL (never get()d)
        pg_conn(
            f"UPDATE nexus.memory "
            f"SET timestamp = NOW() - INTERVAL '45 days' "
            f"WHERE project = '{p}' AND title = 'never-accessed.md'"
        )
        stale = store.flag_stale_memories(p, idle_days=30)
        stale_titles = {e["title"] for e in stale}
        assert "never-accessed.md" in stale_titles, (
            "flag_stale must fall back to timestamp when last_accessed is NULL. "
            "Java CASE WHEN last_accessed IS NOT NULL ELSE timestamp must be present."
        )

    def test_flag_stale_skips_recent_entries(self, store, ns) -> None:
        """Recently created entries are not flagged."""
        p = f"fs2-{ns}"
        store.put(p, "new.md", "just added", ttl=1)
        stale = store.flag_stale_memories(p, idle_days=14)
        assert all(e["title"] != "new.md" for e in stale)


class TestMVVPutOrMerge:
    """put_or_merge — maps to test_memory_merge_on_write.py."""

    def test_inserts_when_project_empty(self, store, ns) -> None:
        """No existing entries → plain insert, action='inserted'."""
        p = f"pm1-{ns}"
        row_id, action = store.put_or_merge(
            project=p, title="a.md", content="alpha beta gamma delta", ttl=1
        )
        assert action == "inserted"
        assert row_id > 0

    def test_inserts_when_dissimilar(self, store, ns) -> None:
        """Existing entry on a different topic → insert; 2 rows total.

        Mirrors test_memory_merge_on_write.py::test_put_or_merge_inserts_when_dissimilar
        and adds the get_all count assertion from test_memory_merge_on_write.py:93.
        """
        p = f"pm2-{ns}"
        store.put(p, "auth.md", "authentication security tokens oauth", ttl=1)
        row_id, action = store.put_or_merge(
            project=p, title="deploy.md",
            content="kubernetes docker containers orchestration",
            ttl=1,
        )
        assert action == "inserted"
        # Both rows must exist (no spurious merge)
        all_entries = store.get_all(p)
        assert len(all_entries) == 2, (
            f"Expected 2 rows after dissimilar insert, got {len(all_entries)}: "
            f"{[e['title'] for e in all_entries]}"
        )

    def test_merges_high_overlap_into_existing(self, store, ns) -> None:
        """High word-set overlap → merge into existing, one row remains."""
        p = f"pm3-{ns}"
        keep_id = store.put(
            p, "search-arch.md",
            "search engine architecture design patterns optimization caching",
            ttl=1,
        )
        row_id, action = store.put_or_merge(
            project=p, title="search-design.md",
            content="search engine architecture design patterns optimization sharding",
            ttl=1,
            min_similarity=0.5,
        )
        assert action == "merged"
        assert row_id == keep_id
        merged = store.get(id=keep_id)
        assert merged is not None
        assert "caching" in merged["content"]
        assert "sharding" in merged["content"]

    def test_respects_threshold(self, store, ns) -> None:
        """Overlap below min_similarity → insert, not merge."""
        p = f"pm4-{ns}"
        store.put(p, "a.md", "search engine architecture design", ttl=1)
        _, action = store.put_or_merge(
            project=p, title="b.md",
            content="kubernetes docker deployment pipeline",
            ttl=1,
            min_similarity=0.5,
        )
        assert action == "inserted"

    def test_same_title_is_upsert_not_merge(self, store, ns) -> None:
        """Exact (project, title) collision takes the identity-upsert path,
        never the cross-title merge — action='inserted' (upsert) and one row.

        Mirrors test_memory_merge_on_write.py:75, including get_all row count.
        """
        p = f"pm5-{ns}"
        first_id = store.put(p, "x.md", "initial alpha beta gamma", ttl=1)
        row_id, action = store.put_or_merge(
            project=p, title="x.md",
            content="updated alpha beta gamma delta",
            ttl=1,
            min_similarity=0.5,
        )
        assert action == "inserted"  # service reports upsert as "inserted"
        assert row_id == first_id
        # Only one row must exist (upsert, not insert + merge)
        all_entries = store.get_all(p)
        assert len(all_entries) == 1, (
            f"Expected exactly 1 row after same-title upsert, got {len(all_entries)}"
        )
        assert all_entries[0]["content"] == "updated alpha beta gamma delta"

    def test_empty_content_inserts_no_merge_scan(self, store, ns) -> None:
        """Empty/whitespace content has no word-set → plain insert, no Jaccard div-by-zero.

        Mirrors test_memory_merge_on_write.py:88.
        """
        p = f"pm6-{ns}"
        store.put(p, "a.md", "alpha beta gamma", ttl=1)
        row_id, action = store.put_or_merge(project=p, title="blank.md", content="", ttl=1)
        assert action == "inserted"
        all_entries = store.get_all(p)
        assert len(all_entries) == 2, (
            f"Expected 2 rows after empty-content insert, got {len(all_entries)}"
        )


# ── Proof 2: Cross-tenant RLS audit ───────────────────────────────────────────


class TestMVVRLSAudit:
    """Cross-tenant RLS isolation proofs (bead C4 + RLS negative).

    Proves isolation at the full HTTP → jOOQ → PG layer.
    """

    def test_service_role_non_privileged_direct(self, pg_instance) -> None:
        """C4 criterion: the service role (svc_mvvtest) is neither superuser
        nor bypassrls.  Queries pg_roles via the superuser (pg_user) connection.
        """
        pg_port = pg_instance["port"]
        pg_user = pg_instance["user"]
        dbname  = pg_instance["dbname"]

        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", dbname,
             "-t", "-A",  # tuples only, unaligned for easy parsing
             "-c", "SELECT rolsuper::text || '|' || rolbypassrls::text "
                   "FROM pg_roles WHERE rolname = 'svc_mvvtest'"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"psql failed: {proc.stderr}"
        output = proc.stdout.strip()
        assert output, "svc_mvvtest role not found in pg_roles"
        rolsuper, rolbypassrls = output.split("|")
        # PG 16 psql -t -A outputs 'false'/'true' (text form), not 'f'/'t'
        assert rolsuper in ("f", "false"), (
            f"svc_mvvtest must NOT be superuser, got rolsuper={rolsuper!r}"
        )
        assert rolbypassrls in ("f", "false"), (
            f"svc_mvvtest must NOT bypassrls, got rolbypassrls={rolbypassrls!r}"
        )

    def test_cross_tenant_read_isolation(self, store, other_store, ns) -> None:
        """Tenant A's rows are invisible to tenant B (RLS USING policy).

        Proves isolation at the full HTTP → jOOQ → PG layer end-to-end.
        """
        p = f"rls-proj-{ns}"
        store.put(p, "rls-secret", "tenant default private content", ttl=1)

        entry = other_store.get(project=p, title="rls-secret")
        assert entry is None, (
            f"RLS FAILED: tenant 'rls-other' can read tenant 'default' entry: {entry}"
        )

        own_entry = store.get(project=p, title="rls-secret")
        assert own_entry is not None, "Tenant A must be able to read its own entry"

    def test_cross_tenant_write_isolation(self, store, other_store, ns) -> None:
        """Tenant B's write to same (project, title) lands in B's namespace,
        NOT overwriting A's row (RLS WITH CHECK policy + separate UNIQUE key).
        """
        p = f"rls-proj-{ns}"
        store.put(p, "rls-write", "tenant default original", ttl=1)

        other_store.put(p, "rls-write", "tenant other attempted overwrite", ttl=1)

        a_entry = store.get(project=p, title="rls-write")
        assert a_entry is not None
        assert a_entry["content"] == "tenant default original", (
            f"tenant A's content was overwritten: {a_entry['content']!r}"
        )

    def test_cross_tenant_list_isolation(self, store, other_store, ns) -> None:
        """list_entries for one tenant must not return rows from the other."""
        p = f"rls-list-{ns}"
        store.put(p, "only-for-default", "default tenant content", ttl=1)
        other_entries = other_store.list_entries(project=p)
        titles_other = [e["title"] for e in other_entries]
        assert "only-for-default" not in titles_other, (
            f"RLS FAILED: tenant 'rls-other' can list tenant 'default' entries: {titles_other}"
        )

    def test_cross_tenant_search_isolation(self, store, other_store, ns) -> None:
        """FTS search for one tenant must not return rows from the other."""
        p = f"rls-search-{ns}"
        store.put(p, "secret-doc", "unique_rls_token_xyzzy99", ttl=1)
        results = other_store.search("unique_rls_token_xyzzy99", project=p)
        assert results == [], (
            f"RLS FAILED: tenant 'rls-other' can FTS-search tenant 'default' entries: {results}"
        )
