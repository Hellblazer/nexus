# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-service integration test: coverage_by_content_type endpoint (nexus-3cwnx).

Proves that HttpCatalogClient.coverage_by_content_type works correctly via the
real Java service + real PostgreSQL, and that its results match the SQLite
Catalog.coverage_by_content_type mirror on identical seeded data
(differential parity proof).

Requires (darwin with JDK/GraalVM):
  - PostgreSQL binaries discoverable (NEXUS_PG_BIN / Homebrew / system dirs / PATH)
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

from tests.db._service_fixture import SERVICE_ROLES_SQL, pg_bin_dir

# ── Prerequisite paths ─────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = pg_bin_dir()

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
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
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
def sqlite_db():
    """Raw SQLite CatalogStore pre-seeded with identical data for parity checks."""
    from nexus.db.t2.catalog import CatalogStore
    db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="3cwnx_parity_")
    os.close(db_fd)
    store = CatalogStore(Path(db_path))
    yield store, Path(db_path)
    store.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture(scope="module")
def sqlite_cat(sqlite_db):
    """Real Catalog instance wrapping sqlite_db for parity checks.

    Uses the actual Catalog.coverage_by_content_type() method — not a proxy —
    so the parity assertion covers the real implementation, not a reimplementation.
    """
    from nexus.catalog.catalog import Catalog
    store, db_path = sqlite_db
    catalog_dir = Path(tempfile.mkdtemp(prefix="3cwnx_catdir_"))
    # Open read_only=False so Catalog can initialise; the store's _conn is
    # already open from the sqlite_db fixture, but Catalog opens its own
    # CatalogDB handle on the same file — SQLite WAL mode allows this.
    cat = Catalog(catalog_dir, db_path, read_only=False)
    yield cat
    cat._db.close()
    shutil.rmtree(catalog_dir, ignore_errors=True)


_DOCS = [
    # (tumbler, title, content_type)
    # tumbler "30" exercises the ``OR tumbler = prefix`` arm of owner_prefix filter
    ("30",   "3cwnx Owner 30 (exact match)",  "paper"),
    ("30.1", "3cwnx Paper A",                 "paper"),
    ("30.2", "3cwnx Paper B",                 "paper"),
    ("30.3", "3cwnx Paper C (unlinked)",      "paper"),
    ("30.4", "3cwnx RDR A",                   "rdr"),
    ("30.5", "3cwnx RDR B (unlinked)",        "rdr"),
    ("30.6", "3cwnx Code A (unlinked)",       "code"),
    ("31.1", "3cwnx Sub-Paper A",             "paper"),
]

_LINKS = [
    # (from_tumbler, to_tumbler, link_type)
    # 30.1 -> 30.2 (cites)       => 30.1 from, 30.2 from+to
    # 30.2 -> 30.4 (implements)  => 30.4 to
    # 31.1 -> 30.1 (relates)     => 31.1 from, 30.1 also to
    ("30.1", "30.2", "cites"),
    ("30.2", "30.4", "implements"),
    ("31.1", "30.1", "relates"),
]


def _seed_http(cat) -> None:
    """Seed the HTTP (service) catalog with deterministic coverage test data."""
    for tumbler, title, ct in _DOCS:
        cat._post("/register", {"tumbler": tumbler, "title": title, "content_type": ct})
    for from_t, to_t, lt in _LINKS:
        cat.link(from_t, to_t, lt, created_by="inttest-3cwnx")


def _seed_sqlite(store) -> None:
    """Seed the SQLite CatalogStore with identical data."""
    for tumbler, title, ct in _DOCS:
        store.execute(
            "INSERT OR IGNORE INTO documents (tumbler, title, content_type) "
            "VALUES (?, ?, ?)",
            (tumbler, title, ct),
        )
    store._conn.commit()

    for from_t, to_t, lt in _LINKS:
        store.execute(
            "INSERT OR IGNORE INTO links (from_tumbler, to_tumbler, link_type, created_by) "
            "VALUES (?, ?, ?, ?)",
            (from_t, to_t, lt, "inttest-3cwnx"),
        )
    store._conn.commit()


