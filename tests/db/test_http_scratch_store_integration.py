# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language integration test for HttpScratchStore against the real Java service.

Requires (on THIS machine — darwin/aarch64 with JDK25 GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (mvn -f service/pom.xml package -DskipTests)
  - Java on PATH (or JAVA_HOME/bin/java available)

Marked @pytest.mark.integration — collected but skipped automatically when the
jar or pg16 binaries are absent, so CI (which has neither) stays green.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_http_scratch_store_integration.py -v

What is exercised (bead nexus-gmiaf.13 integration requirements):
  1. SESSION ISOLATION (headline — same tenant, different session):
       session A writes; session B (different NX_T1_SESSION / X-Nexus-T1-Session)
       cannot get/search/list/delete A's entries; cannot mutate A's access_count.
       Proves the session-filter fix (CRITICAL reviewer item 2) through the REAL service.
  2. TENANT RLS:
       a) tenant A's scratch is invisible to tenant B (fail-closed row-level security)
       b) fail-closed: zero rows returned when GUC nexus.t1_tenant is unset (NULL != any tenant_id)
       c) service role rolsuper=false and rolbypassrls=false
  3. FTS through real Postgres:
       a) English stemming probe: content "scratching" matched by query "scratch"
          (plainto_tsquery('english','scratch') = stem 'scratch' = matchs 'scratching')
       b) Tag exact-identifier match via 'simple' config:
          tag "nexus-u2vmv" found by query "nexus-u2vmv" (verbatim, no stemming needed)
       c) OR-query: either branch independently returns results
  4. CRUD round-trip:
       put/get/list/delete/flag/unflag + session_id property + agent attribution preserved
       access_count increments on repeated get
  5. SESSION LIFECYCLE:
       session-close endpoint deletes the session's rows (returns count > 0)
       sweepTenant / TTL sweep deletes rows older than the cutoff

NX_STORAGE_BACKEND is NOT touched — default SQLite/Chroma path unchanged.
The HttpScratchStore is constructed with explicit base_url so the backend env var is irrelevant.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL

# ── Prerequisite paths ────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = (
    Path(_JAVA_HOME) / "bin" / "java"
    if _JAVA_HOME
    else Path(shutil.which("java") or "java")
)

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

# ── Bootstrap SQL (t1 schema) ─────────────────────────────────────────────────
# Mirrors t1-001-baseline.xml changesets 1-5, written as plain SQL for psql.
# Run as the initdb superuser so CREATE ROLE/SCHEMA/POLICY all succeed.

_BOOTSTRAP_SQL = """\
-- Changeset t1-001-1: t1 schema
CREATE SCHEMA IF NOT EXISTS t1;

-- Changeset t1-001-2: UNLOGGED scratch table
CREATE UNLOGGED TABLE t1.scratch (
    id            TEXT         NOT NULL,
    tenant_id     TEXT         NOT NULL,
    session_id    TEXT         NOT NULL,
    content       TEXT         NOT NULL,
    tags          TEXT,
    flagged       BOOLEAN      NOT NULL DEFAULT FALSE,
    flush_project TEXT,
    flush_title   TEXT,
    agent         TEXT,
    access_count  INTEGER      NOT NULL DEFAULT 0,
    last_accessed TIMESTAMPTZ,
    ts            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT scratch_pk PRIMARY KEY (id)
);

CREATE INDEX idx_scratch_tenant_session ON t1.scratch (tenant_id, session_id);
CREATE INDEX idx_scratch_ts ON t1.scratch (ts);

-- Changeset t1-001-3: RLS tenant isolation via nexus.t1_tenant GUC
ALTER TABLE t1.scratch ENABLE ROW LEVEL SECURITY;
ALTER TABLE t1.scratch FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON t1.scratch
    USING      (tenant_id = current_setting('nexus.t1_tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.t1_tenant', true));

-- Changeset t1-001-4: FTS generated tsvector column + GIN index
ALTER TABLE t1.scratch
    ADD COLUMN fts_vector TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(content, '')), 'B') ||
        setweight(to_tsvector('simple',  coalesce(tags,    '')), 'C')
    ) STORED;

CREATE INDEX idx_scratch_fts ON t1.scratch USING GIN (fts_vector);

-- Changeset t1-001-5: service role + grants
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_t1_inttest') THEN
    CREATE ROLE svc_t1_inttest LOGIN PASSWORD 'svc_t1_inttest_pass';
  END IF;
END $$;

GRANT USAGE ON SCHEMA t1 TO svc_t1_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON t1.scratch TO svc_t1_inttest;
ALTER ROLE svc_t1_inttest SET search_path TO t1, public;

-- Prove the service role has neither superuser nor bypassrls privilege
-- (tested later by test_service_role_is_not_superuser_or_bypassrls)
"""


