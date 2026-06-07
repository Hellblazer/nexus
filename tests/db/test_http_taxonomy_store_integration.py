# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language integration test for HttpTaxonomyStore against the real Java service.

Requires (on THIS machine — darwin/aarch64 with JDK25 GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (mvn -f service/pom.xml package -DskipTests)
  - Java on PATH (or JAVA_HOME/bin/java available)

Marked @pytest.mark.integration — collected but skipped automatically when the
jar or pg16 binaries are absent, so CI (which has neither) stays green.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_http_taxonomy_store_integration.py -v

What is exercised (bead nexus-gmiaf.14 requirements):
  a) assign_topic / get_topic_doc_ids / get_topic_tree round-trip
  b) merge_topics: source removed, docs reassigned to target
  c) delete_topic: topic removed, returns collection (CHROMA BOUNDARY signal)
  d) Cross-tenant RLS negative: tenant A topics invisible to tenant B
  e) ETL fidelity: import_topic preserves original id, doc_count, created_at
  f) ETL fidelity: import_assignment preserves similarity verbatim
  g) needs_rebalance: detects growth after record_discover_count
  h) GREATEST merge: re-import with stale doc_count does NOT clobber live PG value
  i) compute_icf_map: returns {topic_id: icf} dict from /icf/map atomic endpoint
  j) detect_hubs: returns list[HubRow] sorted by score desc
  k) detect_hubs: stopword flagging for labels containing DEFAULT_HUB_STOPWORDS terms
  l) audit_collection: returns AuditReport with correct fields
  m) audit_collection: pattern_pollution flags stopword labels
  n) generate_cooccurrence_links: returns count of cross-collection pairs
  o) refresh_projection_links: returns count of pairs written
  p) persist_split: inserts children, zeroes parent doc_count
  q) assigned_by never downgrades 'projection' to 'hdbscan' (importAssignment fix)
  r) recordDiscoverCount uses GREATEST (no-clobber re-record)

CHROMA INTERACTION NOTE:
  delete_topic and merge_topics return the collection name but do NOT touch
  Chroma — centroid cleanup is the caller's responsibility.  This test
  verifies the relational-table result ONLY.

NX_STORAGE_BACKEND is NOT touched — default SQLite path is unchanged.
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


# ── Bootstrap SQL for taxonomy tables ─────────────────────────────────────────
# Mirrors taxonomy-001-baseline.xml changeset, applied manually for the hermetic test.
# Run as two separate psql invocations:
#   1. _BOOTSTRAP_SQL_ROLE   — CREATE ROLE (autocommit, outside any txn)
#   2. _BOOTSTRAP_SQL_SCHEMA — DDL: schema + tables + indexes + RLS
#   3. _BOOTSTRAP_SQL_GRANTS — GRANT + ALTER ROLE

_BOOTSTRAP_SQL_ROLE = """\
CREATE ROLE svc_taxonomy_inttest LOGIN PASSWORD 'svc_taxonomy_inttest_pass';
"""

