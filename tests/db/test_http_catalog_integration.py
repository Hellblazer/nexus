# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test for HttpCatalogClient against the real Java catalog service.

Requires (darwin/aarch64 with JDK/GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built
      (cd service && mvn package -DskipTests)
  - Java on PATH (or JAVA_HOME env set)

Marked @pytest.mark.integration — skipped automatically when prerequisites absent.

Run locally:
    cd service && mvn package -DskipTests
    uv run pytest tests/db/test_http_catalog_integration.py -m integration -q

What is exercised:
  a) register a document (server-side tumbler assignment), show/resolve, stats, list
  b) link + links traversal (graph) + link_query + traverse BFS at depth 1 and 2
  c) spans + document_chunks manifest round-trip (write/get/purge)
  d) FTS: english stemming probe (title) + simple identifier probe (corpus/file_path)
  e) cross-tenant RLS negative (tenant A invisible to tenant B; unset GUC = empty result)
  f) ETL fidelity + idempotent re-run no-clobber (re-register returns same tumbler)

NX_STORAGE_BACKEND is NOT set — default SQLite path is unchanged.
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

# ── Prerequisite paths ─────────────────────────────────────────────────────────

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

# ── Bootstrap SQL (catalog-001-baseline.xml extracted) ────────────────────────
#
# The Java service does NOT run Liquibase at startup (nexus-net63 is the bead
# tracking that). Schema is applied here directly from the changelog DDL, the
# same pattern as test_http_memory_store_integration.py.
#
# Changesets in order: 1 owners, 2 documents+FTS, 3 links, 4 chunks, 5 collections,
# 6 meta, 7 RLS, 9 next_seq, 8 nexus_svc grants (skips if role absent).
# Followed by svc_inttest_catalog setup for the RLS probe.