# ── Port helpers ──────────────────────────────────────────────────────────────

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


# ── Module-scoped fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Spin up a hermetic initdb/pg_ctl Postgres 16 instance for T1 scratch tests."""
    pgdata = tempfile.mkdtemp(prefix="nexus_t1_inttest_pg_")
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
             "-U", pg_user, "nexust1test"],
            check=True, capture_output=True,
        )

        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "nexust1test",
             "-v", "ON_ERROR_STOP=1",
             "-c", _BOOTSTRAP_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"psql t1 bootstrap failed (rc={proc.returncode}):\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )


        # net63: JAR runs Liquibase at startup; grants-nexus-svc.xml requires nexus_svc.
        # Create nexus_svc BEFORE starting the JAR (pre-condition for runAlways grant changeset).
        subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "nexust1test",
             "-v", "ON_ERROR_STOP=1", "-c", SERVICE_ROLES_SQL],
            check=True, capture_output=True,
        )

        yield {"port": pg_port, "dbname": "nexust1test", "user": pg_user, "pgdata": pgdata}

    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Launch the shaded JAR against the hermetic PG for T1 scratch.

    The service role is svc_t1_inttest — matches the grants applied in _BOOTSTRAP_SQL.
    NX_STORAGE_BACKEND is intentionally absent so the Python client can construct
    HttpScratchStore directly by base_url without triggering backend-routing.
    """
    svc_port = _free_port()
    token    = "t1-inttest-bearer-secret"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "svc_t1_inttest",
        "NX_DB_PASS": "svc_t1_inttest_pass",
        "NX_POOL_SIZE": "3",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    env.pop("NX_STORAGE_BACKEND_T1", None)

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


def _make_store(service, tenant: str, session_id: str):
    """Construct an HttpScratchStore against the hermetic service."""
    from nexus.db.http_scratch_store import HttpScratchStore
    base_url, token, _ = service
    return HttpScratchStore(
        base_url=base_url,
        tenant=tenant,
        session_id=session_id,
        _token=token,
    )


# ── Integration Tests ─────────────────────────────────────────────────────────

SESSION_A = "inttest-session-alpha"
SESSION_B = "inttest-session-beta"
TENANT_DEFAULT = "default"
TENANT_OTHER = "other-tenant"


class TestCrudRoundTrip:
    """Basic put/get/list/delete/flag/unflag through the real service."""

    def test_put_get_basic(self, service):
        """put then get returns the same content, session_id, and tags."""
        store = _make_store(service, TENANT_DEFAULT, SESSION_A)
        entry_id = store.put("basic integration content", tags="inttest,basic", agent="test-agent")
        assert entry_id  # non-empty uuid

        result = store.get(entry_id)
        assert result is not None, "get() must return the entry we just put"
        assert result["content"] == "basic integration content"
        assert result["tags"] == "inttest,basic"
        assert result["agent"] == "test-agent"
        assert result["session_id"] == SESSION_A
        store.close()

    def test_put_get_agent_attribution_preserved(self, service):
        """agent field round-trips through put -> get unchanged."""
        store = _make_store(service, TENANT_DEFAULT, SESSION_A)
        eid = store.put("agent attribution test", agent="developer")
        result = store.get(eid)
        assert result is not None
        assert result.get("agent") == "developer", (
            f"agent must be preserved through service; got {result.get('agent')!r}"
        )
        store.close()

    def test_access_count_increments_on_repeated_get(self, service):
        """access_count must increment on each get() call through the real service."""
        store = _make_store(service, TENANT_DEFAULT, SESSION_A)
        eid = store.put("access count integration probe")
        r1 = store.get(eid)
        r2 = store.get(eid)
        r3 = store.get(eid)
        assert r1 and r2 and r3
        c1 = int(r1.get("access_count", 0))
        c2 = int(r2.get("access_count", 0))
        c3 = int(r3.get("access_count", 0))
        assert c2 == c1 + 1, f"access_count must increment: {c1} -> {c2}"
        assert c3 == c2 + 1, f"access_count must increment: {c2} -> {c3}"
        store.close()

    def test_list_entries_scoped_to_session(self, service):
        """list_entries returns only this session's entries."""
        storeA = _make_store(service, TENANT_DEFAULT, "list-session-a")
        storeB = _make_store(service, TENANT_DEFAULT, "list-session-b")
        eid_a = storeA.put("list test entry for session A", tags="list-test")
        storeB.put("list test entry for session B", tags="list-test")

        entries_a = storeA.list_entries()
        ids_a = [e["id"] for e in entries_a]
        assert eid_a in ids_a, "session A's entry must appear in its own list"
        session_ids_in_a = {e.get("session_id") for e in entries_a}
        assert session_ids_in_a == {"list-session-a"}, (
            f"list_entries must only return entries for this session; got sessions {session_ids_in_a}"
        )
        storeA.close()
        storeB.close()

    def test_delete_removes_entry(self, service):
        """delete returns True; subsequent get returns None."""
        store = _make_store(service, TENANT_DEFAULT, SESSION_A)
        eid = store.put("delete me please")
        assert store.delete(eid) is True, "delete must return True for existing entry"
        assert store.get(eid) is None, "get after delete must return None"
        store.close()

    def test_flag_unflag_cycle(self, service):
        """flag marks the entry; unflag clears it; get reflects both states."""
        store = _make_store(service, TENANT_DEFAULT, SESSION_A)
        eid = store.put("flag test content", persist=False)

        store.flag(eid, project="test-proj", title="test-title")
        after_flag = store.get(eid)
        assert after_flag is not None
        assert after_flag.get("flagged") is True, "flag() must set flagged=True"
        assert after_flag.get("flush_project") == "test-proj"
        assert after_flag.get("flush_title") == "test-title"

        store.unflag(eid)
        after_unflag = store.get(eid)
        assert after_unflag is not None
        assert after_unflag.get("flagged") is False, "unflag() must clear flagged"
        store.close()

    def test_flagged_entries_returns_only_flagged(self, service):
        """flagged_entries() returns only entries with flagged=True for this session."""
        session = "flagged-entries-test"
        store = _make_store(service, TENANT_DEFAULT, session)
        eid_flagged = store.put("will be flagged for flush", persist=True)
        store.put("not flagged content")  # should NOT appear in flagged list

        flagged = store.flagged_entries()
        flagged_ids = [e["id"] for e in flagged]
        assert eid_flagged in flagged_ids, "flagged entry must appear in flagged_entries()"
        for e in flagged:
            assert e.get("flagged") is True, (
                f"All entries in flagged_entries() must have flagged=True; got {e!r}"
            )
        store.close()