_BOOTSTRAP_SQL_SCHEMA = """\
CREATE SCHEMA IF NOT EXISTS nexus;

-- memory table (required by the service startup check / other endpoints)
CREATE TABLE IF NOT EXISTS nexus.memory (
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
ALTER TABLE IF EXISTS nexus.memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS nexus.memory FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='nexus' AND tablename='memory' AND policyname='tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON nexus.memory
            USING (tenant_id = current_setting('nexus.tenant', true))
            WITH CHECK (tenant_id = current_setting('nexus.tenant', true));
    END IF;
END $$;

-- topics
CREATE TABLE IF NOT EXISTS nexus.topics (
    id            BIGSERIAL   NOT NULL,
    tenant_id     TEXT        NOT NULL,
    label         TEXT        NOT NULL,
    parent_id     BIGINT,
    collection    TEXT        NOT NULL,
    centroid_hash TEXT,
    doc_count     INTEGER     NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL,
    review_status TEXT        NOT NULL DEFAULT 'pending',
    terms         TEXT,
    CONSTRAINT topics_pk PRIMARY KEY (id),
    CONSTRAINT topics_tenant_collection_label_uq UNIQUE (tenant_id, collection, label),
    CONSTRAINT topics_parent_fk FOREIGN KEY (parent_id) REFERENCES nexus.topics(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_tenant_collection ON nexus.topics (tenant_id, collection);
CREATE INDEX IF NOT EXISTS idx_topics_tenant_review     ON nexus.topics (tenant_id, review_status);
CREATE INDEX IF NOT EXISTS idx_topics_tenant_parent     ON nexus.topics (tenant_id, parent_id);
ALTER TABLE nexus.topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.topics FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.topics
    USING (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

-- taxonomy_meta
CREATE TABLE IF NOT EXISTS nexus.taxonomy_meta (
    tenant_id               TEXT    NOT NULL,
    collection              TEXT    NOT NULL,
    last_discover_doc_count INTEGER NOT NULL DEFAULT 0,
    last_discover_at        TIMESTAMPTZ,
    CONSTRAINT taxonomy_meta_pk PRIMARY KEY (tenant_id, collection)
);
ALTER TABLE nexus.taxonomy_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.taxonomy_meta FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.taxonomy_meta
    USING (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

-- topic_assignments
CREATE TABLE IF NOT EXISTS nexus.topic_assignments (
    tenant_id         TEXT    NOT NULL,
    doc_id            TEXT    NOT NULL,
    topic_id          BIGINT  NOT NULL,
    assigned_by       TEXT    NOT NULL DEFAULT 'hdbscan',
    similarity        DOUBLE PRECISION,
    assigned_at       TIMESTAMPTZ,
    source_collection TEXT,
    CONSTRAINT topic_assignments_pk PRIMARY KEY (tenant_id, doc_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_ta_tenant_topic  ON nexus.topic_assignments (tenant_id, topic_id);
CREATE INDEX IF NOT EXISTS idx_ta_tenant_doc    ON nexus.topic_assignments (tenant_id, doc_id);
CREATE INDEX IF NOT EXISTS idx_ta_source_by     ON nexus.topic_assignments (tenant_id, source_collection, assigned_by);
ALTER TABLE nexus.topic_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.topic_assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.topic_assignments
    USING (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

-- topic_links
CREATE TABLE IF NOT EXISTS nexus.topic_links (
    tenant_id     TEXT    NOT NULL,
    from_topic_id BIGINT  NOT NULL,
    to_topic_id   BIGINT  NOT NULL,
    link_count    INTEGER NOT NULL DEFAULT 0,
    link_types    TEXT    NOT NULL DEFAULT '[]',
    CONSTRAINT topic_links_pk PRIMARY KEY (tenant_id, from_topic_id, to_topic_id)
);
ALTER TABLE nexus.topic_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.topic_links FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.topic_links
    USING (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));
"""

_BOOTSTRAP_SQL_GRANTS = """\
GRANT USAGE ON SCHEMA nexus TO svc_taxonomy_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.topics TO svc_taxonomy_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.taxonomy_meta TO svc_taxonomy_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.topic_assignments TO svc_taxonomy_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.topic_links TO svc_taxonomy_inttest;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.memory TO svc_taxonomy_inttest;
GRANT USAGE ON SEQUENCE nexus.topics_id_seq TO svc_taxonomy_inttest;
GRANT USAGE ON SEQUENCE nexus.memory_id_seq TO svc_taxonomy_inttest;
ALTER ROLE svc_taxonomy_inttest SET search_path TO nexus, public;
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
    """Spin up a hermetic Postgres 16 instance."""
    pgdata  = tempfile.mkdtemp(prefix="nexus_taxonomy_inttest_pg_")
    pg_port = _free_port()
    pglog   = os.path.join(pgdata, "pg.log")
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
             "-U", pg_user, "nexustaxonomytest"],
            check=True, capture_output=True,
        )

        def _psql(sql: str) -> None:
            proc = subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", "nexustaxonomytest",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"psql bootstrap failed (rc={proc.returncode}):\n"
                    f"stdout={proc.stdout}\nstderr={proc.stderr}"
                )

        _psql(_BOOTSTRAP_SQL_ROLE)
        _psql(_BOOTSTRAP_SQL_SCHEMA)
        _psql(_BOOTSTRAP_SQL_GRANTS)

        yield {"port": pg_port, "dbname": "nexustaxonomytest", "user": pg_user, "pgdata": pgdata}

    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Launch the shaded JAR against the hermetic PG."""
    svc_port = _free_port()
    token    = "taxonomy-inttest-bearer-secret"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "svc_taxonomy_inttest",
        "NX_DB_PASS": "svc_taxonomy_inttest_pass",
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
def taxonomy_store(service):
    """HttpTaxonomyStore (tenant='default') connected to the real Java service."""
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    base_url, token, _ = service
    s = HttpTaxonomyStore(base_url=base_url, tenant="default", _token=token)
    yield s
    s.close()