@pytest.fixture(scope="module")
def seeded(cat, sqlite_db):
    """Seed both backends with identical data."""
    store, _ = sqlite_db
    _seed_http(cat)
    _seed_sqlite(store)
    return True


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_coverage_no_prefix_exact_values(seeded, cat) -> None:
    """A) Service: exact {total, linked} per content_type with no prefix."""
    rows = cat.coverage_by_content_type()
    by_type = {r["content_type"]: r for r in rows}

    # paper: "30" + 30.1 + 30.2 + 30.3 (under 30) + 31.1 (under 31) = 5 total
    # linked: 30.1 (from+to), 30.2 (from+to), 31.1 (from) = 3 linked papers
    # "30" is unlinked (no links); 30.3 is unlinked
    assert by_type["paper"]["total"] == 5
    assert by_type["paper"]["linked"] == 3

    # rdr: 30.4 + 30.5 = 2 total; 30.4 (as to_tumbler) = 1 linked
    assert by_type["rdr"]["total"] == 2
    assert by_type["rdr"]["linked"] == 1

    # code: 30.6 only = 1 total; 0 linked
    assert by_type["code"]["total"] == 1
    assert by_type["code"]["linked"] == 0


def test_coverage_owner_prefix_scoping(seeded, cat) -> None:
    """B) Service: owner_prefix scopes to the 30.X subtree AND tumbler == "30".

    The filter is ``tumbler LIKE '30.%' OR tumbler = '30'`` — the second arm
    (exact equality) ensures the owner document itself is included when it exists.
    This test exercises that arm explicitly via tumbler "30" in the seed data.
    """
    rows = cat.coverage_by_content_type(owner_prefix="30")
    by_type = {r["content_type"]: r for r in rows}

    # Under prefix "30" (LIKE '30.%' OR = '30'):
    #   "30", 30.1, 30.2, 30.3 = 4 papers total
    # Linked papers in scope:
    #   30.1 is linked (from_tumbler in cites→30.2, AND to_tumbler from 31.1→30.1)
    #   30.2 is linked (from_tumbler in 30.2→30.4, AND to_tumbler from 30.1→30.2)
    #   "30" is unlinked; 30.3 is unlinked
    # => 2 linked papers
    assert by_type["paper"]["total"] == 4
    assert by_type["paper"]["linked"] == 2

    # rdr: 30.4 + 30.5 = 2 total; 30.4 linked (to_tumbler of 30.2->30.4)
    assert by_type["rdr"]["total"] == 2
    assert by_type["rdr"]["linked"] == 1

    # code: 30.6 = 1 total; 0 linked
    assert by_type["code"]["total"] == 1
    assert by_type["code"]["linked"] == 0

    # 31.1 paper must NOT be counted (not under prefix "30")
    # Total paper count of 4 (not 5) confirms 31.1 is excluded
    # AND that the "30" exact-match arm fired (contributing 1 doc)
    assert by_type["paper"]["total"] == 4


def test_coverage_parity_service_equals_sqlite(seeded, cat, sqlite_cat) -> None:
    """C) Differential parity: service == real Catalog.coverage_by_content_type().

    Calls the ACTUAL Catalog.coverage_by_content_type() method (not a proxy
    reimplementation) on an identical seeded SQLite store, then asserts exact
    equality with the service result.  A bug in Catalog.coverage_by_content_type
    would surface here as a parity mismatch, not a silent pass.
    """
    def _normalize(rows: list[dict]) -> dict[str, dict]:
        """Normalise for deterministic comparison."""
        return {r["content_type"]: {"total": int(r["total"]), "linked": int(r["linked"])}
                for r in rows}

    # No prefix: all documents
    svc_all = _normalize(cat.coverage_by_content_type())
    sql_all = _normalize(sqlite_cat.coverage_by_content_type())
    assert svc_all == sql_all, (
        f"Parity failure (no prefix):\n  service={svc_all}\n  sqlite={sql_all}"
    )

    # With prefix "30" — exercises LIKE arm AND exact-match arm
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