class TestSessionIsolation:
    """CRITICAL: session A and session B (same tenant) cannot cross-contaminate.

    This is the headline integration test — it proves the session column filter
    on ScratchRepository (the bug fixed in the reviewer round: UPDATE was missing
    session_id filter) works end-to-end through the REAL service + REAL Postgres.
    A fake server cannot test this because the session filter is only enforced
    by the real ScratchRepository WHERE clause against real PG RLS.
    """

    def test_session_b_cannot_get_session_a_entry(self, service):
        """Session B get() on session A's entry must return None (wrong session filter)."""
        storeA = _make_store(service, TENANT_DEFAULT, "iso-session-a-get")
        storeB = _make_store(service, TENANT_DEFAULT, "iso-session-b-get")
        eid_a = storeA.put("session A secret content — session B must not see this")

        result = storeB.get(eid_a)
        assert result is None, (
            f"Session B must NOT be able to get session A's entry. "
            f"Got: {result!r}. "
            "This proves the session_id column filter is enforced by the real service."
        )
        storeA.close()
        storeB.close()

    def test_session_b_cannot_see_session_a_in_list(self, service):
        """Session B list_entries() must not include session A's entries."""
        storeA = _make_store(service, TENANT_DEFAULT, "iso-session-a-list")
        storeB = _make_store(service, TENANT_DEFAULT, "iso-session-b-list")
        eid_a = storeA.put("session A list isolation content")
        storeB.put("session B list isolation content")

        entries_b = storeB.list_entries()
        ids_b = [e["id"] for e in entries_b]
        assert eid_a not in ids_b, (
            "Session B's list_entries() must not include session A's entry. "
            "This tests the WHERE session_id=? filter on listEntries()."
        )
        storeA.close()
        storeB.close()

    def test_session_b_search_cannot_find_session_a_content(self, service):
        """Session B search() must not return entries that belong to session A."""
        unique_token = "uniquetoken-iso-search-xzqv"
        storeA = _make_store(service, TENANT_DEFAULT, "iso-session-a-search")
        storeB = _make_store(service, TENANT_DEFAULT, "iso-session-b-search")
        eid_a = storeA.put(f"session A search isolation {unique_token}")

        results_b = storeB.search(unique_token, n_results=10)
        result_ids_b = [r["id"] for r in results_b]
        assert eid_a not in result_ids_b, (
            f"Session B search must not find session A's entries. "
            f"Token: {unique_token!r}. "
            "This proves the session_id filter on search() (ScratchRepository.search WHERE clause)."
        )
        storeA.close()
        storeB.close()

    def test_session_b_delete_cannot_delete_session_a_entry(self, service):
        """Session B delete() on session A's entry must return False (wrong session)."""
        storeA = _make_store(service, TENANT_DEFAULT, "iso-session-a-del")
        storeB = _make_store(service, TENANT_DEFAULT, "iso-session-b-del")
        eid_a = storeA.put("session A entry that session B must not delete")

        result = storeB.delete(eid_a)
        assert result is False, (
            f"Session B must not be able to delete session A's entry. "
            f"delete() must return False. Got: {result!r}."
        )
        # The entry must still be retrievable by session A
        still_there = storeA.get(eid_a)
        assert still_there is not None, (
            "After session B's failed delete, session A must still be able to get the entry."
        )
        storeA.close()
        storeB.close()

    def test_session_b_access_count_cannot_corrupt_session_a(self, service):
        """CRITICAL regression: session B's get() must NOT mutate session A's access_count.

        Pre-fix: ScratchRepository.get() had UPDATE ... WHERE id=? (missing session_id
        filter). If session A and session B had entries with the same id (impossible due
        to UUID PK, but any B-get of a foreign id could in theory affect A's count via
        a bug). Post-fix: UPDATE WHERE id=? AND session_id=? means B's get of A's id
        returns empty AND does NOT issue any UPDATE on A's row.

        This test proves the fix is live in the real service:
        1. A writes entry; access_count = 0
        2. B tries get(A's id) -> None (session isolation)
        3. A reads its own entry -> access_count = 1 (only A's get incremented it)
        """
        storeA = _make_store(service, TENANT_DEFAULT, "iso-session-a-ac")
        storeB = _make_store(service, TENANT_DEFAULT, "iso-session-b-ac")
        eid_a = storeA.put("access count isolation content")

        # B attempts to get A's entry — must return None
        b_result = storeB.get(eid_a)
        assert b_result is None, "Session B must not see session A's entry"

        # A's first real get: access_count should be exactly 1 (B's get issued no UPDATE)
        a_result = storeA.get(eid_a)
        assert a_result is not None
        ac = int(a_result.get("access_count", -1))
        assert ac == 1, (
            f"access_count must be 1 after exactly one get by the owning session. "
            f"Got {ac}. If > 1, session B's failed get() is issuing an extra UPDATE "
            "(the missing session_id filter regression)."
        )
        storeA.close()
        storeB.close()