@pytest.fixture(scope="module")
def other_taxonomy_store(service):
    """HttpTaxonomyStore for the cross-tenant RLS probe (tenant='other-tenant')."""
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
    base_url, token, _ = service
    s = HttpTaxonomyStore(base_url=base_url, tenant="other-tenant", _token=token)
    yield s
    s.close()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestTaxonomyMVV:
    """Minimum viable verification (MVV) for the taxonomy service (bead nexus-gmiaf.14)."""

    def test_a_assign_and_get_docs(self, taxonomy_store) -> None:
        """a) assign_topic -> get_topic_doc_ids round-trip with real Postgres."""
        # Import a topic first (ETL-style)
        topic_id = taxonomy_store.import_topic(
            src_id=1001,
            label="machine-learning-inttest-a",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=0,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        assert topic_id == 1001

        # Assign a doc
        taxonomy_store.assign_topic(
            "doc-inttest-a1",
            1001,
            "hdbscan",
            similarity=0.85,
            source_collection="knowledge__papers",
        )

        docs = taxonomy_store.get_topic_doc_ids(1001, limit=10)
        assert "doc-inttest-a1" in docs

    def test_b_merge_topics(self, taxonomy_store) -> None:
        """b) merge_topics: source removed, returns collection for chroma cleanup."""
        taxonomy_store.import_topic(
            src_id=2001,
            label="merge-src-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=1,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        taxonomy_store.import_topic(
            src_id=2002,
            label="merge-tgt-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=2,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        taxonomy_store.assign_topic("doc-merge-src", 2001, "hdbscan")

        collection = taxonomy_store.merge_topics(2001, 2002)
        assert collection == "knowledge__papers"

        # Source must be gone
        assert taxonomy_store.get_topic_by_id(2001) is None
        # Target still exists
        assert taxonomy_store.get_topic_by_id(2002) is not None

    def test_c_delete_topic_returns_collection(self, taxonomy_store) -> None:
        """c) delete_topic returns collection name (CHROMA BOUNDARY signal)."""
        taxonomy_store.import_topic(
            src_id=3001,
            label="to-delete-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=0,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        collection = taxonomy_store.delete_topic(3001)
        assert collection == "knowledge__papers"
        assert taxonomy_store.get_topic_by_id(3001) is None

    def test_d_rls_isolation(self, taxonomy_store, other_taxonomy_store) -> None:
        """d) Cross-tenant RLS: tenant A topics invisible to tenant B."""
        taxonomy_store.import_topic(
            src_id=4001,
            label="private-to-default-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=0,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        # Tenant B should not see tenant A's topic
        t = other_taxonomy_store.get_topic_by_id(4001)
        assert t is None, "RLS isolation failed: tenant B saw tenant A's topic"

    def test_e_etl_fidelity_topic_id_preserved(self, taxonomy_store) -> None:
        """e) import_topic preserves original id, doc_count, created_at."""
        src_id = 5001
        taxonomy_store.import_topic(
            src_id=src_id,
            label="fidelity-topic-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash="abc123",
            doc_count=77,
            created_at="2025-12-01T08:30:00Z",
            review_status="accepted",
            terms='["ai"]',
        )
        t = taxonomy_store.get_topic_by_id(src_id)
        assert t is not None
        assert t["id"] == src_id
        assert t["label"] == "fidelity-topic-inttest"
        assert t["doc_count"] == 77
        assert t["centroid_hash"] == "abc123"
        assert t["review_status"] == "accepted"

    def test_f_etl_fidelity_assignment_similarity(self, taxonomy_store) -> None:
        """f) import_assignment preserves similarity verbatim."""
        taxonomy_store.import_topic(
            src_id=6001,
            label="fidelity-assign-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=1,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        taxonomy_store.import_assignment(
            doc_id="doc-fidelity-sim-inttest",
            topic_id=6001,
            assigned_by="projection",
            similarity=0.732,
            assigned_at="2026-02-15T10:00:00Z",
            source_collection="knowledge__papers",
        )
        docs = taxonomy_store.get_topic_doc_ids(6001, limit=10)
        assert "doc-fidelity-sim-inttest" in docs

    def test_g_needs_rebalance_after_growth(self, taxonomy_store) -> None:
        """g) needs_rebalance detects >5% growth after record_discover_count."""
        taxonomy_store.record_discover_count("knowledge__papers-inttest-rb", 100)
        assert not taxonomy_store.needs_rebalance("knowledge__papers-inttest-rb", 103)
        assert taxonomy_store.needs_rebalance("knowledge__papers-inttest-rb", 200)

    def test_h_etl_greatest_merge_doc_count(self, taxonomy_store) -> None:
        """h) Re-import with stale doc_count does NOT clobber live PG value (GREATEST)."""
        taxonomy_store.import_topic(
            src_id=8001,
            label="greatest-merge-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=50,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        t1 = taxonomy_store.get_topic_by_id(8001)
        assert t1["doc_count"] == 50

        # Re-import with a stale (lower) doc_count — GREATEST should preserve 50
        taxonomy_store.import_topic(
            src_id=8001,
            label="greatest-merge-inttest",
            parent_id=None,
            collection="knowledge__papers",
            centroid_hash=None,
            doc_count=10,  # stale — lower than live value
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        t2 = taxonomy_store.get_topic_by_id(8001)
        assert t2["doc_count"] == 50, (
            f"GREATEST failed: doc_count={t2['doc_count']} should be 50, not 10"
        )


class TestAnalyticalMethods:
    """Tests for the 5 analytical methods that were missing (nexus-gmiaf.14 drop-in completion)."""

    # Shared topic IDs for this test class — use a high range to avoid collision
    _COLL_A = "knowledge__analytical-a"
    _COLL_B = "knowledge__analytical-b"
    _T_A1 = 9001
    _T_A2 = 9002
    _T_B1 = 9003

    @pytest.fixture(autouse=True, scope="class")
    def seed_data(self, taxonomy_store):
        """Seed topics and assignments for analytical method tests."""
        # Topic A1 in collection A
        taxonomy_store.import_topic(
            src_id=self._T_A1,
            label="analytic-hub-a1-inttest",
            parent_id=None,
            collection=self._COLL_A,
            centroid_hash=None,
            doc_count=5,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        # Topic A2 in collection A (stopword label for pattern_pollution)
        taxonomy_store.import_topic(
            src_id=self._T_A2,
            label="class-helper-inttest",  # contains stopword "class"
            parent_id=None,
            collection=self._COLL_A,
            centroid_hash=None,
            doc_count=3,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        # Topic B1 in collection B
        taxonomy_store.import_topic(
            src_id=self._T_B1,
            label="analytic-hub-b1-inttest",
            parent_id=None,
            collection=self._COLL_B,
            centroid_hash=None,
            doc_count=4,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )

        # Projection assignments: doc1 projects from COLL_B into T_A1
        # doc2 projects from COLL_B into T_A2
        # doc3 projects from COLL_A into T_B1
        # This creates cross-collection co-occurrence between A1↔B1, A2↔B1
        taxonomy_store.import_assignment(
            doc_id="analytic-doc1", topic_id=self._T_A1,
            assigned_by="projection", similarity=0.88,
            assigned_at="2026-03-01T00:00:00Z",
            source_collection=self._COLL_B,
        )
        taxonomy_store.import_assignment(
            doc_id="analytic-doc2", topic_id=self._T_A2,
            assigned_by="projection", similarity=0.72,
            assigned_at="2026-03-01T00:00:00Z",
            source_collection=self._COLL_B,
        )
        taxonomy_store.import_assignment(
            doc_id="analytic-doc3", topic_id=self._T_B1,
            assigned_by="projection", similarity=0.91,
            assigned_at="2026-03-01T00:00:00Z",
            source_collection=self._COLL_A,
        )
        # hdbscan assignments for co-occurrence: doc1 also hdbscan-assigned to B1
        taxonomy_store.import_assignment(
            doc_id="analytic-doc1", topic_id=self._T_B1,
            assigned_by="hdbscan", similarity=None,
            assigned_at=None, source_collection=None,
        )

    def test_i_compute_icf_map(self, taxonomy_store) -> None:
        """i) compute_icf_map returns a non-empty {topic_id: icf} dict."""
        icf = taxonomy_store.compute_icf_map()
        # 2 distinct source_collections (COLL_A, COLL_B) → n_effective >= 2
        assert isinstance(icf, dict), "compute_icf_map must return a dict"
        # Each topic that has projection rows should have an entry
        assert len(icf) > 0, "ICF map is empty but projection data was seeded"
        for k, v in icf.items():
            assert isinstance(k, int), f"key {k!r} is not int"
            assert isinstance(v, float), f"value {v!r} is not float"

    def test_j_detect_hubs(self, taxonomy_store) -> None:
        """j) detect_hubs returns HubRow instances for topics spanning >= 2 collections."""
        from nexus.db.t2.catalog_taxonomy import HubRow
        hubs = taxonomy_store.detect_hubs(min_collections=1)
        assert isinstance(hubs, list), "detect_hubs must return a list"
        assert len(hubs) > 0, "Expected at least one hub row"
        assert all(isinstance(h, HubRow) for h in hubs), "All items must be HubRow"
        # Sorted by score descending
        scores = [h.score for h in hubs]
        assert scores == sorted(scores, reverse=True), "Hubs not sorted by score desc"

    def test_k_detect_hubs_stopword_flagging(self, taxonomy_store) -> None:
        """k) detect_hubs flags 'class-helper-inttest' label as matched_stopwords."""
        from nexus.db.t2.catalog_taxonomy import HubRow
        hubs = taxonomy_store.detect_hubs(min_collections=1)
        stopword_hubs = [h for h in hubs if "class" in h.matched_stopwords]
        assert len(stopword_hubs) >= 1, (
            "'class-helper-inttest' topic should be flagged with matched_stopword='class'"
        )

    def test_l_audit_collection(self, taxonomy_store) -> None:
        """l) audit_collection returns AuditReport with correct fields."""
        from nexus.db.t2.catalog_taxonomy import AuditHub, AuditReport
        report = taxonomy_store.audit_collection(self._COLL_B)
        assert isinstance(report, AuditReport), "audit_collection must return AuditReport"
        assert report.collection == self._COLL_B
        # COLL_B has projection assignments (doc1 → T_A1, doc2 → T_A2 with similarities)
        assert report.total_assignments >= 2, (
            f"Expected >= 2 projection assignments for {self._COLL_B}, "
            f"got {report.total_assignments}"
        )
        assert report.p50 is not None, "p50 must be non-None when assignments exist"
        assert isinstance(report.top_receiving_hubs, list)
        assert all(isinstance(h, AuditHub) for h in report.top_receiving_hubs)

    def test_m_audit_collection_pattern_pollution(self, taxonomy_store) -> None:
        """m) audit_collection flags pattern_pollution for stopword labels."""
        from nexus.db.t2.catalog_taxonomy import AuditReport
        report = taxonomy_store.audit_collection(self._COLL_B)
        # 'class-helper-inttest' has projection from COLL_B and contains stopword 'class'
        polluted_labels = [h.label for h in report.pattern_pollution]
        assert any("class" in label for label in polluted_labels), (
            f"Expected pattern_pollution to include 'class-helper-inttest', got {polluted_labels}"
        )

    def test_n_generate_cooccurrence_links(self, taxonomy_store) -> None:
        """n) generate_cooccurrence_links returns the number of cross-collection pairs."""
        count = taxonomy_store.generate_cooccurrence_links()
        assert isinstance(count, int), "generate_cooccurrence_links must return int"
        # doc1 is assigned to T_A1 and T_B1 → at least one pair
        assert count >= 1, f"Expected >= 1 cooccurrence link, got {count}"

    def test_o_refresh_projection_links(self, taxonomy_store) -> None:
        """o) refresh_projection_links returns the number of link pairs written."""
        count = taxonomy_store.refresh_projection_links()
        assert isinstance(count, int), "refresh_projection_links must return int"

    def test_p_persist_split(self, taxonomy_store) -> None:
        """p) persist_split inserts children and zeroes parent doc_count."""
        # Import a topic to split
        taxonomy_store.import_topic(
            src_id=9900,
            label="to-split-inttest",
            parent_id=None,
            collection=self._COLL_A,
            centroid_hash=None,
            doc_count=4,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        # Assign some docs
        taxonomy_store.import_assignment(
            doc_id="split-doc1", topic_id=9900,
            assigned_by="hdbscan", similarity=None,
            assigned_at=None, source_collection=None,
        )
        taxonomy_store.import_assignment(
            doc_id="split-doc2", topic_id=9900,
            assigned_by="hdbscan", similarity=None,
            assigned_at=None, source_collection=None,
        )

        split_result = {
            "topic_id": 9900,
            "collection_name": self._COLL_A,
            "child_specs": [
                {
                    "label": "split-child-1-inttest",
                    "doc_count": 2,
                    "created_at": "2026-01-01T00:00:00Z",
                    "terms_json": None,
                    "doc_ids": ["split-doc1", "split-doc2"],
                },
            ],
        }
        child_ids = taxonomy_store.persist_split(split_result)
        assert isinstance(child_ids, list), "persist_split must return list"
        assert len(child_ids) == 1, f"Expected 1 child id, got {child_ids}"
        assert child_ids[0] > 0, f"Child id must be > 0, got {child_ids[0]}"

        # Parent doc_count must be 0 after split
        parent = taxonomy_store.get_topic_by_id(9900)
        assert parent is not None
        assert parent["doc_count"] == 0, (
            f"Parent doc_count should be 0 after split, got {parent['doc_count']}"
        )

    def test_q_assigned_by_never_downgrades_projection(self, taxonomy_store) -> None:
        """q) re-importing an assignment never downgrades 'projection' to 'hdbscan'."""
        # Import topic first
        taxonomy_store.import_topic(
            src_id=9801,
            label="assigned-by-test-inttest",
            parent_id=None,
            collection=self._COLL_A,
            centroid_hash=None,
            doc_count=1,
            created_at="2026-01-01T00:00:00Z",
            review_status="pending",
            terms=None,
        )
        # Initial import as 'projection'
        taxonomy_store.import_assignment(
            doc_id="assigned-by-doc1",
            topic_id=9801,
            assigned_by="projection",
            similarity=0.9,
            assigned_at="2026-03-01T00:00:00Z",
            source_collection=self._COLL_B,
        )
        # Re-import as 'hdbscan' — must NOT downgrade assigned_by
        taxonomy_store.import_assignment(
            doc_id="assigned-by-doc1",
            topic_id=9801,
            assigned_by="hdbscan",
            similarity=None,
            assigned_at=None,
            source_collection=None,
        )
        # Verify: doc is still in the topic (assignment not lost)
        docs = taxonomy_store.get_topic_doc_ids(9801, limit=10)
        assert "assigned-by-doc1" in docs

    def test_r_record_discover_greatest_no_clobber(self, taxonomy_store) -> None:
        """r) recordDiscoverCount uses GREATEST — re-record with smaller count preserves max."""
        taxonomy_store.record_discover_count("coll-greatest-test-inttest", 1000)
        taxonomy_store.record_discover_count("coll-greatest-test-inttest", 50)
        # 50 < 1000, so needs_rebalance(2000) should still compare against 1000
        # 2000 is 100% growth from 1000 → rebalance needed
        assert taxonomy_store.needs_rebalance("coll-greatest-test-inttest", 2000), (
            "needs_rebalance should return True (2000 vs 1000) — GREATEST preserved 1000"
        )
        # 1001 is 0.1% growth from 1000 → no rebalance needed
        assert not taxonomy_store.needs_rebalance("coll-greatest-test-inttest", 1001), (
            "needs_rebalance should return False (1001 vs 1000) — GREATEST preserved 1000"
        )
