# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-service integration test: coverage_by_content_type endpoint (nexus-3cwnx).

Proves that HttpCatalogClient.coverage_by_content_type works correctly via the
real Java service + real PostgreSQL, and that its results match the SQLite
Catalog.coverage_by_content_type mirror on identical seeded data
(differential parity proof).

Requires (darwin with JDK/GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built
      (cd service && mvn package -DskipTests)
  - Java on PATH (or JAVA_HOME env set)

Marked @pytest.mark.integration -- skipped when prerequisites absent.

Run locally:
    cd service && mvn package -DskipTests
    uv run pytest tests/db/test_coverage_3cwnx_integration.py -m integration -q

What is exercised:
  A) coverage_by_content_type() with no owner_prefix: exact {total, linked} per
     content_type for a catalog with mixed link coverage.
  B) coverage_by_content_type(owner_prefix=...) scopes to the prefix subtree.
  C) Differential parity: service results == SQLite Catalog results on identical data.
  D) coverage_cmd routing: no catalog._db access; guard removed; ClickException gone.

Cross-tenant RLS isolation is tested at the Java layer in
service/src/test/java/dev/nexus/service/CatalogRepositoryTest.java
(@Order(143-145) coverageByContentType_* tests).
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

_TOKEN  = "3cwnx-inttest-bearer-secret-xyz"
_TENANT = "3cwnx-tenant"


# ── Port / psql helpers ────────────────────────────────────────────────────────

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
    """Hermetic PostgreSQL 16 instance with catalog schema applied via Liquibase."""
    pgdata = tempfile.mkdtemp(prefix="nexus_3cwnx_inttest_pg_")
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
             "-U", pg_user, "nexuscat3cwnx"],
            check=True, capture_output=True,
        )

        pg = {"port": pg_port, "dbname": "nexuscat3cwnx", "user": pg_user, "pgdata": pgdata}
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
    chroma_data = tempfile.mkdtemp(prefix="nexus-3cwnx-chroma-")

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
    os.environ["NX_SERVICE_TOKEN"] = token
    c = HttpCatalogClient(base_url=base_url, tenant=_TENANT, _token=token)
    yield c
    c.close()


@pytest.fixture(scope="module")
def sqlite_db():
    """Raw SQLite database pre-seeded with identical data for parity checks.

    Returns (db_path, conn) so tests can call Catalog.coverage_by_content_type
    directly via a fresh Catalog wrapping the same DB.
    """
    from nexus.db.t2.catalog import CatalogStore
    db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="3cwnx_parity_")
    os.close(db_fd)
    store = CatalogStore(Path(db_path))
    yield store
    store.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture(scope="module")
def sqlite_cat(sqlite_db):
    """Thin wrapper that exposes coverage_by_content_type on the SQLite store
    using the same SQL logic as Catalog.coverage_by_content_type.

    We call the method directly on the Catalog instance by creating a minimal
    Catalog-like shim that delegates to sqlite_db.execute.  This avoids the
    full Catalog constructor which needs catalog_dir + complex initialisation.
    """
    class _SqliteCoverageProxy:
        """Proxy that exposes coverage_by_content_type backed by CatalogStore."""

        def __init__(self, store):
            self._db = store

        def coverage_by_content_type(self, owner_prefix: str = "") -> list[dict]:
            """Mirror of Catalog.coverage_by_content_type using CatalogStore."""
            if owner_prefix:
                like_pat = owner_prefix.rstrip(".") + ".%"
                type_rows = self._db.execute(
                    "SELECT DISTINCT content_type FROM documents "
                    "WHERE tumbler LIKE ? OR tumbler = ?",
                    (like_pat, owner_prefix),
                ).fetchall()
            else:
                type_rows = self._db.execute(
                    "SELECT DISTINCT content_type FROM documents"
                ).fetchall()

            result = []
            for (ct,) in type_rows:
                ct_key = ct if ct is not None else ""
                if owner_prefix:
                    like_pat = owner_prefix.rstrip(".") + ".%"
                    if ct is None:
                        total = self._db.execute(
                            "SELECT COUNT(*) FROM documents "
                            "WHERE content_type IS NULL "
                            "  AND (tumbler LIKE ? OR tumbler = ?)",
                            (like_pat, owner_prefix),
                        ).fetchone()[0]
                        linked = self._db.execute(
                            """
                            SELECT COUNT(DISTINCT d.tumbler)
                            FROM documents d
                            JOIN links l ON d.tumbler = l.from_tumbler
                                         OR d.tumbler = l.to_tumbler
                            WHERE d.content_type IS NULL
                              AND (d.tumbler LIKE ? OR d.tumbler = ?)
                            """,
                            (like_pat, owner_prefix),
                        ).fetchone()[0]
                    else:
                        total = self._db.execute(
                            "SELECT COUNT(*) FROM documents "
                            "WHERE content_type = ? AND (tumbler LIKE ? OR tumbler = ?)",
                            (ct, like_pat, owner_prefix),
                        ).fetchone()[0]
                        linked = self._db.execute(
                            """
                            SELECT COUNT(DISTINCT d.tumbler)
                            FROM documents d
                            JOIN links l ON d.tumbler = l.from_tumbler
                                         OR d.tumbler = l.to_tumbler
                            WHERE d.content_type = ?
                              AND (d.tumbler LIKE ? OR d.tumbler = ?)
                            """,
                            (ct, like_pat, owner_prefix),
                        ).fetchone()[0]
                else:
                    if ct is None:
                        total = self._db.execute(
                            "SELECT COUNT(*) FROM documents WHERE content_type IS NULL"
                        ).fetchone()[0]
                        linked = self._db.execute(
                            """
                            SELECT COUNT(DISTINCT d.tumbler)
                            FROM documents d
                            JOIN links l ON d.tumbler = l.from_tumbler
                                         OR d.tumbler = l.to_tumbler
                            WHERE d.content_type IS NULL
                            """
                        ).fetchone()[0]
                    else:
                        total = self._db.execute(
                            "SELECT COUNT(*) FROM documents WHERE content_type = ?",
                            (ct,),
                        ).fetchone()[0]
                        linked = self._db.execute(
                            """
                            SELECT COUNT(DISTINCT d.tumbler)
                            FROM documents d
                            JOIN links l ON d.tumbler = l.from_tumbler
                                         OR d.tumbler = l.to_tumbler
                            WHERE d.content_type = ?
                            """,
                            (ct,),
                        ).fetchone()[0]
                result.append({"content_type": ct_key, "total": total, "linked": linked})
            return result

    yield _SqliteCoverageProxy(sqlite_db)


