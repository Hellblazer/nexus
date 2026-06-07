# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language integration test for HttpMemoryStore against the real Java service.

Requires (on THIS machine — darwin/aarch64 with JDK25 GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (mvn -f service/pom.xml package -DskipTests)
  - Java on PATH (or JAVA_HOME/bin/java available)

Marked @pytest.mark.integration — collected but skipped automatically when the
jar or pg16 binaries are absent, so CI (which has neither) stays green.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_http_memory_store_integration.py -v

What is exercised (critic issue #7):
  a) Real FTS: put -> search; stemming probe "run" finds content with "running"
     (ts_lexize('english_stem','running') = {run} — Snowball stemmer)
  b) tags round-trip: untagged entry has tags=""  (Critical #2)
  c) Timestamp format: UTC second-precision Z    (Significant #4)
  d) Cross-tenant RLS negative: tenant A's rows invisible to tenant B
  e) Cross-tenant write isolation: RLS WITH CHECK prevents cross-tenant overwrite
  f) put_or_merge server-side endpoint: merge path  (Significant #6)
  g) Access count: get() twice -> access_count increases  (Significant #5)
  h) search(access='silent'): does NOT increment access_count beyond the get() call
  i) delete: put -> delete -> get returns None

NX_STORAGE_BACKEND is NOT touched — default SQLite path is unchanged.
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

# ── Prerequisite paths ────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

# Java binary: honour JAVA_HOME if set, fall back to PATH
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

# ── Schema DDL (extracted from memory-001-baseline.xml) ──────────────────────
# Run as the superuser (the initdb OS user) so CREATE ROLE succeeds.

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
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_inttest') THEN
    CREATE ROLE svc_inttest LOGIN PASSWORD 'svc_inttest_pass';
  END IF;
END $$;

