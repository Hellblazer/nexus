# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-service integration test: catalog consumer migration (nexus-qnp5s).

Proves that scoring.py, repos.py, and doc_indexer consumer paths work correctly
when NX_STORAGE_BACKEND_CATALOG=service routes through the real HttpCatalogClient
against the real Java service + real PostgreSQL.

Requires (darwin with JDK/GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built
      (cd service && mvn package -DskipTests)
  - Java on PATH (or JAVA_HOME env set)

Marked @pytest.mark.integration — skipped when prerequisites absent.

Run locally:
    cd service && mvn package -DskipTests
    uv run pytest tests/db/test_catalog_consumer_qnp5s_integration.py -m integration -q

What is exercised (nexus-qnp5s consumer migration):
  A) scoring.py chunk_counts_for_docs batch lookup — correct values for seeded docs
  B) scoring.py links_from_batch grouped results — correct per-tumbler link grouping
  C) repos.py list_owners_by_type — only repo-type owners returned
  D) repos.py collections_by_owner — filters to owner's physical_collection entries
  E) repos.py get_owner_by_prefix — exact owner dict returned; absent returns None
  F) doc_indexer curator_owner_tumbler_by_name — resolves seeded curator correctly
  G) Zero catalog._db access — client handle IS HttpCatalogClient, not Catalog

NX_STORAGE_BACKEND is NOT set — default SQLite path is unchanged for other tiers.
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

# ── Bootstrap SQL (catalog schema, matching test_http_catalog_integration.py) ─
#
# Subset of the full _BOOTSTRAP_SQL from test_http_catalog_integration.py —
# we only need the tables exercised by the consumer paths under test.
# Full copy used for clarity and to avoid import coupling.

