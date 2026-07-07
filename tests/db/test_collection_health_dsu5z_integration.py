# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-service integration test: collection_health endpoint (nexus-dsu5z).

Proves that HttpCatalogClient.collection_health_meta and
collection_health._default_catalog_stats_fn work correctly via the real Java
service + real PostgreSQL.

Requires (darwin with JDK/GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built
      (cd service && mvn package -DskipTests)
  - Java on PATH (or JAVA_HOME env set)

Marked @pytest.mark.integration -- skipped when prerequisites absent.

Run locally:
    cd service && mvn package -DskipTests
    uv run pytest tests/db/test_collection_health_dsu5z_integration.py -m integration -q

What is exercised:
  A) collection_health_meta returns {last_indexed, orphan_count} with exact values
     for docs with known indexed_at and link structure.
  B) Unknown collection returns null last_indexed and zero orphan_count (safe defaults).
  C) collection_health._default_catalog_stats_fn delegates to cat.collection_health_meta
     end-to-end via the Java service (no hasattr(_db) guard firing).

Cross-tenant RLS isolation is tested at the Java layer in
service/src/test/java/dev/nexus/service/CatalogRepositoryTest.java
(@Order(121) collectionHealthMeta_crossTenantIsolation).
This integration test uses the OS superuser (bypasses FORCE RLS) which is the
correct setup for consumer-path correctness tests per tests/db/_service_fixture.py.
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
from unittest.mock import patch

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

_TOKEN  = "dsu5z-inttest-bearer-secret"
_TENANT = "dsu5z-tenant"
_COLL   = "knowledge__dsu5z__voyage-context-3__v1"