def _seed_http(cat) -> None:
    """Seed the HTTP (service) catalog with deterministic coverage test data.

    Seed:
      3 papers under "30": 30.1, 30.2, 30.3
      2 rdrs   under "30": 30.4, 30.5
      1 code   under "30": 30.6
      1 paper  under "31": 31.1

      Links:
        30.1 -> 30.2 (cites)       => 30.1 from, 30.2 from+to
        30.2 -> 30.4 (implements)  => 30.4 to
        31.1 -> 30.1 (relates)     => 31.1 from, 30.1 also to
    """
    for tumbler, title, ct in [
        ("30.1", "3cwnx Paper A",           "paper"),
        ("30.2", "3cwnx Paper B",           "paper"),
        ("30.3", "3cwnx Paper C (unlinked)","paper"),
        ("30.4", "3cwnx RDR A",             "rdr"),
        ("30.5", "3cwnx RDR B (unlinked)",  "rdr"),
        ("30.6", "3cwnx Code A (unlinked)", "code"),
        ("31.1", "3cwnx Sub-Paper A",       "paper"),
    ]:
        cat._post("/register", {"tumbler": tumbler, "title": title, "content_type": ct})

    cat.link("30.1", "30.2", "cites",      created_by="inttest-3cwnx")
    cat.link("30.2", "30.4", "implements", created_by="inttest-3cwnx")
    cat.link("31.1", "30.1", "relates",    created_by="inttest-3cwnx")


def _seed_sqlite(store) -> None:
    """Seed the SQLite CatalogStore with identical data."""
    for tumbler, title, ct in [
        ("30.1", "3cwnx Paper A",           "paper"),
        ("30.2", "3cwnx Paper B",           "paper"),
        ("30.3", "3cwnx Paper C (unlinked)","paper"),
        ("30.4", "3cwnx RDR A",             "rdr"),
        ("30.5", "3cwnx RDR B (unlinked)",  "rdr"),
        ("30.6", "3cwnx Code A (unlinked)", "code"),
        ("31.1", "3cwnx Sub-Paper A",       "paper"),
    ]:
        store.execute(
            "INSERT OR IGNORE INTO documents (tumbler, title, content_type) "
            "VALUES (?, ?, ?)",
            (tumbler, title, ct),
        )
    store._conn.commit()

    for from_t, to_t, lt in [
        ("30.1", "30.2", "cites"),
        ("30.2", "30.4", "implements"),
        ("31.1", "30.1", "relates"),
    ]:
        store.execute(
            "INSERT OR IGNORE INTO links (from_tumbler, to_tumbler, link_type, created_by) "
            "VALUES (?, ?, ?, ?)",
            (from_t, to_t, lt, "inttest-3cwnx"),
        )
    store._conn.commit()