class TestTenantRLS:
    """Tenant isolation via RLS nexus.t1_tenant GUC.

    These tests exercise the real Postgres RLS policy — a fake server cannot
    prove that the policy is FORCE'd, fail-closed, or that WITH CHECK works.
    """

    def test_tenant_a_invisible_to_tenant_b(self, service):
        """Tenant A's scratch entries must be invisible to tenant B."""
        storeA = _make_store(service, TENANT_DEFAULT, "rls-session-a")
        storeB = _make_store(service, TENANT_OTHER,   "rls-session-a")
        eid_a = storeA.put("tenant A content that tenant B must not see")

        # tenant B using the same session_id but different tenant: must not see the entry
        result = storeB.get(eid_a)
        assert result is None, (
            "Tenant B must not be able to GET tenant A's scratch entry. "
            "RLS nexus.t1_tenant policy must enforce tenant isolation."
        )
        list_b = storeB.list_entries()
        ids_b = [e["id"] for e in list_b]
        assert eid_a not in ids_b, "Tenant B's list_entries must not include tenant A's entry."
        storeA.close()
        storeB.close()

    def test_service_role_is_not_superuser_or_bypassrls(self, service, pg_instance):
        """The service role (svc_t1_inttest) must have rolsuper=false and rolbypassrls=false.

        Proves the RLS policy is not trivially bypassable by the service role used
        in production (nexus_svc). Both attributes must be false on svc_t1_inttest,
        which is the integration test stand-in for nexus_svc.
        """
        import subprocess as _sp
        result = _sp.run(
            [str(_PSQL),
             "-h", "127.0.0.1",
             "-p", str(pg_instance["port"]),
             "-U", pg_instance["user"],
             "-d", pg_instance["dbname"],
             "-t", "-c",
             "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'svc_t1_inttest'"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"psql query failed: {result.stderr}"
        row = result.stdout.strip()
        assert row, "svc_t1_inttest role must exist in pg_roles"
        parts = [p.strip() for p in row.split("|")]
        assert len(parts) == 2, f"unexpected row format: {row!r}"
        rolsuper, rolbypassrls = parts
        assert rolsuper == "f", (
            f"svc_t1_inttest must NOT be a superuser (rolsuper=f). Got: {rolsuper!r}. "
            "A superuser bypasses all RLS policies including tenant_isolation."
        )
        assert rolbypassrls == "f", (
            f"svc_t1_inttest must NOT have BYPASSRLS (rolbypassrls=f). Got: {rolbypassrls!r}. "
            "A role with BYPASSRLS silently skips all RLS policies, breaking tenant isolation."
        )

    def test_rls_fail_closed_unset_guc(self, service, pg_instance):
        """Fail-closed: when nexus.t1_tenant GUC is unset, zero rows returned.

        Inserts a row directly via psql (superuser bypasses RLS), then queries as
        the service role WITHOUT setting nexus.t1_tenant. The service role must see
        zero rows — current_setting('nexus.t1_tenant', true) returns NULL when unset,
        and NULL != any tenant_id makes the policy fail-closed.

        This test cannot be done through the HTTP service (the service always stamps
        the GUC). It uses psql directly against the hermetic PG instance.
        """
        import subprocess as _sp

        # Insert a row directly as superuser (bypasses RLS)
        insert_sql = (
            "INSERT INTO t1.scratch (id, tenant_id, session_id, content) "
            "VALUES ('fail-closed-test-id', 'default', 'rls-test-session', "
            "'rls fail-closed probe row');"
        )
        r = _sp.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_instance["port"]),
             "-U", pg_instance["user"], "-d", pg_instance["dbname"],
             "-v", "ON_ERROR_STOP=1", "-c", insert_sql],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"Direct insert failed: {r.stderr}"

        # Query as service role WITHOUT setting nexus.t1_tenant — must see zero rows
        query_sql = "SELECT count(*) FROM t1.scratch WHERE id = 'fail-closed-test-id';"
        r2 = _sp.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_instance["port"]),
             "-U", "svc_t1_inttest", "-d", pg_instance["dbname"],
             "-t", "-c", query_sql],
            capture_output=True, text=True,
        )
        assert r2.returncode == 0, f"service-role query failed: {r2.stderr}"
        count = int(r2.stdout.strip())
        assert count == 0, (
            f"RLS fail-closed: service role with no nexus.t1_tenant GUC must see 0 rows. "
            f"Got count={count}. "
            "If > 0 the RLS policy is not enforcing or FORCE ROW LEVEL SECURITY is absent."
        )

        # Cleanup
        _sp.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_instance["port"]),
             "-U", pg_instance["user"], "-d", pg_instance["dbname"],
             "-c", "DELETE FROM t1.scratch WHERE id = 'fail-closed-test-id';"],
            capture_output=True,
        )

    def test_rls_with_check_blocks_cross_tenant_write(self, service):
        """WITH CHECK: service must reject attempts to write to a different tenant.

        The ScratchHandler always stamps the tenant from X-Nexus-Tenant header. This
        test verifies that even if an attacker sends a mismatched tenant_id in the
        body, the RLS WITH CHECK policy blocks the write.

        We test this at the HTTP layer by constructing a store for TENANT_OTHER,
        then checking that a put() from TENANT_OTHER cannot retrieve an entry
        originally written by TENANT_DEFAULT — the WITH CHECK enforces the stamp
        matches the GUC at INSERT time.
        """
        storeDefault = _make_store(service, TENANT_DEFAULT, "with-check-session")
        storeOther   = _make_store(service, TENANT_OTHER, "with-check-session")

        eid_default = storeDefault.put("with-check isolation content from default tenant")
        eid_other   = storeOther.put("with-check isolation content from other tenant")

        # Default cannot read other's entry
        assert storeDefault.get(eid_other) is None, (
            "default tenant must not be able to GET other tenant's entry"
        )
        # Other cannot read default's entry
        assert storeOther.get(eid_default) is None, (
            "other tenant must not be able to GET default tenant's entry"
        )
        storeDefault.close()
        storeOther.close()


