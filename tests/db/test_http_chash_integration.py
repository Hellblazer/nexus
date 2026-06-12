# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language integration test for HttpChashIndex against the real Java service.

Requires (on THIS machine — darwin/aarch64 with JDK25 GraalVM):
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (mvn -f service/pom.xml package -DskipTests)
  - Java on PATH (or JAVA_HOME/bin/java available)

Marked @pytest.mark.integration — collected but skipped automatically when the
jar or pg16 binaries are absent, so CI (which has neither) stays green.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_http_chash_integration.py -v

What is exercised (bead nexus-gmiaf.16 gate requirements):
  a) upsert + lookup round-trip (same chash in multiple collections)
  b) upsert_many batch round-trip
  c) delete_collection removes rows; absent collection returns 0
  d) distinct_collections across upserts
  e) rename_collection re-points rows
  f) delete_stale removes specific PK; absent PK returns 0
  g) is_empty and count_for_collection
  h) Cross-tenant RLS negative: default tenant rows invisible to other-tenant
  i) RLS isolation: a genuine other-tenant write is invisible to default
  j) ETL fidelity: migrate_chash_rows preserves created_at verbatim
  k) ETL idempotent re-run: second pass produces no duplicates
  l) Phase E: tenant comes from the bearer, not the X-Nexus-Tenant header
     (the unset-GUC fail-closed property lives at the repo layer —
      ChashRepositoryTest / ChunksRlsBehavioralTest)

NX_STORAGE_BACKEND is NOT touched — default SQLite path is unchanged.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL, create_tenant_token

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


# ── Module-scoped fixtures ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Spin up a hermetic Postgres 16 instance."""
    pgdata  = tempfile.mkdtemp(prefix="nexus_chash_inttest_pg_")
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
             "-U", pg_user, "nexuschashtest"],
            check=True, capture_output=True,
        )

        def _psql(sql: str) -> None:
            proc = subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", "nexuschashtest",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"psql bootstrap failed (rc={proc.returncode}):\n"
                    f"stdout={proc.stdout}\nstderr={proc.stderr}"
                )

        # net63: the JAR runs Liquibase at startup and owns the full chash schema
        # + grants before binding the HTTP port. The fixture must NOT pre-apply schema
        # — doing so collides ("relation already exists") and the service exits at
        # migration. The only pre-start SQL is SERVICE_ROLES_SQL, which creates
        # nexus_svc (the NOSUPERUSER NOBYPASSRLS DML/RLS role grants-nexus-svc.xml
        # grants to, and the role the RLS-negative tests use).
        _psql(SERVICE_ROLES_SQL)

        yield {"port": pg_port, "dbname": "nexuschashtest", "user": pg_user,
               "pgdata": pgdata}

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
    token    = "chash-inttest-bearer-secret"

    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        # net63 two-role: app pool = nexus_svc (NOSUPERUSER NOBYPASSRLS → FORCE RLS
        # applies); migration pool = OS superuser (trust auth) for the Liquibase DDL.
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "3",
        "NX_DB_ADMIN_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_ADMIN_USER": pg_instance["user"],
        "NX_DB_ADMIN_PASS": "",
        "NX_CHROMA_PATH": tempfile.mkdtemp(prefix="nexus-chash-chroma-"),
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
def chash_store(service):
    """HttpChashIndex (tenant='default') connected to the real Java service."""
    from nexus.db.t2.http_chash_index import HttpChashIndex
    base_url, token, _ = service
    s = HttpChashIndex(base_url=base_url, _token=token, tenant="default")
    yield s
    s.close()