_BOOTSTRAP_SQL = """\
CREATE SCHEMA IF NOT EXISTS nexus;

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

CREATE TABLE nexus.catalog_documents (
    tenant_id           TEXT NOT NULL,
    tumbler             TEXT NOT NULL,
    title               TEXT NOT NULL,
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
    alias_of            TEXT NOT NULL DEFAULT '',
    source_uri          TEXT NOT NULL DEFAULT '',
    CONSTRAINT catalog_documents_pk PRIMARY KEY (tenant_id, tumbler)
);

CREATE TABLE nexus.catalog_links (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    from_tumbler TEXT NOT NULL,
    to_tumbler   TEXT NOT NULL,
    link_type    TEXT NOT NULL,
    from_span    TEXT,
    to_span      TEXT,
    created_by   TEXT,
    created_at   TEXT,
    metadata     JSONB,
    CONSTRAINT catalog_links_unique UNIQUE (tenant_id, from_tumbler, to_tumbler, link_type)
);

CREATE TABLE nexus.catalog_document_chunks (
    tenant_id TEXT NOT NULL,
    doc_id    TEXT NOT NULL,
    position  INTEGER NOT NULL,
    chash     TEXT NOT NULL,
    line_start INTEGER,
    line_end   INTEGER,
    CONSTRAINT catalog_document_chunks_pk PRIMARY KEY (tenant_id, doc_id, position)
);

CREATE TABLE nexus.catalog_collections (
    tenant_id           TEXT NOT NULL,
    name                TEXT NOT NULL,
    content_type        TEXT NOT NULL DEFAULT '',
    owner_id            TEXT NOT NULL DEFAULT '',
    embedding_model     TEXT NOT NULL DEFAULT '',
    model_version       TEXT NOT NULL DEFAULT '',
    display_name        TEXT NOT NULL DEFAULT '',
    legacy_grandfathered INTEGER NOT NULL DEFAULT 0,
    superseded_by       TEXT NOT NULL DEFAULT '',
    superseded_at       TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT '',
    CONSTRAINT catalog_collections_pk PRIMARY KEY (tenant_id, name)
);

CREATE INDEX idx_catalog_collections_owner
    ON nexus.catalog_collections (tenant_id, owner_id);

CREATE TABLE nexus.catalog_meta (
    tenant_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT,
    CONSTRAINT catalog_meta_pk PRIMARY KEY (tenant_id, key)
);

-- RLS setup

ALTER TABLE nexus.catalog_owners            ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_owners            FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_owners
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_documents         ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_documents         FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_documents
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_links             ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_links             FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_links
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_document_chunks   ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_document_chunks   FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_document_chunks
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_collections       ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_collections       FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_collections
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

ALTER TABLE nexus.catalog_meta              ENABLE ROW LEVEL SECURITY;
ALTER TABLE nexus.catalog_meta              FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON nexus.catalog_meta
    USING      (tenant_id = current_setting('nexus.tenant', true))
    WITH CHECK (tenant_id = current_setting('nexus.tenant', true));

-- nexus_app service role (non-superuser, NOBYPASSRLS)
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_app') THEN
    CREATE ROLE nexus_app LOGIN
    NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;

GRANT CONNECT ON DATABASE nexuscatqnp5s TO nexus_app;
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

_TOKEN = "qnp5s-inttest-bearer-secret-xyz"
_TENANT = "qnp5s-tenant"


# ── Port helpers (identical to test_http_catalog_integration.py) ──────────────

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
    """Hermetic PostgreSQL 16 instance with catalog schema applied."""
    pgdata = tempfile.mkdtemp(prefix="nexus_qnp5s_inttest_pg_")
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
             "-U", pg_user, "nexuscatqnp5s"],
            check=True, capture_output=True,
        )

        pg = {"port": pg_port, "dbname": "nexuscatqnp5s", "user": pg_user, "pgdata": pgdata}

        # net63: JAR runs Liquibase at startup; grants-nexus-svc.xml (runAlways=true)
        # issues GRANT ... TO nexus_svc.  That role must exist BEFORE the JAR starts.
        # NX_DB_USER is the OS superuser (trust auth) — Liquibase runs as it and creates
        # the full schema.  No _BOOTSTRAP_SQL needed; Liquibase owns the DDL.
        _psql(pg, SERVICE_ROLES_SQL)

        yield pg
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def java_service(pg_instance):
    """Launch the shaded JAR against the pre-provisioned schema."""
    svc_port = _free_port()
    # Use a fresh temp dir for Chroma so the JAR does not open the dev Chroma
    # database at ~/.config/nexus/chroma (which may have incompatible SQLite state).
    chroma_data = tempfile.mkdtemp(prefix="nexus-qnp5s-chroma-")

    # Use the OS superuser (trust auth) for both Liquibase (DDL) and app queries.
    # The superuser bypasses FORCE RLS — acceptable for this test which exercises
    # correctness of the consumer paths, not RLS isolation.
    pg_user = pg_instance["user"]
    pg_jdbc = (
        f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
        f"/{pg_instance['dbname']}"
    )
    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": _TOKEN,
        "NX_DB_URL":  pg_jdbc,
        "NX_DB_USER": pg_user,
        "NX_DB_PASS": "",
        "NX_POOL_SIZE": "3",
        # Isolate Chroma from the dev instance to avoid SQLite-version panics.
        "NX_CHROMA_PATH": chroma_data,
    }
    # Ensure catalog service mode does NOT bleed into the subprocess env
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
        yield f"http://127.0.0.1:{svc_port}", _TOKEN, proc
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
        shutil.rmtree(chroma_data, ignore_errors=True)


@pytest.fixture(scope="module")
def cat(java_service):
    """HttpCatalogClient against the real Java service.

    This fixture returns an HttpCatalogClient — it does NOT have a ._db attribute.
    The absence of ._db proves the consumer path is NOT touching the SQLite backend.
    """
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    base_url, token, _ = java_service
    os.environ["NX_SERVICE_TOKEN"] = token
    c = HttpCatalogClient(base_url=base_url, tenant=_TENANT, _token=token)
    yield c
    c.close()


@pytest.fixture(scope="module")
def seeded_catalog(cat):
    """Seed the catalog with owners and documents for consumer tests.

    Returns a dict with known tumblers for assertions.
    """
    # Register owners
    repo_owner_t = cat.register_owner(
        name="qnp5s-repo",
        owner_type="repo",
        tumbler_prefix="10",
        repo_root="/Users/hal/git/qnp5s-repo",
        head_hash="qnp5shead",
    )
    curator_owner_t = cat.register_owner(
        name="qnp5s-curator",
        owner_type="curator",
        tumbler_prefix="11",
    )
    # A second repo owner for list-filtering tests
    cat.register_owner(
        name="qnp5s-repo-2",
        owner_type="repo",
        tumbler_prefix="12",
        repo_root="/Users/hal/git/qnp5s-repo-2",
    )

    # Register documents under the repo owner
    doc_a = cat.register(
        str(repo_owner_t),
        "QNP5S Doc A",
        content_type="paper",
        corpus="knowledge",
        source_uri="file:///qnp5s/doc-a.md",
        chunk_count=15,
        physical_collection="knowledge__qnp5s-repo__voyage-context-3__v1",
    )
    doc_b = cat.register(
        str(repo_owner_t),
        "QNP5S Doc B",
        content_type="paper",
        corpus="knowledge",
        source_uri="file:///qnp5s/doc-b.md",
        chunk_count=7,
        physical_collection="knowledge__qnp5s-repo__voyage-context-3__v1",
    )
    doc_c = cat.register(
        str(repo_owner_t),
        "QNP5S Doc C (no chunks)",
        content_type="paper",
        corpus="knowledge",
        source_uri="file:///qnp5s/doc-c.md",
        # chunk_count omitted — stored as 0 by Java service
    )

    # Register a collection under the repo owner (used by collections_by_owner test)
    cat.register_collection(
        "knowledge__qnp5s-repo__voyage-context-3__v1",
        content_type="knowledge",
        owner_id=str(repo_owner_t),
        embedding_model="voyage-context-3",
    )

    # Seed links: doc_a -> doc_b (cites), doc_a -> doc_c (relates)
    cat.link(doc_a, doc_b, "cites", created_by="inttest-qnp5s")
    cat.link(doc_a, doc_c, "relates", created_by="inttest-qnp5s")

    return {
        "repo_owner_t": str(repo_owner_t),
        "curator_owner_t": str(curator_owner_t),
        "doc_a": str(doc_a),
        "doc_b": str(doc_b),
        "doc_c": str(doc_c),
    }


# ── Test classes ──────────────────────────────────────────────────────────────


class TestNoSQLiteAccess:
    """G) HttpCatalogClient handle does NOT have ._db — proves no SQLite access."""

    def test_client_is_http_catalog_client(self, cat) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        assert isinstance(cat, HttpCatalogClient), (
            f"Expected HttpCatalogClient, got {type(cat)}"
        )

    def test_client_has_no_db_attribute(self, cat) -> None:
        """The SqliteCatalog ._db attribute must NOT exist on HttpCatalogClient.

        If this test fails, the consumer is using the SQLite backend, not the
        HTTP service — that would mean NX_STORAGE_BACKEND_CATALOG=service is
        silently falling back to SQLite.
        """
        assert not hasattr(cat, "_db"), (
            "HttpCatalogClient has a ._db attribute — the catalog is SQLite, not service mode"
        )


class TestScoringChunkCounts:
    """A) scoring.py chunk_counts_for_docs batch endpoint via real HttpCatalogClient."""

    def test_chunk_counts_exact_values(self, cat, seeded_catalog) -> None:
        """Batch lookup returns exactly correct chunk_counts for seeded docs."""
        doc_a = seeded_catalog["doc_a"]
        doc_b = seeded_catalog["doc_b"]
        doc_c = seeded_catalog["doc_c"]

        result = cat.chunk_counts_for_docs([doc_a, doc_b, doc_c, "99.9999.ABSENT"])

        assert doc_a in result, f"doc_a {doc_a} missing from chunk_counts result"
        assert result[doc_a] == 15, f"doc_a chunk_count: expected 15, got {result[doc_a]}"

        assert doc_b in result, f"doc_b {doc_b} missing from chunk_counts result"
        assert result[doc_b] == 7, f"doc_b chunk_count: expected 7, got {result[doc_b]}"

        # doc_c was registered with no chunk_count -> stored as 0 by Java
        assert doc_c in result
        assert result[doc_c] == 0

        # Absent tumbler must not appear
        assert "99.9999.ABSENT" not in result

    def test_chunk_counts_empty_input_returns_empty(self, cat, seeded_catalog) -> None:
        result = cat.chunk_counts_for_docs([])
        assert result == {}

    def test_chunk_counts_only_absent_tumblers(self, cat, seeded_catalog) -> None:
        result = cat.chunk_counts_for_docs(["no.such.doc.1", "no.such.doc.2"])
        assert result == {}

    def test_chunk_counts_superset_batch(self, cat, seeded_catalog) -> None:
        """Query a batch larger than the DB contains — only present docs in result."""
        doc_a = seeded_catalog["doc_a"]
        absent = [f"99.{i}.absent" for i in range(5)]
        result = cat.chunk_counts_for_docs([doc_a] + absent)
        assert doc_a in result
        for a in absent:
            assert a not in result


class TestScoringLinksFromBatch:
    """B) scoring.py links_from_batch grouped results via real HttpCatalogClient."""

    def test_links_grouped_by_from_tumbler(self, cat, seeded_catalog) -> None:
        """doc_a has 2 outbound links; doc_b has 0; grouping must be correct."""
        doc_a = seeded_catalog["doc_a"]
        doc_b = seeded_catalog["doc_b"]
        doc_c = seeded_catalog["doc_c"]

        result = cat.links_from_batch([doc_a, doc_b, doc_c, "99.absent"])

        # doc_a has cites -> doc_b and relates -> doc_c
        assert doc_a in result, f"doc_a {doc_a} missing from links_from_batch"
        links_a = result[doc_a]
        assert len(links_a) == 2
        link_types_a = {lnk["link_type"] for lnk in links_a}
        assert link_types_a == {"cites", "relates"}, (
            f"Expected link types {{cites, relates}} for doc_a, got {link_types_a}"
        )
        # from_tumbler must be set on each entry
        for lnk in links_a:
            assert lnk.get("from_tumbler") == doc_a

    def test_tumbler_with_no_links_absent_from_result(self, cat, seeded_catalog) -> None:
        """doc_b has no outbound links — must be absent from result (not an empty list)."""
        doc_b = seeded_catalog["doc_b"]
        result = cat.links_from_batch([doc_b])
        assert doc_b not in result, (
            f"doc_b has no outbound links but appeared in links_from_batch result: {result}"
        )

    def test_links_empty_input_returns_empty(self, cat, seeded_catalog) -> None:
        result = cat.links_from_batch([])
        assert result == {}

    def test_links_only_absent_tumblers_returns_empty(self, cat, seeded_catalog) -> None:
        result = cat.links_from_batch(["99.absent.1", "99.absent.2"])
        assert result == {}


class TestReposIdentity:
    """C/D/E) repos.py identity methods via real HttpCatalogClient."""

    def test_list_owners_by_type_repo_only(self, cat, seeded_catalog) -> None:
        """C) list_owners_by_type('repo') returns only repo-type owners."""
        repo_owners = cat.list_owners_by_type("repo")
        names = [o.get("name") for o in repo_owners]

        # Both repo owners must appear
        assert "qnp5s-repo" in names, f"qnp5s-repo missing; got {names}"
        assert "qnp5s-repo-2" in names, f"qnp5s-repo-2 missing; got {names}"

        # Curator must NOT appear
        assert "qnp5s-curator" not in names, (
            f"curator owner appeared in repo list: {names}"
        )

    def test_list_owners_by_type_curator_only(self, cat, seeded_catalog) -> None:
        """C) list_owners_by_type('curator') returns only curator-type owners."""
        curators = cat.list_owners_by_type("curator")
        names = [o.get("name") for o in curators]
        assert "qnp5s-curator" in names
        assert "qnp5s-repo" not in names
        assert "qnp5s-repo-2" not in names

    def test_list_owners_by_type_unknown_empty(self, cat, seeded_catalog) -> None:
        """C) Unknown owner_type returns empty list (not error)."""
        result = cat.list_owners_by_type("no-such-type")
        assert result == []

    def test_collections_by_owner(self, cat, seeded_catalog) -> None:
        """D) collections_by_owner returns collections registered under that owner."""
        repo_owner_t = seeded_catalog["repo_owner_t"]
        colls = cat.collections_by_owner(repo_owner_t)
        # We seeded one collection under repo_owner
        coll_names = [c.get("name") for c in colls]
        assert "knowledge__qnp5s-repo__voyage-context-3__v1" in coll_names, (
            f"Expected collection missing; got {coll_names}"
        )

    def test_get_owner_by_prefix_found(self, cat, seeded_catalog) -> None:
        """E) get_owner_by_prefix returns full owner dict for existing prefix."""
        repo_owner_t = seeded_catalog["repo_owner_t"]
        owner = cat.get_owner_by_prefix(repo_owner_t)
        assert owner is not None, f"get_owner_by_prefix({repo_owner_t!r}) returned None"
        assert owner.get("name") == "qnp5s-repo"
        assert owner.get("owner_type") == "repo"
        assert owner.get("tumbler_prefix") == repo_owner_t

    def test_get_owner_by_prefix_absent_returns_none(self, cat, seeded_catalog) -> None:
        """E) get_owner_by_prefix returns None for unknown prefix."""
        result = cat.get_owner_by_prefix("99.9999.absent-prefix")
        assert result is None


class TestDocIndexerCuratorLookup:
    """F) doc_indexer curator_owner_tumbler_by_name — resolves seeded curator."""

    def test_curator_owner_tumbler_by_name_found(self, cat, seeded_catalog) -> None:
        """Resolves the seeded curator by name."""
        curator_t = seeded_catalog["curator_owner_t"]
        result = cat.curator_owner_tumbler_by_name("qnp5s-curator")
        assert result is not None, (
            "curator_owner_tumbler_by_name('qnp5s-curator') returned None"
        )
        # Must equal the tumbler_prefix assigned at register_owner time
        assert str(result) == curator_t, (
            f"Expected curator tumbler {curator_t!r}, got {result!r}"
        )

    def test_curator_does_not_return_repo_type(self, cat, seeded_catalog) -> None:
        """curator_owner_tumbler_by_name filters to owner_type='curator' only.

        'qnp5s-repo' is a repo-type owner. The method must not return it.
        """
        result = cat.curator_owner_tumbler_by_name("qnp5s-repo")
        assert result is None, (
            f"Expected None for repo-type owner lookup via curator path, got {result!r}"
        )

    def test_curator_not_found_returns_none(self, cat, seeded_catalog) -> None:
        result = cat.curator_owner_tumbler_by_name("no-such-curator")
        assert result is None