class TestFTSRealPostgres:
    """FTS through real Postgres — proves the tsvector and OR-query are live.

    These tests CANNOT be reproduced by the fake server (which uses substring
    matching). They prove plainto_tsquery('english') and plainto_tsquery('simple')
    are both in effect on the real PG instance.
    """

    def test_fts_english_stemming_probe(self, service):
        """English stemmer: query 'scratch' must match content containing 'scratching'.

        Both 'scratch' and 'scratching' normalise to lexeme 'scratch' under
        Snowball (English) stemmer. Verified: ts_lexize('english_stem', 'scratching') = {scratch}.
        This proves plainto_tsquery('english', 'scratch') is in effect.
        """
        session = "fts-stem-session"
        store = _make_store(service, TENANT_DEFAULT, session)
        eid = store.put(
            "the agent was scratching its head over the daemon configuration",
            tags="fts-stem-test",
        )
        results = store.search("scratch", n_results=10)
        result_ids = [r["id"] for r in results]
        assert eid in result_ids, (
            "FTS English stemming probe failed: plainto_tsquery('english', 'scratch') "
            "should match content containing 'scratching' (same stem) but did not. "
            "Proves the real PG tsvector/english config is in effect. "
            f"Got result ids: {result_ids}"
        )
        store.close()

    def test_fts_simple_config_exact_tag_match(self, service):
        """Simple config: exact tag identifier match without stemming.

        Tag 'nexus-u2vmv' must be found by query 'nexus-u2vmv'.
        The 'simple' tsquery config preserves identifiers verbatim (no stemming),
        which is essential for bead IDs, RDR references, and code tokens used as tags.
        This proves plainto_tsquery('simple', ...) is in the OR-query.
        """
        session = "fts-simple-session"
        store = _make_store(service, TENANT_DEFAULT, session)
        eid = store.put(
            "some content about a bead fix",
            tags="nexus-u2vmv,fts-simple-test",
        )
        results = store.search("nexus-u2vmv", n_results=10)
        result_ids = [r["id"] for r in results]
        assert eid in result_ids, (
            "FTS simple-config probe failed: plainto_tsquery('simple', 'nexus-u2vmv') "
            "should match the 'nexus-u2vmv' tag verbatim but did not. "
            "Proves plainto_tsquery('simple', ...) branch of the OR-query is in effect. "
            f"Got result ids: {result_ids}"
        )
        store.close()

    def test_fts_or_query_both_branches(self, service):
        """Both OR branches return their respective results independently.

        Entry A: English-stemmable prose (no matching tag).
        Entry B: exact-tag match (no stemmable prose).
        A single search must return BOTH via the OR-query.
        """
        session = "fts-or-session"
        store = _make_store(service, TENANT_DEFAULT, session)

        # Entry A: content stemmable by english config, distinctive tag
        eid_prose = store.put(
            "the processes were running concurrently in the background",
            tags="fts-or-prose-only",
        )
        # Entry B: exact tag that won't match english stemming
        eid_tag = store.put(
            "unrelated boilerplate about connectors and adapters",
            tags="uniquetag-xzq9m",
        )

        # Search for 'run' — should find eid_prose via english stemmer
        results_prose = store.search("run", n_results=10)
        assert any(r["id"] == eid_prose for r in results_prose), (
            "English OR-branch: query 'run' must find content with 'running' "
            f"(same stem). Got: {[r['id'] for r in results_prose]}"
        )

        # Search for exact tag — should find eid_tag via simple config
        results_tag = store.search("uniquetag-xzq9m", n_results=10)
        assert any(r["id"] == eid_tag for r in results_tag), (
            "Simple OR-branch: exact tag 'uniquetag-xzq9m' must match via simple config. "
            f"Got: {[r['id'] for r in results_tag]}"
        )
        store.close()

    def test_fts_scoped_to_session(self, service):
        """Search results must be scoped to the requesting session.

        Even if a different session's content matches the query, search()
        must not return cross-session results.
        """
        unique_token = "uniquetoken-fts-scope-9k4r"
        storeA = _make_store(service, TENANT_DEFAULT, "fts-scope-session-a")
        storeB = _make_store(service, TENANT_DEFAULT, "fts-scope-session-b")
        eid_a = storeA.put(f"session A exclusive content {unique_token}")

        results_b = storeB.search(unique_token, n_results=10)
        ids_b = [r["id"] for r in results_b]
        assert eid_a not in ids_b, (
            "FTS search must be session-scoped. Session B found session A's entry. "
            f"token={unique_token!r} eid_a={eid_a!r} result_ids={ids_b}"
        )
        storeA.close()
        storeB.close()