@pytest.fixture(scope="module")
def other_chash_store(service):
    """HttpChashIndex for the cross-tenant RLS probe (tenant='other-tenant')."""
    from nexus.db.t2.http_chash_index import HttpChashIndex
    base_url, token, _ = service
    # Phase E: real other-tenant-bound bearer (mirrors `nx tenant create`).
    other_token = create_tenant_token(base_url, token, "other-tenant")
    s = HttpChashIndex(base_url=base_url, _token=other_token, tenant="other-tenant")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean_collections(chash_store):
    """Delete all known collections between tests to avoid cross-test pollution."""
    for coll in list(chash_store.distinct_collections()):
        chash_store.delete_collection(coll)
    yield
    for coll in list(chash_store.distinct_collections()):
        chash_store.delete_collection(coll)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestChashMVV:
    """Minimum viable verification for the chash_index service (nexus-gmiaf.16)."""

    def test_a_upsert_lookup_roundtrip(self, chash_store):
        """a) upsert + lookup: same chash can live in multiple collections."""
        chash_store.upsert(chash="sha256abc001", collection="col_a")
        chash_store.upsert(chash="sha256abc001", collection="col_b")

        rows = chash_store.lookup("sha256abc001")
        colls = {r["collection"] for r in rows}
        assert colls == {"col_a", "col_b"}, (
            f"lookup must return both collections; got {colls!r}"
        )

    def test_a2_lookup_unknown_returns_empty(self, chash_store):
        """a2) lookup for absent chash returns []."""
        rows = chash_store.lookup("nosuch00000000000000000000000000")
        assert rows == []

    def test_b_upsert_many_batch(self, chash_store):
        """b) upsert_many round-trip."""
        chashes = [f"hash{i:04d}" for i in range(5)]
        chash_store.upsert_many(chashes=chashes, collection="col_batch")

        assert chash_store.count_for_collection("col_batch") == 5

    def test_c_delete_collection(self, chash_store):
        """c) delete_collection removes all rows for a collection."""
        chash_store.upsert(chash="c1", collection="col_del")
        chash_store.upsert(chash="c2", collection="col_del")
        chash_store.upsert(chash="c3", collection="col_other")

        deleted = chash_store.delete_collection("col_del")
        assert deleted == 2

        assert chash_store.count_for_collection("col_del") == 0
        assert chash_store.count_for_collection("col_other") == 1

    def test_c2_delete_collection_absent_returns_zero(self, chash_store):
        """c2) delete_collection on absent collection returns 0."""
        assert chash_store.delete_collection("no_such_collection") == 0

    def test_d_distinct_collections(self, chash_store):
        """d) distinct_collections returns all unique collection names."""
        chash_store.upsert(chash="c1", collection="col_x")
        chash_store.upsert(chash="c2", collection="col_y")
        chash_store.upsert(chash="c3", collection="col_x")

        result = chash_store.distinct_collections()
        assert "col_x" in result
        assert "col_y" in result

    def test_e_rename_collection(self, chash_store):
        """e) rename_collection re-points all rows from old -> new."""
        chash_store.upsert(chash="c1", collection="old_col")
        chash_store.upsert(chash="c2", collection="old_col")

        updated = chash_store.rename_collection(old="old_col", new="new_col")
        assert updated == 2

        assert chash_store.count_for_collection("old_col") == 0
        assert chash_store.count_for_collection("new_col") == 2

    def test_f_delete_stale_specific_pk(self, chash_store):
        """f) delete_stale removes only the specific (chash, collection) row."""
        chash_store.upsert(chash="c1", collection="col_a")
        chash_store.upsert(chash="c1", collection="col_b")

        deleted = chash_store.delete_stale(chash="c1", collection="col_a")
        assert deleted == 1
        assert chash_store.count_for_collection("col_a") == 0
        assert chash_store.count_for_collection("col_b") == 1

    def test_f2_delete_stale_absent_returns_zero(self, chash_store):
        """f2) delete_stale on absent PK returns 0 (idempotent)."""
        assert chash_store.delete_stale(chash="ghost", collection="nowhere") == 0

    def test_g_is_empty_and_count(self, chash_store):
        """g) is_empty + count_for_collection."""
        assert chash_store.is_empty() is True

        chash_store.upsert(chash="c1", collection="col_g")
        assert chash_store.is_empty() is False
        assert chash_store.count_for_collection("col_g") == 1

    def test_h_cross_tenant_rls_negative(self, chash_store, other_chash_store):
        """h) rows inserted by tenant 'default' are invisible to 'other-tenant'."""
        chash_store.upsert(chash="rls_probe_ch", collection="col_rls")

        # default can see it
        rows = chash_store.lookup("rls_probe_ch")
        assert len(rows) == 1, "default tenant must see its own row"

        # other-tenant must NOT see it
        other_rows = other_chash_store.lookup("rls_probe_ch")
        assert other_rows == [], (
            f"Cross-tenant RLS must filter: 'other-tenant' must not see 'default' "
            f"rows; got {other_rows!r}"
        )

    def test_i_other_tenant_write_invisible_to_default(self, service):
        """i) RLS isolation: a genuine other-tenant write is invisible to default."""
        import httpx

        base_url, token, _ = service
        # Phase E (nexus-gmiaf.32.5): tenant_id is taken from the AUTHENTICATED
        # bearer, not the X-Nexus-Tenant header. A genuine evil-tenant write
        # therefore needs an evil-tenant-bound bearer (mirrors `nx tenant create`);
        # the header alone resolves back to the bearer's tenant.
        evil_token = create_tenant_token(base_url, token, "evil-tenant")
        evil_headers = {
            "Authorization": f"Bearer {evil_token}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(
            f"{base_url}/v1/chash/upsert",
            headers=evil_headers,
            json={"chash": "evil_ch", "collection": "evil_col"},
        )
        # The upsert itself should succeed (evil-tenant's RLS allows own writes)
        assert resp.status_code == 200

        # But default tenant must NOT see that row
        from nexus.db.t2.http_chash_index import HttpChashIndex
        s = HttpChashIndex(base_url=base_url, _token=token, tenant="default")
        rows = s.lookup("evil_ch")
        assert rows == [], (
            f"RLS must isolate: default tenant must not see evil-tenant row; got {rows!r}"
        )
        s.close()

    def test_j_etl_fidelity_preserves_created_at(self, service, tmp_path, chash_store):
        """j) ETL: migrate_chash_rows preserves created_at verbatim."""
        from nexus.db.t2.chash_etl import migrate_chash_rows
        from nexus.db.t2.http_chash_index import HttpChashIndex

        ts = "2024-03-15T08:00:00Z"
        db = tmp_path / "t2_etl_fidelity.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE chash_index "
            "(chash TEXT, physical_collection TEXT, created_at TEXT)"
        )
        conn.execute(f"INSERT INTO chash_index VALUES ('etl_sha001', 'col_etl', '{ts}')")
        conn.commit()
        conn.close()

        base_url, token, _ = service
        store = HttpChashIndex(base_url=base_url, _token=token, tenant="default")
        result = migrate_chash_rows(db, store)
        store.close()

        assert result["total"]    == 1
        assert result["imported"] == 1
        assert result["errors"]   == 0

        rows = chash_store.lookup("etl_sha001")
        assert len(rows) == 1
        assert rows[0]["created_at"] == ts, (
            f"created_at must be preserved verbatim; got {rows[0]['created_at']!r}"
        )

    def test_k_etl_idempotent_rerun(self, service, tmp_path, chash_store):
        """k) Running ETL twice produces no duplicates (idempotent upsert)."""
        from nexus.db.t2.chash_etl import migrate_chash_rows
        from nexus.db.t2.http_chash_index import HttpChashIndex

        db = tmp_path / "t2_etl_idem.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE chash_index "
            "(chash TEXT, physical_collection TEXT, created_at TEXT)"
        )
        conn.execute("INSERT INTO chash_index VALUES ('idem_sha001', 'col_idem', '2024-01-01T00:00:00Z')")
        conn.execute("INSERT INTO chash_index VALUES ('idem_sha002', 'col_idem', '2024-01-02T00:00:00Z')")
        conn.commit()
        conn.close()

        base_url, token, _ = service
        store = HttpChashIndex(base_url=base_url, _token=token, tenant="default")
        r1 = migrate_chash_rows(db, store)
        r2 = migrate_chash_rows(db, store)
        store.close()

        assert r1["imported"] == 2
        assert r2["imported"] == 2  # idempotent: upserts same rows

        # Only 2 distinct rows after double ETL (no duplication)
        assert chash_store.count_for_collection("col_idem") == 2

    def test_l_tenant_from_bearer_not_header(self, service):
        """l) Phase E: the X-Nexus-Tenant header is advisory-only — the bearer is
        authoritative. A request with NO header is valid (not 400), and a SPOOFED
        header cannot redirect the request to another tenant's view.

        Pre-Phase-E this asserted fail-closed-on-missing-header; the header was
        load-bearing then. Phase E (nexus-gmiaf.32.5) made the bearer
        authoritative; the unset-GUC fail-closed property moved to the SQL layer
        (ChashRepositoryTest / ChunksRlsBehavioralTest). Here we pin the HTTP
        contract: header omitted → 200, and a spoofed header yields the IDENTICAL
        bearer-scoped result (so it cannot leak/cross into another tenant).
        """
        import httpx

        base_url, token, _ = service

        # No tenant header → valid, scoped to the bearer (default).
        bare = httpx.get(
            f"{base_url}/v1/chash/is_empty",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert bare.status_code == 200, (
            f"missing X-Nexus-Tenant is valid under Phase E (bearer is "
            f"authoritative); expected 200, got {bare.status_code}: {bare.text[:200]}"
        )

        # Spoofed tenant header must be IGNORED — identical result to no header.
        # If the header were load-bearing this would switch to 'evil-spoof''s
        # (empty) view and diverge; identical bodies prove bearer-scoping.
        spoofed = httpx.get(
            f"{base_url}/v1/chash/is_empty",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Nexus-Tenant": "evil-spoof",
            },
        )
        assert spoofed.status_code == 200
        assert spoofed.json() == bare.json(), (
            "a spoofed X-Nexus-Tenant header must NOT change the tenant view — "
            f"bearer is authoritative; got bare={bare.json()} spoofed={spoofed.json()}"
        )

    def test_m_registered_chashes_for_collection(self, chash_store):
        """m) registered_chashes_for_collection returns chash[:32] set for collection.

        Mirrors ChashIndex.registered_chashes_for_collection: 32-char prefix
        matches Chroma natural-ID shape (RDR-108 D1). Exercises the new
        GET /v1/chash/registered_chashes endpoint end-to-end.
        """
        # Insert rows for col_reg
        chash_store.upsert(chash="reg_chash001", collection="col_reg")
        chash_store.upsert(chash="reg_chash002", collection="col_reg")
        chash_store.upsert(chash="other_chash", collection="col_other")

        result = chash_store.registered_chashes_for_collection("col_reg")

        assert "reg_chash001" in result, f"reg_chash001 must be in result; got {result!r}"
        assert "reg_chash002" in result
        assert "other_chash" not in result, "other_chash must not appear for col_reg"

    def test_m2_registered_chashes_unknown_collection(self, chash_store):
        """m2) registered_chashes_for_collection returns empty for absent collection."""
        result = chash_store.registered_chashes_for_collection("col_no_such")
        assert result == set()

    def test_m3_registered_chashes_rls_isolation(self, chash_store, other_chash_store):
        """m3) registered_chashes_for_collection is RLS-isolated per tenant."""
        chash_store.upsert(chash="rls_reg_chash", collection="col_rls_reg")

        # default tenant sees its own row
        own = chash_store.registered_chashes_for_collection("col_rls_reg")
        assert "rls_reg_chash" in own

        # other-tenant gets empty (RLS filter)
        other = other_chash_store.registered_chashes_for_collection("col_rls_reg")
        assert other == set(), (
            f"RLS must isolate registered_chashes: 'other-tenant' must get empty; got {other!r}"
        )