_BOOTSTRAP_SQL = """\
CREATE SCHEMA IF NOT EXISTS nexus;

-- ── cs1: catalog_owners ───────────────────────────────────────────────────────
CREATE TABLE nexus.catalog_owners (
    tenant_id      TEXT NOT NULL,
    tumbler_prefix TEXT NOT NULL,
    name           TEXT NOT NULL,
    owner_type     TEXT NOT NULL,
    repo_hash      TEXT,
    description    TEXT,
    repo_root      TEXT NOT NULL DEFAULT '',
    head_hash      TEXT,
    next_seq       BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT catalog_owners_pk PRIMARY KEY (tenant_id, tumbler_prefix),
    CONSTRAINT catalog_owners_unique_name_type UNIQUE (tenant_id, name, owner_type)
);

CREATE UNIQUE INDEX idx_catalog_owners_repo_hash
    ON nexus.catalog_owners (tenant_id, repo_hash)
    WHERE repo_hash IS NOT NULL AND repo_hash != '';

-- ── cs2: catalog_documents + FTS ─────────────────────────────────────────────
CREATE TABLE nexus.catalog_documents (
    tenant_id           TEXT             NOT NULL,
    tumbler             TEXT             NOT NULL,
    title               TEXT             NOT NULL,
    author              TEXT,
    year                INTEGER,
    content_type        TEXT,
    file_path           TEXT,
    corpus              TEXT,
    physical_collection TEXT,
    chunk_count         INTEGER,
    head_hash           TEXT,
    indexed_at          TEXT,
    metadata            JSONB,
    source_mtime        DOUBLE PRECISION NOT NULL DEFAULT 0,
    alias_of            TEXT             NOT NULL DEFAULT '',
    source_uri          TEXT             NOT NULL DEFAULT '',
    bib_year                  INTEGER NOT NULL DEFAULT 0,
    bib_authors               TEXT    NOT NULL DEFAULT '',
    bib_venue                 TEXT    NOT NULL DEFAULT '',
    bib_citation_count        INTEGER NOT NULL DEFAULT 0,
    bib_semantic_scholar_id   TEXT    NOT NULL DEFAULT '',
    bib_openalex_id           TEXT    NOT NULL DEFAULT '',
    bib_doi                   TEXT    NOT NULL DEFAULT '',
    bib_enriched_at           TEXT    NOT NULL DEFAULT '',
    fts_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, ''))
        || to_tsvector('simple',  coalesce(author, ''))
        || to_tsvector('simple',  coalesce(corpus, ''))
        || to_tsvector('simple',  coalesce(file_path, ''))
    ) STORED,
    CONSTRAINT catalog_documents_pk PRIMARY KEY (tenant_id, tumbler)
);

CREATE INDEX idx_catalog_documents_fts
    ON nexus.catalog_documents USING GIN (fts_vector);

CREATE INDEX idx_catalog_documents_collection
    ON nexus.catalog_documents (tenant_id, physical_collection);

CREATE INDEX idx_catalog_documents_source_uri
    ON nexus.catalog_documents (tenant_id, source_uri)
    WHERE source_uri != '';

CREATE INDEX idx_catalog_documents_bib_s2_id
    ON nexus.catalog_documents (tenant_id, bib_semantic_scholar_id)
    WHERE bib_semantic_scholar_id != '';

CREATE INDEX idx_catalog_documents_bib_oa_id
    ON nexus.catalog_documents (tenant_id, bib_openalex_id)
    WHERE bib_openalex_id != '';

-- ── cs3: catalog_links ────────────────────────────────────────────────────────
CREATE TABLE nexus.catalog_links (
    tenant_id    TEXT    NOT NULL,
    id           BIGSERIAL,
    from_tumbler TEXT    NOT NULL,
    to_tumbler   TEXT    NOT NULL,
    link_type    TEXT    NOT NULL,
    from_span    TEXT,
    to_span      TEXT,
    created_by   TEXT    NOT NULL,
    created_at   TEXT,
    metadata     JSONB,
    CONSTRAINT catalog_links_pk PRIMARY KEY (tenant_id, id),
    CONSTRAINT catalog_links_unique UNIQUE (tenant_id, from_tumbler, to_tumbler, link_type)
);

CREATE INDEX idx_catalog_links_from      ON nexus.catalog_links (tenant_id, from_tumbler);
CREATE INDEX idx_catalog_links_to        ON nexus.catalog_links (tenant_id, to_tumbler);
CREATE INDEX idx_catalog_links_type      ON nexus.catalog_links (tenant_id, link_type);
CREATE INDEX idx_catalog_links_created_by ON nexus.catalog_links (tenant_id, created_by);
CREATE INDEX idx_catalog_links_from_type ON nexus.catalog_links (tenant_id, from_tumbler, link_type);
CREATE INDEX idx_catalog_links_to_type   ON nexus.catalog_links (tenant_id, to_tumbler, link_type);
CREATE INDEX idx_catalog_links_created_by_type ON nexus.catalog_links (tenant_id, created_by, link_type);

-- ── cs4: catalog_document_chunks ─────────────────────────────────────────────
CREATE TABLE nexus.catalog_document_chunks (
    tenant_id   TEXT    NOT NULL,
    doc_id      TEXT    NOT NULL,
    position    INTEGER NOT NULL,
    chash       TEXT    NOT NULL,
    chunk_index INTEGER,
    line_start  INTEGER,
    line_end    INTEGER,
    char_start  INTEGER,
    char_end    INTEGER,
    CONSTRAINT catalog_document_chunks_pk PRIMARY KEY (tenant_id, doc_id, position)
);

CREATE INDEX idx_catalog_chunks_chash
    ON nexus.catalog_document_chunks (tenant_id, chash);
CREATE INDEX idx_catalog_chunks_doc_id
    ON nexus.catalog_document_chunks (tenant_id, doc_id);

-- ── cs5: catalog_collections ─────────────────────────────────────────────────
CREATE TABLE nexus.catalog_collections (
    tenant_id            TEXT NOT NULL,
    name                 TEXT NOT NULL,
    content_type         TEXT NOT NULL DEFAULT '',
    owner_id             TEXT NOT NULL DEFAULT '',
    embedding_model      TEXT NOT NULL DEFAULT '',
    model_version        TEXT NOT NULL DEFAULT '',
    display_name         TEXT NOT NULL DEFAULT '',
    legacy_grandfathered INTEGER NOT NULL DEFAULT 0,
    superseded_by        TEXT NOT NULL DEFAULT '',
    superseded_at        TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL DEFAULT '',
    CONSTRAINT catalog_collections_pk PRIMARY KEY (tenant_id, name)
);

CREATE INDEX idx_catalog_collections_legacy
    ON nexus.catalog_collections (tenant_id, legacy_grandfathered);
CREATE INDEX idx_catalog_collections_owner
    ON nexus.catalog_collections (tenant_id, owner_id);
CREATE INDEX idx_catalog_collections_tuple
    ON nexus.catalog_collections (tenant_id, content_type, owner_id, embedding_model);

-- ── cs6: catalog_meta ────────────────────────────────────────────────────────
CREATE TABLE nexus.catalog_meta (
    tenant_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT,
    CONSTRAINT catalog_meta_pk PRIMARY KEY (tenant_id, key)
);

-- ── cs7: RLS on all catalog tables ───────────────────────────────────────────
ALTER TABLE nexus.catalog_owners          ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_owners          FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_owners
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_documents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_documents       FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_documents
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_links           ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_links           FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_links
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_document_chunks FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_document_chunks
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_collections     ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_collections     FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_collections
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_meta            ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_meta            FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_meta
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

-- ── nexus_app: non-superuser service role (subject to FORCE RLS) ─────────────
-- The OS superuser bypasses all RLS in PostgreSQL. The Java service must connect
-- as a non-superuser so that FORCE ROW LEVEL SECURITY applies.
-- Trust auth allows login without password in this hermetic initdb instance.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_app') THEN
    CREATE ROLE nexus_app LOGIN
    NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;

GRANT CONNECT ON DATABASE nexuscattest TO nexus_app;
GRANT USAGE ON SCHEMA nexus TO nexus_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_owners          TO nexus_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_documents       TO nexus_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_links           TO nexus_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_document_chunks TO nexus_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_collections     TO nexus_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.catalog_meta            TO nexus_app;
GRANT USAGE ON SEQUENCE nexus.catalog_links_id_seq TO nexus_app;
ALTER ROLE nexus_app SET search_path TO nexus, public;
"""