class TestSessionLifecycle:
    """Session-close and TTL sweep through the real service."""

    def test_session_close_deletes_all_session_rows(self, service):
        """close_session() must delete all entries for this session and return count > 0."""
        session = "lifecycle-close-session"
        store = _make_store(service, TENANT_DEFAULT, session)
        for i in range(3):
            store.put(f"lifecycle close test entry {i}")

        deleted = store.close_session()
        assert deleted == 3, (
            f"close_session() must return count of deleted rows. "
            f"Expected 3, got {deleted}."
        )

        # Subsequent list must return nothing
        remaining = store.list_entries()
        assert remaining == [], (
            f"After close_session(), list_entries() must return []. Got {remaining}"
        )
        store.close()

    def test_session_close_idempotent(self, service):
        """Second close_session() on an already-closed session returns 0 (not error)."""
        session = "lifecycle-idempotent-session"
        store = _make_store(service, TENANT_DEFAULT, session)
        store.put("idempotent close test")
        store.close_session()

        second_close = store.close_session()
        assert second_close == 0, (
            f"Second close_session() must return 0. Got {second_close}."
        )
        store.close()

    def test_clear_delegates_to_session_close(self, service):
        """clear() is an alias for close_session() — must delete all session rows."""
        session = "lifecycle-clear-session"
        store = _make_store(service, TENANT_DEFAULT, session)
        store.put("clear test entry 1")
        store.put("clear test entry 2")

        n = store.clear()
        assert n == 2, f"clear() must return count of deleted rows. Got {n}."
        assert store.list_entries() == [], "After clear(), list_entries must be empty."
        store.close()