# ── Port / wait helpers ────────────────────────────────────────────────────────

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
    """Hermetic PostgreSQL 16 instance."""
    pgdata = tempfile.mkdtemp(prefix="nexus_dsu5z_inttest_pg_")
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
             "-U", pg_user, "nexuscatdsu5z"],
            check=True, capture_output=True,
        )

        pg = {"port": pg_port, "dbname": "nexuscatdsu5z", "user": pg_user, "pgdata": pgdata}
        # net63: provision nexus_svc role BEFORE the JAR starts Liquibase
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
    chroma_data = tempfile.mkdtemp(prefix="nexus-dsu5z-chroma-")
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
        "NX_CHROMA_PATH": chroma_data,
    }
    # Ensure service-mode env vars do not bleed into the subprocess
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
    """HttpCatalogClient against the real Java service."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    base_url, token, _ = java_service
    _saved_token = os.environ.get("NX_SERVICE_TOKEN")
    os.environ["NX_SERVICE_TOKEN"] = token
    c = HttpCatalogClient(base_url=base_url, tenant=_TENANT, _token=token)
    yield c
    c.close()
    # Restore: a leaked module token poisons later env-resolving modules (nexus-edwlp).
    if _saved_token is None:
        os.environ.pop("NX_SERVICE_TOKEN", None)
    else:
        os.environ["NX_SERVICE_TOKEN"] = _saved_token


@pytest.fixture(scope="module")
def seeded_catalog(cat):
    """Seed documents and links in _TENANT for collection_health assertions.

    Seeded state:
        owner: dsu5z-repo (prefix "20")
        doc_a: indexed_at="2026-01-01T08:00:00" -- no incoming links (orphan)
        doc_b: indexed_at="2026-06-01T12:00:00" -- no incoming links (orphan)
        doc_c: indexed_at="2026-03-15T00:00:00" -- has one incoming link (non-orphan)

    link: doc_a -> doc_c (cites) -- makes doc_c a non-orphan

    Expected collection_health_meta(_COLL):
        last_indexed = "2026-06-01T12:00:00"  (MAX of the three)
        orphan_count = 2  (doc_a and doc_b have no incoming links)
    """
    owner_t = cat.register_owner(
        name="dsu5z-repo", owner_type="repo", tumbler_prefix="20"
    )
    doc_a = cat.register(
        str(owner_t), "DSU5Z Doc A",
        physical_collection=_COLL,
        source_uri="file:///dsu5z/doc-a.md",
        indexed_at="2026-01-01T08:00:00",
    )
    doc_b = cat.register(
        str(owner_t), "DSU5Z Doc B",
        physical_collection=_COLL,
        source_uri="file:///dsu5z/doc-b.md",
        indexed_at="2026-06-01T12:00:00",
    )
    doc_c = cat.register(
        str(owner_t), "DSU5Z Doc C",
        physical_collection=_COLL,
        source_uri="file:///dsu5z/doc-c.md",
        indexed_at="2026-03-15T00:00:00",
    )
    # Link: doc_a -> doc_c (makes doc_c a non-orphan; doc_a and doc_b remain orphans)
    cat.link(doc_a, doc_c, "cites", created_by="inttest-dsu5z")

    return {
        "owner_t": str(owner_t),
        "doc_a": str(doc_a),
        "doc_b": str(doc_b),
        "doc_c": str(doc_c),
    }


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestCollectionHealthMetaLiveService:
    """A) collection_health_meta returns exact values from the real Java service."""

    def test_last_indexed_is_max_indexed_at(self, cat, seeded_catalog) -> None:
        """last_indexed = MAX(indexed_at) = '2026-06-01T12:00:00' for _COLL."""
        result = cat.collection_health_meta(_COLL)
        assert result["last_indexed"] == "2026-06-01T12:00:00", (
            f"Expected last_indexed='2026-06-01T12:00:00', got {result['last_indexed']!r}"
        )

    def test_orphan_count_exact(self, cat, seeded_catalog) -> None:
        """orphan_count = 2 (doc_a and doc_b have no incoming links; doc_c has one)."""
        result = cat.collection_health_meta(_COLL)
        assert result["orphan_count"] == 2, (
            f"Expected orphan_count=2, got {result['orphan_count']}"
        )

    def test_result_has_required_keys(self, cat, seeded_catalog) -> None:
        """Result dict always contains last_indexed, orphan_count, and
        stale_source_ratio.

        nexus-u26b4: nexus-agsq7's FIRST (source_mtime-based, structurally
        vacuous) stale_source_ratio wiring was reverted, which is why this
        test used to assert the key's ABSENCE. A second, index-age-based
        implementation (``_STALE_SOURCE_AGE_DAYS`` in catalog.py, backed by
        the catalog-011 PG view service-side) reintroduced the same key on
        both local ``Catalog.collection_health_meta()`` and the wire
        response; HttpCatalogClient.collection_health_meta() was silently
        dropping it when reconstructing its return dict (the h8rf6.3
        incident class) until this fix — the shape-parity tripwire
        (tests/catalog/test_shape_parity_tripwire.py) now pins the field
        present on both sides.
        """
        result = cat.collection_health_meta(_COLL)
        assert "last_indexed" in result
        assert "orphan_count" in result
        assert "stale_source_ratio" in result

    def test_unknown_collection_returns_safe_defaults(self, cat, seeded_catalog) -> None:
        """B) Unknown collection -> last_indexed=None, orphan_count=0."""
        result = cat.collection_health_meta("no__such__collection__v99")
        assert result["last_indexed"] is None, (
            f"Expected last_indexed=None for unknown collection, got {result['last_indexed']!r}"
        )
        assert result["orphan_count"] == 0, (
            f"Expected orphan_count=0 for unknown collection, got {result['orphan_count']}"
        )


class TestCollectionHealthDefaultStatsFn:
    """C) _default_catalog_stats_fn delegates to collection_health_meta end-to-end."""

    def test_default_stats_fn_via_service(self, java_service, seeded_catalog) -> None:
        """_default_catalog_stats_fn returns correct values via HttpCatalogClient."""
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.collection_health import _default_catalog_stats_fn

        base_url, token, _ = java_service
        client = HttpCatalogClient(
            base_url=base_url, tenant=_TENANT, _token=token
        )
        try:
            with patch("nexus.collection_health._open_catalog", return_value=client):
                result = _default_catalog_stats_fn(_COLL)

            assert result["last_indexed"] == "2026-06-01T12:00:00", (
                f"_default_catalog_stats_fn last_indexed wrong: {result}"
            )
            assert result["orphan_count"] == 2, (
                f"_default_catalog_stats_fn orphan_count wrong: {result}"
            )
        finally:
            client.close()

    def test_default_stats_fn_no_db_access(self, java_service, seeded_catalog) -> None:
        """_default_catalog_stats_fn must NOT access ._db on the HttpCatalogClient.

        If the old hasattr(cat, '_db') guard fired, it would bypass
        collection_health_meta and return {last_indexed: None, orphan_count: 0}.
        Correct result from the Java service proves the new code path is taken.
        """
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.collection_health import _default_catalog_stats_fn

        base_url, token, _ = java_service
        client = HttpCatalogClient(
            base_url=base_url, tenant=_TENANT, _token=token
        )
        try:
            with patch("nexus.collection_health._open_catalog", return_value=client):
                result = _default_catalog_stats_fn(_COLL)

            # Non-zero orphan_count proves the Java endpoint was hit (old guard
            # would have returned 0 without hitting the service at all).
            assert result["orphan_count"] > 0, (
                f"orphan_count=0 suggests the old hasattr(_db) guard fired; "
                f"full result: {result}"
            )
        finally:
            client.close()