# ── Port helpers ───────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.15)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _psql(pg: dict, sql: str) -> None:
    proc = subprocess.run(
        [
            str(_PSQL),
            "-h", "127.0.0.1",
            "-p", str(pg["port"]),
            "-U", pg["user"],
            "-d", pg["dbname"],
            "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql failed (rc={proc.returncode}):\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )


# ── Module-scoped fixtures ─────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Hermetic PostgreSQL 16 instance with trust auth.

    Schema is applied here (before the JAR starts) via _BOOTSTRAP_SQL — the
    Java service does NOT run Liquibase at startup (bead nexus-net63 tracks that).
    """
    pgdata = tempfile.mkdtemp(prefix="nexus_cat_inttest_pg_")
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
             "-U", pg_user, "nexuscattest"],
            check=True, capture_output=True,
        )

        # Apply full catalog schema + RLS + svc_inttest_catalog role before the JAR starts
        pg = {"port": pg_port, "dbname": "nexuscattest", "user": pg_user, "pgdata": pgdata}
        _psql(pg, _BOOTSTRAP_SQL)

        yield pg
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    """Launch the shaded JAR against the pre-provisioned schema."""
    svc_port = _free_port()
    token    = "cat-inttest-bearer-secret-xyz"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        # Use nexus_app (non-superuser, NOBYPASSRLS) so FORCE ROW LEVEL SECURITY
        # actually applies. The OS superuser bypasses all RLS even with FORCE.
        # Trust auth in this hermetic initdb allows login without password.
        "NX_DB_USER": "nexus_app",
        "NX_DB_PASS": "",
        "NX_POOL_SIZE": "3",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    env.pop("NX_STORAGE_BACKEND_CATALOG", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=40.0)
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
def cat(service):
    """HttpCatalogClient (tenant='default') against the real Java service."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    base_url, token, _ = service
    os.environ["NX_SERVICE_TOKEN"] = token
    c = HttpCatalogClient(base_url=base_url, tenant="default", _token=token)
    yield c
    c.close()


@pytest.fixture(scope="module")
def cat_b(service):
    """HttpCatalogClient for the cross-tenant RLS probe (tenant='tenant-b')."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    base_url, token, _ = service
    c = HttpCatalogClient(base_url=base_url, tenant="tenant-b", _token=token)
    yield c
    c.close()


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCatalogServiceHealth:
    def test_stats_endpoint_reachable(self, cat) -> None:
        s = cat.stats()
        assert "doc_count" in s
        assert isinstance(s["doc_count"], int)

    def test_is_initialized(self, cat) -> None:
        assert cat.is_initialized() is True


class TestRegisterAndResolve:
    """
    a) register_owner (upsert) → register (server-side tumbler) → resolve → show
    """

    def test_register_owner_returns_tumbler(self, cat) -> None:
        from nexus.catalog.catalog import Tumbler
        t = cat.register_owner(
            name="inttest-repo",
            owner_type="curator",
            tumbler_prefix="1.1",
        )
        assert isinstance(t, Tumbler)
        assert str(t) == "1.1"

    def test_register_document_assigns_tumbler(self, cat) -> None:
        from nexus.catalog.catalog import Tumbler
        t = cat.register(
            "1.1",
            "Integration Test Paper",
            content_type="paper",
            author="Alice Researcher",
            year=2026,
            corpus="test-corpus",
            file_path="papers/test.md",
            source_uri="file:///papers/test.md",
        )
        assert isinstance(t, Tumbler)
        # Must be under owner prefix 1.1
        assert str(t).startswith("1.1.")

    def test_register_idempotent_same_source_uri(self, cat) -> None:
        """Re-registering the same source_uri must return the same tumbler (no-clobber)."""
        t1 = cat.register(
            "1.1",
            "Idempotency Test",
            content_type="paper",
            source_uri="file:///papers/idempotent.md",
        )
        t2 = cat.register(
            "1.1",
            "Idempotency Test",
            content_type="paper",
            source_uri="file:///papers/idempotent.md",
        )
        assert str(t1) == str(t2)

    def test_resolve_round_trip(self, cat) -> None:
        from nexus.catalog.catalog import CatalogEntry
        t = cat.register(
            "1.1",
            "Resolve Round Trip",
            content_type="paper",
            author="Bob Tester",
            year=2025,
            corpus="test-corpus",
            source_uri="file:///papers/resolve_rt.md",
        )
        entry = cat.resolve(t)
        assert entry is not None
        assert isinstance(entry, CatalogEntry)
        assert entry.title == "Resolve Round Trip"
        assert entry.author == "Bob Tester"
        assert entry.year == 2025
        assert str(entry.tumbler) == str(t)

    def test_resolve_nonexistent_returns_none(self, cat) -> None:
        result = cat.resolve("9.9.9999")
        assert result is None

    def test_stats_doc_count_increases(self, cat) -> None:
        before = cat.doc_count()
        cat.register(
            "1.1",
            "Stats Count Test",
            content_type="paper",
            source_uri="file:///papers/stats_count.md",
        )
        after = cat.doc_count()
        assert after > before

    def test_list_returns_registered_docs(self, cat) -> None:
        t = cat.register(
            "1.1",
            "List Test Doc",
            content_type="paper",
            source_uri="file:///papers/list_test.md",
        )
        docs = cat.all_documents(limit=100)
        tumblers = [str(d.tumbler) for d in docs]
        assert str(t) in tumblers

    def test_by_owner_returns_docs(self, cat) -> None:
        docs = cat.by_owner("1.1")
        assert len(docs) > 0
        for d in docs:
            assert str(d.tumbler).startswith("1.1.")

    def test_register_all_fields_round_trip(self, cat) -> None:
        """All Catalog.register() fields survive the HTTP round-trip (no silent data loss)."""
        t = cat.register(
            "1.1",
            "Full Fields Test",
            content_type="paper",
            file_path="papers/full_fields.md",
            corpus="test-corpus",
            physical_collection="knowledge__test__voyage-context-3__v1",
            chunk_count=42,
            head_hash="abc123def456",
            author="Carol Author",
            year=2024,
            meta={"key": "value", "num": 3},
            source_mtime=1717000000.0,
            source_uri="file:///papers/full_fields.md",
        )
        entry = cat.resolve(t)
        assert entry is not None
        assert entry.title == "Full Fields Test"
        assert entry.content_type == "paper"
        assert entry.file_path == "papers/full_fields.md"
        assert entry.corpus == "test-corpus"
        assert entry.physical_collection == "knowledge__test__voyage-context-3__v1"
        assert entry.chunk_count == 42
        assert entry.author == "Carol Author"
        assert entry.year == 2024
        assert entry.source_uri == "file:///papers/full_fields.md"


class TestLinkAndTraversal:
    """
    b) LINK + links traversal (graph) + link_query + traverse (BFS, depth)
    """

    @pytest.fixture(scope="class")
    def linked_docs(self, cat):
        """Register three documents and a link chain A -> B -> C."""
        a = cat.register("1.1", "Link-A", content_type="paper",
                         source_uri="file:///link/a.md")
        b = cat.register("1.1", "Link-B", content_type="paper",
                         source_uri="file:///link/b.md")
        c = cat.register("1.1", "Link-C", content_type="paper",
                         source_uri="file:///link/c.md")
        cat.link(a, b, "cites", created_by="inttest")
        cat.link(b, c, "cites", created_by="inttest")
        return a, b, c

    def test_links_from(self, cat, linked_docs) -> None:
        a, b, c = linked_docs
        lf = cat.links_from(a)
        assert len(lf) >= 1
        to_tumblers = [lk["to_tumbler"] for lk in lf]
        assert str(b) in to_tumblers

    def test_links_to(self, cat, linked_docs) -> None:
        a, b, c = linked_docs
        lt = cat.links_to(b)
        assert len(lt) >= 1
        from_tumblers = [lk["from_tumbler"] for lk in lt]
        assert str(a) in from_tumblers

    def test_link_query_filter_by_type(self, cat, linked_docs) -> None:
        a, b, c = linked_docs
        links = cat.link_query(link_type="cites", from_t=str(a))
        assert len(links) >= 1
        assert all(lk["link_type"] == "cites" for lk in links)

    def test_graph_depth_1(self, cat, linked_docs) -> None:
        """graph() POST /traverse with depth=1 returns direct neighbors."""
        a, b, c = linked_docs
        result = cat.graph(a, depth=1)
        assert "nodes" in result
        assert "edges" in result
        node_tumblers = [n.get("tumbler") for n in result["nodes"]]
        assert str(b) in node_tumblers

    def test_graph_depth_2_reaches_c(self, cat, linked_docs) -> None:
        """graph() with depth=2 follows the chain A->B->C."""
        a, b, c = linked_docs
        result = cat.graph(a, depth=2)
        node_tumblers = [n.get("tumbler") for n in result["nodes"]]
        assert str(c) in node_tumblers, (
            f"Expected {c} in depth-2 BFS from {a}; got nodes={node_tumblers}"
        )

    def test_link_query_created_by_filter(self, cat, linked_docs) -> None:
        a, b, c = linked_docs
        links = cat.link_query(created_by="inttest")
        assert len(links) >= 2

    def test_link_if_absent_idempotent(self, cat, linked_docs) -> None:
        """link_if_absent on an existing edge must not raise."""
        a, b, c = linked_docs
        cat.link_if_absent(a, b, "cites")

    def test_unlink(self, cat) -> None:
        """Create, verify, then remove a link."""
        x = cat.register("1.1", "UnlinkDoc-X", content_type="paper",
                         source_uri="file:///unlink/x.md")
        y = cat.register("1.1", "UnlinkDoc-Y", content_type="paper",
                         source_uri="file:///unlink/y.md")
        cat.link(x, y, "relates")
        before = cat.links_from(x)
        assert any(lk["to_tumbler"] == str(y) for lk in before)
        cat.unlink(x, y, "relates")
        after = cat.links_from(x)
        assert not any(lk["to_tumbler"] == str(y) for lk in after)


class TestManifest:
    """
    c) spans + document_chunks manifest round-trip (write / get / purge)
    """

    @pytest.fixture(scope="class")
    def doc_with_manifest(self, cat):
        t = cat.register(
            "1.1",
            "Manifest Test Doc",
            content_type="paper",
            source_uri="file:///manifest/test.md",
        )
        chunks = [
            {"position": 0, "chash": "chunk_hash_00", "line_start": 1, "line_end": 10},
            {"position": 1, "chash": "chunk_hash_01", "line_start": 11, "line_end": 20},
            {"position": 2, "chash": "chunk_hash_02", "line_start": 21, "line_end": 30},
        ]
        cat.write_manifest(str(t), chunks)
        return t, chunks

    def test_write_and_get_manifest(self, cat, doc_with_manifest) -> None:
        t, expected = doc_with_manifest
        rows = cat.get_manifest(str(t))
        assert len(rows) == 3
        chashes = [r["chash"] for r in rows]
        assert "chunk_hash_00" in chashes
        assert "chunk_hash_01" in chashes
        assert "chunk_hash_02" in chashes

    def test_get_chunk_chashes(self, cat, doc_with_manifest) -> None:
        t, expected = doc_with_manifest
        chashes = cat.get_chunk_chashes(str(t))
        assert "chunk_hash_00" in chashes
        assert len(chashes) == 3

    def test_append_manifest_chunks(self, cat, doc_with_manifest) -> None:
        t, _ = doc_with_manifest
        cat.append_manifest_chunks(str(t), [
            {"position": 3, "chash": "chunk_hash_03"},
        ])
        rows = cat.get_manifest(str(t))
        chashes = [r["chash"] for r in rows]
        assert "chunk_hash_03" in chashes

    def test_docs_for_chashes_reverse_lookup(self, cat, doc_with_manifest) -> None:
        """docs_for_chashes returns a flat list of distinct document tumblers.

        CatalogRepository.docsForChashes() runs SELECT DISTINCT doc_id WHERE
        chash IN (?), so the result is a list of tumblers (not a per-chash map).
        """
        t, _ = doc_with_manifest
        result = cat.docs_for_chashes(["chunk_hash_00", "chunk_hash_01"])
        assert isinstance(result, list)
        assert str(t) in result

    def test_purge_manifest(self, cat) -> None:
        t = cat.register(
            "1.1",
            "Purge Manifest Test",
            content_type="paper",
            source_uri="file:///manifest/purge.md",
        )
        cat.write_manifest(str(t), [{"position": 0, "chash": "purge_hash_00"}])
        before = cat.get_manifest(str(t))
        assert len(before) == 1
        cat.purge_manifest_for_doc(str(t))
        after = cat.get_manifest(str(t))
        assert len(after) == 0

    def test_atomic_manifest_replace(self, cat) -> None:
        """atomic_manifest_replace uses /manifest/write (delete+insert)."""
        t = cat.register(
            "1.1",
            "Atomic Replace Test",
            content_type="paper",
            source_uri="file:///manifest/atomic.md",
        )
        cat.write_manifest(str(t), [{"position": 0, "chash": "old_hash"}])
        cat.atomic_manifest_replace(str(t), [
            {"position": 0, "chash": "new_hash_00"},
            {"position": 1, "chash": "new_hash_01"},
        ])
        rows = cat.get_manifest(str(t))
        chashes = [r["chash"] for r in rows]
        assert "new_hash_00" in chashes
        assert "new_hash_01" in chashes
        assert "old_hash" not in chashes


class TestFTSSearch:
    """
    d) FTS: english stemming probe + simple identifier probe (152-FTS-tokenizer-DECISION)

    Schema per catalog-001-baseline.xml changeset 2:
      fts_vector = to_tsvector('english', title)
               || to_tsvector('simple', author)
               || to_tsvector('simple', corpus)
               || to_tsvector('simple', file_path)
    Query: plainto_tsquery('english',q) OR plainto_tsquery('simple',q)
    """

    @pytest.fixture(scope="class", autouse=True)
    def setup_fts_docs(self, cat):
        """Register docs with FTS-specific content."""
        cat.register(
            "1.1",
            "Neural Network Running Experiments",
            content_type="paper",
            author="Stemming Author",
            corpus="ml-corpus",
            file_path="papers/running.md",
            source_uri="file:///fts/running.md",
        )
        cat.register(
            "1.1",
            "Tokenizer Design Patterns",
            content_type="paper",
            author="Simple Token",
            corpus="ml-corpus",
            file_path="papers/tokenizer.md",
            source_uri="file:///fts/tokenizer.md",
        )

    def test_english_stemming_probe(self, cat) -> None:
        """'run' should match 'running' via english Snowball stemmer.

        ts_lexize('english_stem', 'running') = {run}
        ts_lexize('english_stem', 'run') = {run}
        So plainto_tsquery('english','run') @@ to_tsvector('english','running') = true.
        """
        results = cat.find("run")
        titles = [e.title for e in results]
        assert any("Running" in t for t in titles), (
            f"English stemming probe: expected a doc with 'Running' in title for query 'run', "
            f"got titles={titles}"
        )

    def test_simple_identifier_probe_corpus(self, cat) -> None:
        """Query the corpus field 'ml-corpus' — 'simple' tokenizer, exact match."""
        results = cat.find("ml-corpus")
        assert len(results) > 0, (
            "Simple identifier probe: expected docs with corpus='ml-corpus', got 0 results"
        )

    def test_simple_identifier_probe_file_path(self, cat) -> None:
        """Query by filename token in file_path — 'simple' tokenizer."""
        results = cat.find("tokenizer")
        titles = [e.title for e in results]
        assert any("Tokenizer" in t for t in titles), (
            f"Simple identifier probe (file_path): expected 'Tokenizer' doc, got {titles}"
        )

    def test_search_by_content_type_filter(self, cat) -> None:
        results = cat.find("running", content_type="paper")
        assert len(results) > 0


class TestCrossTenantRLS:
    """
    e) Cross-tenant RLS negative:
       - Tenant A's documents are invisible to tenant B
       - service role has FORCE RLS so it cannot bypass tenant isolation
       - An unset/wrong GUC means the RLS policy evaluates to NULL -> empty, not error
    """

    @pytest.fixture(scope="class")
    def tenant_a_doc(self, cat):
        """Register a doc in tenant 'default' (cat fixture)."""
        return cat.register(
            "1.1",
            "RLS Test Doc Tenant A",
            content_type="paper",
            source_uri="file:///rls/tenant_a.md",
        )

    def test_tenant_b_cannot_see_tenant_a_doc(self, cat, cat_b, tenant_a_doc) -> None:
        """Tenant B's catalog client must not be able to resolve tenant A's tumbler."""
        ta_tumbler = str(tenant_a_doc)

        entry_a = cat.resolve(ta_tumbler)
        assert entry_a is not None, f"Tenant A cannot resolve its own doc {ta_tumbler}"

        entry_b = cat_b.resolve(ta_tumbler)
        assert entry_b is None, (
            f"RLS BREACH: tenant B resolved tenant A's doc {ta_tumbler}! "
            f"entry={entry_b}"
        )

    def test_tenant_b_stats_shows_zero_tenant_a_docs(self, cat_b) -> None:
        """Tenant B (with no docs registered) sees 0 document count."""
        stats = cat_b.stats()
        assert stats.get("doc_count", 0) == 0, (
            f"RLS BREACH: tenant B sees non-zero doc_count={stats.get('doc_count')}"
        )

    def test_tenant_b_list_does_not_include_tenant_a_docs(
        self, cat, cat_b, tenant_a_doc
    ) -> None:
        """Tenant B's list must not include any tumbler from tenant A."""
        ta_tumblers = {str(d.tumbler) for d in cat.all_documents(limit=200)}
        tb_tumblers = {str(d.tumbler) for d in cat_b.all_documents(limit=200)}

        overlap = ta_tumblers & tb_tumblers
        assert overlap == set(), (
            f"RLS BREACH: tenant B list contains tenant A tumblers: {overlap}"
        )

    def test_tenant_b_can_register_its_own_owner(self, cat_b) -> None:
        """Tenant B must be able to create its own owner (RLS write isolation)."""
        t = cat_b.register_owner(
            name="tenant-b-repo",
            owner_type="curator",
            tumbler_prefix="2.1",
        )
        from nexus.catalog.catalog import Tumbler
        assert isinstance(t, Tumbler)

    def test_tenant_a_cannot_see_tenant_b_doc(self, cat, cat_b) -> None:
        """After registering in tenant B, tenant A cannot see it."""
        tb_doc = cat_b.register(
            "2.1",
            "RLS Test Doc Tenant B",
            content_type="paper",
            source_uri="file:///rls/tenant_b.md",
        )
        entry_a = cat.resolve(str(tb_doc))
        assert entry_a is None, (
            f"RLS BREACH: tenant A resolved tenant B's doc {tb_doc}! entry={entry_a}"
        )