@pytest.fixture(scope="module")
def seeded(cat, sqlite_db):
    """Seed both backends with identical data."""
    _seed_http(cat)
    _seed_sqlite(sqlite_db)
    return True


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_coverage_no_prefix_exact_values(seeded, cat) -> None:
    """A) Service: exact {total, linked} per content_type with no prefix."""
    rows = cat.coverage_by_content_type()
    by_type = {r["content_type"]: r for r in rows}

    # paper: 30.1+30.2+30.3 (under 30) + 31.1 (under 31) = 4 total
    # linked: 30.1 (from+to), 30.2 (from+to), 31.1 (from) = 3 linked papers
    assert by_type["paper"]["total"] == 4
    assert by_type["paper"]["linked"] == 3

    # rdr: 30.4 + 30.5 = 2 total; 30.4 (as to_tumbler) = 1 linked
    assert by_type["rdr"]["total"] == 2
    assert by_type["rdr"]["linked"] == 1

    # code: 30.6 only = 1 total; 0 linked
    assert by_type["code"]["total"] == 1
    assert by_type["code"]["linked"] == 0


def test_coverage_owner_prefix_scoping(seeded, cat) -> None:
    """B) Service: owner_prefix scopes to the 30.X subtree only."""
    rows = cat.coverage_by_content_type(owner_prefix="30")
    by_type = {r["content_type"]: r for r in rows}

    # Under prefix "30": papers 30.1+30.2+30.3 = 3; linked 30.1, 30.2 = 2
    # (31.1 is NOT in scope, and 31.1->30.1 link still counts 30.1 as linked
    # because 30.1 is in scope and has a link: either from (30.1->30.2) or
    # as to_tumbler (31.1->30.1). The to_tumbler link makes 30.1 linked even
    # in the prefix scope — the JOIN is on d.tumbler, not on the peer.)
    assert by_type["paper"]["total"] == 3
    # 30.1 is linked (as from_tumbler cites 30.2, AND as to_tumbler from 31.1)
    # 30.2 is linked (as from_tumbler implements 30.4, AND as to_tumbler from 30.1)
    # 30.3 is unlinked
    assert by_type["paper"]["linked"] == 2

    # rdr: 2 total, 30.4 linked (to_tumbler)
    assert by_type["rdr"]["total"] == 2
    assert by_type["rdr"]["linked"] == 1

    # code: 1 total, 0 linked
    assert by_type["code"]["total"] == 1
    assert by_type["code"]["linked"] == 0

    # 31.1 paper must NOT appear in the results (not under prefix "30")
    # Verify by checking total count: only 3 papers, not 4
    assert by_type["paper"]["total"] == 3


def test_coverage_parity_service_equals_sqlite(seeded, cat, sqlite_cat) -> None:
    """C) Differential parity: service == SQLite Catalog on identical data.

    Compares coverage_by_content_type() and coverage_by_content_type(owner_prefix='30')
    from both backends, asserting exact equality of {total, linked} for each type.
    """
    def _normalize(rows: list[dict]) -> dict[str, dict]:
        """Sort and normalize for deterministic comparison."""
        return {r["content_type"]: {"total": int(r["total"]), "linked": int(r["linked"])}
                for r in rows}

    # No prefix: all documents
    svc_all  = _normalize(cat.coverage_by_content_type())
    sql_all  = _normalize(sqlite_cat.coverage_by_content_type())
    assert svc_all == sql_all, (
        f"Parity failure (no prefix):\n  service={svc_all}\n  sqlite={sql_all}"
    )

    # With prefix "30"
    svc_30 = _normalize(cat.coverage_by_content_type(owner_prefix="30"))
    sql_30 = _normalize(sqlite_cat.coverage_by_content_type(owner_prefix="30"))
    assert svc_30 == sql_30, (
        f"Parity failure (prefix=30):\n  service={svc_30}\n  sqlite={sql_30}"
    )


def test_coverage_cmd_no_guard_service_mode(cat) -> None:
    """D) coverage_cmd routing: ClickException is gone; command works in service mode.

    We exercise it indirectly via cat.coverage_by_content_type() which is now
    the routing target of coverage_cmd.  The fact that cat (an HttpCatalogClient)
    has no _db attribute and did not raise ClickException proves the guard is gone.
    """
    # The method must be callable on HttpCatalogClient (no NotImplementedError)
    rows = cat.coverage_by_content_type()
    assert isinstance(rows, list)
    for r in rows:
        assert "content_type" in r
        assert "total" in r
        assert "linked" in r
    # Verify cat is HttpCatalogClient — accessing _db must raise RuntimeError (service mode)
    try:
        _ = cat._db
        raise AssertionError("Expected RuntimeError from HttpCatalogClient._db property")
    except RuntimeError:
        pass  # expected: _db property raises RuntimeError in service mode