GRANT USAGE ON SCHEMA nexus TO svc_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO svc_inttest;
GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO svc_inttest;
ALTER ROLE svc_inttest SET search_path TO nexus, public;
"""


# ── Port helpers ─────────────────────────────────────────────────────────────

def _free_port() -> int:
    """Bind to :0, read the OS-assigned port, close, return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 30.0) -> None:
    """Poll until TCP port accepts connections or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


# ── Module-scoped fixtures ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Spin up a hermetic initdb/pg_ctl Postgres 16 instance.

    Key choices:
    - ``--auth=trust``: all connections (local socket + TCP) are trusted — no password prompt.
    - port written to ``postgresql.conf`` before ``pg_ctl start``.
    - ``pg_ctl start -w``: waits until server accepts connections (no extra polling needed).
    - Unix socket dir set to pgdata so it doesn't collide with any running system PG.
    - Torn down with ``pg_ctl stop -m immediate`` even on failure.
    """
    pgdata = tempfile.mkdtemp(prefix="nexus_inttest_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]   # initdb creates a superuser with this name

    try:
        # 1. initdb — trust auth everywhere (no password prompts)
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )

        # 2. Configure port and TCP bind before starting
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")

        # 3. Start and WAIT until server is ready (-w flag)
        #    -o passes extra options to postmaster; -k sets the Unix socket dir.
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}",
             "start", "-w"],
            check=True, capture_output=True,
        )

        # 4. Create the test database (trust auth means no -W needed)
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexustest"],
            check=True, capture_output=True,
        )

        # 5. Bootstrap schema + service role in one psql call
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "-d", "nexustest",
             "-v", "ON_ERROR_STOP=1",
             "-c", _BOOTSTRAP_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"psql bootstrap failed (rc={proc.returncode}):\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )

        yield {"port": pg_port, "dbname": "nexustest", "user": pg_user, "pgdata": pgdata}

    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Launch the shaded JAR against the hermetic PG.

    Yields (base_url: str, token: str, proc: Popen).
    NX_SERVICE_TOKEN is also injected into the child env so the subprocess
    sees it; it is NOT set in the parent process (NX_STORAGE_BACKEND stays unset).
    """
    svc_port = _free_port()
    token    = "inttest-bearer-secret-xyz"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "svc_inttest",
        "NX_DB_PASS": "svc_inttest_pass",
        "NX_POOL_SIZE": "3",
    }
    # Remove any storage-backend override so the service uses its own PG
    env.pop("NX_STORAGE_BACKEND", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,   # put in its own process group for clean kill
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=30.0)
        yield f"http://127.0.0.1:{svc_port}", token, proc
    finally:
        # Kill the entire process group (JVM may spawn child threads)
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
    """HttpMemoryStore (tenant='default') connected to the real Java service.

    NX_SERVICE_TOKEN is set in the environment so the store constructor
    can read it (constructor reads env when base_url is provided and _token is None).
    """
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    base_url, token, _ = service
    # Pass token via private kwarg — constructor signature is _token for external use
    os.environ["NX_SERVICE_TOKEN"] = token
    s = HttpMemoryStore(base_url=base_url, tenant="default")
    yield s
    s.close()


@pytest.fixture(scope="module")
def other_store(service):
    """HttpMemoryStore for the cross-tenant RLS probe (tenant='other-tenant')."""
    from nexus.db.t2.http_memory_store import HttpMemoryStore
    base_url, token, _ = service
    os.environ["NX_SERVICE_TOKEN"] = token
    s = HttpMemoryStore(base_url=base_url, tenant="other-tenant")
    yield s
    s.close()


# ── Integration Tests ─────────────────────────────────────────────────────────

class TestRealService:
    """Exercises HttpMemoryStore against the real Java service + real Postgres 16."""

    def test_put_get_round_trip(self, store):
        """Basic put -> get round-trip through the real service."""
        row_id = store.put("inttest", "rt-1", "round trip content", ttl=1)
        assert isinstance(row_id, int) and row_id > 0

        entry = store.get(project="inttest", title="rt-1")
        assert entry is not None
        assert entry["content"] == "round trip content"
        assert entry["project"] == "inttest"
        assert entry["title"] == "rt-1"

    def test_tags_always_string_not_null(self, store):
        """Critical #2: untagged entry must have tags='' not None or missing key."""
        store.put("inttest", "tags-none", "no tags here", ttl=1)
        entry = store.get(project="inttest", title="tags-none")
        assert entry is not None
        assert "tags" in entry, "tags key must always be present in the response dict"
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
        """Significant #5: access_count must increment on every GET call."""
        store.put("inttest", "ac-test", "access count test", ttl=1)
        e1 = store.get(project="inttest", title="ac-test")
        e2 = store.get(project="inttest", title="ac-test")
        assert e1 is not None and e2 is not None
        assert e2["access_count"] == e1["access_count"] + 1, (
            f"expected access_count to increment: "
            f"{e1['access_count']} -> {e2['access_count']}"
        )

    def test_fts_exact_match(self, store):
        """FTS returns a result for an exact content word match."""
        store.put("inttest", "fts-exact", "chromadb semantic search engine", ttl=1)
        results = store.search("chromadb", project="inttest")
        titles = [r["title"] for r in results]
        assert "fts-exact" in titles, (
            f"FTS exact match failed — 'chromadb' not found. Got titles: {titles}"
        )

    def test_fts_stemming_probe(self, store):
        """Real FTS: 'run' must find content containing 'running' (english Snowball stemmer).

        Both 'run' and 'running' normalise to lexeme 'run' under the 'english' config.
        Verified: ts_lexize('english_stem', 'running') = {run}.
        This probe CANNOT be reproduced by the fake server (substring match only).
        It proves plainto_tsquery('english', ?) is in effect on the real PG.
        """
        store.put("inttest", "fts-stem",
                  "the daemon was running its event loop repeatedly", ttl=1)
        results = store.search("run", project="inttest")
        titles = [r["title"] for r in results]
        assert "fts-stem" in titles, (
            f"FTS stemming probe failed: plainto_tsquery('english','run') "
            f"should match 'running' (same stem) but did not. Got titles: {titles}"
        )

    def test_put_or_merge_insert_path(self, store):
        """Significant #6 (insert path): new entry returns action='inserted'."""
        # Clean slate — use a unique title not used by other tests
        rid, action = store.put_or_merge(
            "inttest", "pom-new",
            "the daemon processes semantic events efficiently",
            ttl=1, min_similarity=0.3,
        )
        assert action == "inserted", f"expected 'inserted' for new entry, got {action!r}"
        assert isinstance(rid, int) and rid > 0

    def test_put_or_merge_merge_path(self, store):
        """Significant #6 (merge path): overlapping content from a DIFFERENT title
        triggers server-side Jaccard merge into an existing entry.

        The server scan excludes same-title entries (those get a plain upsert).
        To exercise the merge code-path, we PUT a base entry under 'pom-base',
        then call put_or_merge with a DIFFERENT title 'pom-duplicate' whose
        content heavily overlaps 'pom-base'.  With min_similarity=0.3 and high
        word-overlap the server should merge 'pom-duplicate' into 'pom-base'.
        """
        # Step 1: insert the base entry with a unique title
        store.put(
            "inttest", "pom-base",
            "nexus daemon semantic search handles processing efficiently",
            ttl=1,
        )
        # Step 2: put_or_merge with DIFFERENT title but heavily overlapping content
        #         Words shared with base: nexus, daemon, semantic, search, handles, efficiently
        #         That gives |inter|/|union| well above 0.3 -> should trigger merge
        _, action2 = store.put_or_merge(
            "inttest", "pom-duplicate",
            "nexus daemon semantic search handles everything efficiently",
            ttl=1, min_similarity=0.3,
        )
        assert action2 == "merged", (
            f"expected 'merged': a different-title entry with high Jaccard overlap "
            f"should be merged server-side into the existing entry. got {action2!r}"
        )

    def test_delete_removes_entry(self, store):
        """put -> delete -> get returns None."""
        store.put("inttest", "del-test", "to be deleted", ttl=1)
        deleted = store.delete(project="inttest", title="del-test")
        assert deleted is True
        entry = store.get(project="inttest", title="del-test")
        assert entry is None, f"entry should be gone after delete but got: {entry}"

    def test_cross_tenant_rls_negative(self, store, other_store):
        """Tenant A's rows must be invisible to tenant B (RLS USING policy).

        This exercises real Postgres RLS FORCE — the fake server cannot reproduce this.
        """
        store.put("rls-proj", "rls-secret", "tenant A private content", ttl=1)

        # Tenant B must NOT see tenant A's row
        entry = other_store.get(project="rls-proj", title="rls-secret")
        assert entry is None, (
            f"RLS FAILED: tenant B can read tenant A's entry: {entry}"
        )

        # Tenant A can still see its own row
        own_entry = store.get(project="rls-proj", title="rls-secret")
        assert own_entry is not None

    def test_cross_tenant_write_isolation(self, store, other_store):
        """Tenant B's write to the same (project, title) lands in B's namespace,
        not overwriting A's row (RLS WITH CHECK policy, separate unique constraint key).
        """
        store.put("rls-proj", "rls-write", "tenant A original content", ttl=1)

        # Tenant B writes to same logical (project, title) — goes into B's namespace
        other_store.put("rls-proj", "rls-write", "tenant B attempted overwrite", ttl=1)

        # Tenant A must still see its own, unmodified content
        a_entry = store.get(project="rls-proj", title="rls-write")
        assert a_entry is not None
        assert a_entry["content"] == "tenant A original content", (
            f"tenant A's content was overwritten! got: {a_entry['content']!r}"
        )

    def test_search_silent_does_not_increment_access_count(self, store):
        """search(access='silent') must NOT increment access_count."""
        store.put("inttest", "search-silent", "search tracking content test", ttl=1)

        # First get to establish baseline (increments once)
        e_before = store.get(project="inttest", title="search-silent")
        assert e_before is not None
        count_before = e_before["access_count"]

        # Silent search — must not track
        store.search("tracking", project="inttest", access="silent")

        # Second get (increments once more)
        e_after = store.get(project="inttest", title="search-silent")
        assert e_after is not None

        # The only increment since count_before should be the e_after get() call itself,
        # not the silent search.
        assert e_after["access_count"] == count_before + 1, (
            f"silent search must not increment access_count beyond the get() call: "
            f"before={count_before}, after={e_after['access_count']} "
            f"(expected {count_before + 1})"
        )