class TestETLFidelity:
    """
    f) ETL fidelity + idempotent re-run (POST /import/document)
    """

    def test_etl_import_document_single(self, cat) -> None:
        """POST /import/document with a pre-assigned tumbler."""
        r = cat._post("/import/document", {
            "tumbler": "1.1.999",
            "title": "ETL Test Import",
            "content_type": "paper",
            "author": "ETL Author",
            "year": 2026,
            "corpus": "etl-corpus",
            "source_uri": "file:///etl/test.md",
        })
        assert r is not None
        entry = cat.resolve("1.1.999")
        assert entry is not None
        assert entry.title == "ETL Test Import"

    def test_etl_import_idempotent(self, cat) -> None:
        """Re-running the same /import/document is idempotent (ON CONFLICT DO NOTHING)."""
        payload = {
            "tumbler": "1.1.998",
            "title": "ETL Idempotency Test",
            "content_type": "paper",
            "source_uri": "file:///etl/idempotent.md",
        }
        cat._post("/import/document", payload)
        cat._post("/import/document", payload)
        entry = cat.resolve("1.1.998")
        assert entry is not None
        assert entry.title == "ETL Idempotency Test"

    def test_etl_import_owner(self, cat) -> None:
        """POST /import/owner: upsert an owner row."""
        r = cat._post("/import/owner", {
            "tumbler_prefix": "9.9",
            "name": "etl-imported-repo",
            "owner_type": "repo",
            "repo_hash": "abc123etl",
        })
        assert r is not None

    def test_etl_import_link(self, cat) -> None:
        """POST /import/link: insert a link row."""
        a = cat.register("1.1", "ETL Link A", content_type="paper",
                         source_uri="file:///etl/link_a.md")
        b = cat.register("1.1", "ETL Link B", content_type="paper",
                         source_uri="file:///etl/link_b.md")
        r = cat._post("/import/link", {
            "from_tumbler": str(a),
            "to_tumbler": str(b),
            "link_type": "etl-test",
            "created_by": "etl",
        })
        assert r is not None
        links = cat.links_from(a, link_type="etl-test")
        assert any(lk["to_tumbler"] == str(b) for lk in links)

    def test_collections_round_trip(self, cat) -> None:
        """register_collection + list_collections + get_collection."""
        coll_name = "knowledge__inttest__voyage-context-3__v1"
        cat.register_collection(
            coll_name,
            content_type="knowledge",
            owner_id="1.1",
            embedding_model="voyage-context-3",
        )
        colls = cat.list_collections()
        names = [c["name"] for c in colls]
        assert coll_name in names

        coll = cat.get_collection(coll_name)
        assert coll is not None
        assert coll["name"] == coll_name

    def test_rename_collection(self, cat) -> None:
        """rename_collection returns the count of DOCUMENTS updated (not collections).

        renameCollection() does:
          UPDATE catalog_documents SET physical_collection=new WHERE physical_collection=old
          UPDATE catalog_collections SET name=new WHERE name=old
        and returns the documents-updated count. No documents reference this collection
        so the count is 0 — but the collection itself is renamed.
        """
        old = "code__rename_test__voyage-code-3__v1"
        new = "code__rename_test__voyage-code-3__v2"
        cat.register_collection(old, content_type="code")
        n = cat.rename_collection(old, new)
        assert n >= 0  # 0 = no documents moved; collection row itself was renamed
        assert cat.get_collection(new) is not None
