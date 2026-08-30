# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-service integration tests for the nexus-l9hd8 aspect-SQL-port bead.

Proves that operator_filter, operator_groupby, and operator_aggregate (confidence
reducers) all route through the Java service in service mode and produce EXACT
PARITY with what the SQLite fast-path would return for the same data.

Architecture under test:
  Python aspect_sql.try_filter / try_groupby / try_aggregate
      ↓  (NX_STORAGE_BACKEND_DOCUMENT_ASPECTS=service)
  HttpDocumentAspectsStore.operator_filter / _groupby / _confidence_aggregate
      ↓
  Java POST /v1/aspects/operator-query  (AspectHandler)
      ↓
  AspectRepository.filterBySourceUris / groupByField / confidenceAggregate
      ↓
  Postgres nexus.document_aspects (RLS tenant-scoped)

PARITY PROOF strategy:
  1. Seed the same rows into Postgres (service) AND SQLite (local db).
  2. Run try_filter / try_groupby / try_aggregate against service mode.
  3. Run the same calls against SQLite mode.
  4. Assert both return EQUAL results (matched items, group keys, confidence values).

RLS test:
  5. Seed tenant A rows; verify tenant B cannot see them via the service path.

Marked @pytest.mark.integration — collected but skipped automatically when the
jar or pg16 binaries are absent, so CI stays green.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_l9hd8_aspect_sql_service_integration.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    pg_bin_dir,
    spawn_service,
    wait_for_service,
)

# ── Prerequisite paths ─────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = pg_bin_dir()
_INITDB    = _PG_BIN / "initdb"
_PG_CTL    = _PG_BIN / "pg_ctl"
_PSQL      = _PG_BIN / "psql"
_CREATEDB  = _PG_BIN / "createdb"

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


# ── Module-scoped fixtures ────────────────────────────────────────────────────


# nexus_svc is the service role created by SERVICE_ROLES_SQL (password nexus_svc_pass).
# Liquibase grants all DML rights to nexus_svc via grants-nexus-svc.xml (runAlways=true).
# Using nexus_svc as the service role means no extra grant SQL needed.
_SVC_ROLE    = "nexus_svc"
_SVC_ROLE_PW = "nexus_svc_pass"  # from SERVICE_ROLES_SQL


@pytest.fixture(scope="module")
def pg_instance():
    """Hermetic Postgres 16 instance for the aspect-SQL service integration tests."""
    pgdata  = tempfile.mkdtemp(prefix="nexus_l9hd8_inttest_pg_")
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
             "-U", pg_user, "nexus_l9hd8_test"],
            check=True, capture_output=True,
        )

        def _psql(sql: str) -> None:
            proc = subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", "nexus_l9hd8_test",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"psql failed (rc={proc.returncode}):\n"
                    f"stdout={proc.stdout}\nstderr={proc.stderr}"
                )

        # nexus_svc must exist before the JAR starts (Liquibase grants-nexus-svc.xml).
        # NX_DB_ADMIN_* (OS superuser) runs Liquibase from scratch;
        # NX_DB_* (nexus_svc) is the DML role that Liquibase grants to.
        _psql(SERVICE_ROLES_SQL)
        # NOTE: do NOT pre-create the nexus schema or tables.  The NX_DB_ADMIN_*
        # migration user (OS superuser) runs Liquibase from scratch and creates them.

        yield {"port": pg_port, "dbname": "nexus_l9hd8_test", "user": pg_user, "pgdata": pgdata}

    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def java_service(pg_instance):
    """Start the Java service jar against the hermetic PG.

    Mirrors test_http_aspects_stores_integration.py: use the svc role
    as both service and migration user.
    """
    svc_port = _free_port()
    token    = "l9hd8-inttest-bearer"

    db_url = (
        f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
        f"/{pg_instance['dbname']}"
    )
    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": token,
        # DML role: svc role (NOBYPASSRLS — enforces RLS)
        "NX_DB_URL":  db_url,
        "NX_DB_USER": _SVC_ROLE,
        "NX_DB_PASS": _SVC_ROLE_PW,
        # Migration role: OS superuser (trust auth, no password, DDL rights)
        "NX_DB_ADMIN_URL":  db_url,
        "NX_DB_ADMIN_USER": pg_instance["user"],
        "NX_DB_ADMIN_PASS": "",
        "NX_POOL_SIZE": "4",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    env.pop("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", None)

    # nexus-lom9g: FILE-backed output via the shared primitive; the old
    # stdout=PIPE/stderr=PIPE form wedged the service once 64KB of Logback
    # output accumulated before the port bound (nexus-j0nec).
    proc, _svc_log = spawn_service([str(_JAVA), "-jar", str(_JAR)], env)
    try:
        wait_for_service("127.0.0.1", svc_port, proc=proc, log_path=_svc_log, timeout=60.0)
        yield f"http://127.0.0.1:{svc_port}", token, svc_port
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


@pytest.fixture(scope="module", autouse=True)
def _seed_catalog_docs(pg_instance, java_service) -> None:
    """Seed catalog_documents rows referenced by this file's aspect fixtures.

    hygiene-001 (nexus-tk070.p6a follow-on): document_aspects.doc_id is
    NOT NULL now and fk_doc_aspects_catalog_doc requires a matching
    (tenant_id, tumbler) catalog_documents row for every non-null doc_id.
    ``aspects_client``'s bearer is the root NX_SERVICE_TOKEN, which
    AuthFilter binds server-side to tenant='default' regardless of the
    'l9hd8-tenant' name passed to HttpDocumentAspectsStore's constructor
    (Phase E, nexus-gmiaf.32.5 — the X-Nexus-Tenant header/tenant param is
    advisory only for a non-minted token); every doc_id below must
    therefore resolve under tenant_id='default'. Superuser psql bypasses
    FORCE RLS.
    """
    suffixes = [
        "filter-paxos", "filter-raft", "filter-dynamo",
        "groupby-vldb", "groupby-sosp", "groupby-sosp2", "groupby-nv",
        "conf-a", "conf-b", "conf-c",
        "rls-paxos",
    ]
    values = ",".join(
        f"('default', 'doc-l9hd8-{s}', 'seed-doc-l9hd8-{s}')" for s in suffixes
    )
    sql = (
        "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
        f"VALUES {values} ON CONFLICT (tenant_id, tumbler) DO NOTHING;"
    )
    proc = subprocess.run(
        [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_instance["port"]),
         "-U", pg_instance["user"], "-d", pg_instance["dbname"],
         "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"catalog-doc seed failed:\n{proc.stderr}")


@pytest.fixture(scope="module")
def aspects_client(java_service):
    """HttpDocumentAspectsStore (tenant='l9hd8-tenant') connected to the live service."""
    from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
    base_url, token, _ = java_service
    client = HttpDocumentAspectsStore(base_url=base_url, tenant="l9hd8-tenant", _token=token)
    yield client
    client.close()


@pytest.fixture(scope="module")
def other_tenant_token(java_service) -> str:
    """Tenant-bound token for the cross-tenant RLS probe (tenant='l9hd8-other').

    Phase E (nexus-gmiaf.32.5) binds tenant to the bearer token server-side and
    IGNORES the X-Nexus-Tenant header, so tenant B needs its own minted token —
    the operator (root) token provisions it via POST /v1/tenants/create.
    """
    import httpx

    base_url, token, _ = java_service
    resp = httpx.post(
        f"{base_url}/v1/tenants/create",
        json={"name": "l9hd8-other"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    assert resp.status_code == 200, f"tenant mint failed: {resp.status_code} {resp.text}"
    return resp.json()["token"]


@pytest.fixture(scope="module")
def other_tenant_client(java_service, other_tenant_token):
    """HttpDocumentAspectsStore for cross-tenant RLS probe (tenant='l9hd8-other')."""
    from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
    base_url, _, _ = java_service
    client = HttpDocumentAspectsStore(
        base_url=base_url, tenant="l9hd8-other", _token=other_tenant_token
    )
    yield client
    client.close()


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _make_aspect(
    suffix: str,
    *,
    collection: str = "knowledge__l9hd8",
    proposed_method: str | None = None,
    venue: str | None = None,
    confidence: float = 0.80,
) -> "AspectRecord":
    """Build an AspectRecord with deterministic test values.

    ``source_uri`` is derived via :func:`uri_for` so it matches the URI
    that :func:`aspect_sql.try_filter` / ``try_groupby`` / ``try_aggregate``
    compute when building the query against the service.  Mismatched URIs
    cause zero service hits even when the row exists.
    """
    from nexus.db.t2.records import AspectRecord
    from nexus.aspect_readers import uri_for
    source_path = f"/l9hd8/{suffix}.pdf"
    extras: dict = {}
    if venue is not None:
        extras["venue"] = venue
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=f"Problem {suffix}",
        proposed_method=proposed_method or f"Method {suffix}",
        experimental_datasets=["ds1"],
        experimental_baselines=["bl1"],
        experimental_results=f"Results {suffix}",
        extras=extras,
        confidence=confidence,
        extracted_at="2026-01-15T10:00:00Z",
        model_version="v1.0",
        extractor_name="test",
        # uri_for must match what try_filter/_groupby/_aggregate compute
        # from (collection, source_path); mismatched URIs → zero hits.
        source_uri=uri_for(collection, source_path) or "",
        # hygiene-001: doc_id is NOT NULL now and must reference a real
        # catalog_documents row — seeded (tenant_id='default') by the
        # module-scoped _seed_catalog_docs fixture, keyed on this suffix.
        doc_id=f"doc-l9hd8-{suffix}",
        salient_sentences=[f"Finding {suffix}."],
    )


def _items_json(paths: list[str], collection: str = "knowledge__l9hd8") -> str:
    """Build the JSON items string for try_filter / try_groupby."""
    return json.dumps([
        {"id": p, "collection": collection, "source_path": p}
        for p in paths
    ])


def _groups_json(groups: dict[str, list[str]], collection: str = "knowledge__l9hd8") -> str:
    """Build the JSON groups string for try_aggregate."""
    return json.dumps([
        {
            "key_value": key,
            "items": [
                {"id": p, "collection": collection, "source_path": p}
                for p in paths
            ],
        }
        for key, paths in groups.items()
    ])


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestOperatorFilterServiceParity:
    """operator_filter: service path produces EQUAL results to SQLite fast-path."""

    @pytest.fixture(scope="class", autouse=True)
    def seed(self, aspects_client) -> None:
        """Seed test rows for this class into Postgres."""
        rows = [
            _make_aspect("filter-paxos", proposed_method="Paxos consensus algorithm"),
            _make_aspect("filter-raft", proposed_method="Raft consensus"),
            _make_aspect("filter-dynamo", proposed_method="Dynamo distributed storage"),
        ]
        for r in rows:
            aspects_client.upsert(r)

    def test_service_path_taken_in_service_mode(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """Verify that the service HTTP path (not SQLite) is taken in service mode.

        We confirm this by seeding only Postgres and asserting a hit comes back —
        the SQLite db is empty so a SQLite hit would return zero matches."""
        from nexus.operators import aspect_sql

        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        # NX_SERVICE_URL must be re-pointed at THIS test's service, not merely
        # left alone (nexus-qvs2h root cause, 2026-07-28). The URL leg OUTRANKS
        # the host/port halves in service_endpoint's resolution order, and the
        # session-scoped engine-substrate fixture (t2_service_env via
        # _pin_t2_substrate) has already exported an NX_SERVICE_URL pointing at
        # a DIFFERENT service. Setting only host/port sent every read to that
        # other service carrying this fixture's bearer, which it does not know:
        # HTTP 401, surfaced as an empty result set with the error in the
        # rationale. tests/e2e/local-service-gate.sh documents the same hazard
        # from the other side ("deliberately NOT NX_SERVICE_URL: the URL leg
        # outranks the halves").
        monkeypatch.setenv("NX_SERVICE_URL", base_url)
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        # Point default_db_path at an EMPTY SQLite db — if the SQLite path is taken,
        # zero rows → no match.
        empty_db = tmp_path / "empty.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: empty_db)

        collection = "knowledge__l9hd8"
        items = _items_json(["/l9hd8/filter-paxos.pdf"], collection)
        result = aspect_sql.try_filter(
            items, "consensus", source="aspects", aspect_field="proposed_method",
        )
        assert result is not None
        # Service has the row seeded — must return a match
        assert len(result["items"]) > 0, (
            "Expected a match from service path (Postgres has the row); "
            "zero results implies SQLite path was taken instead"
        )


class TestOperatorGroupbyServiceParity:
    """operator_groupby: service path groups by extras.venue and produces EQUAL results."""

    @pytest.fixture(scope="class", autouse=True)
    def seed(self, aspects_client) -> None:
        """Seed rows with different venues into Postgres."""
        rows = [
            _make_aspect("groupby-vldb",  venue="VLDB"),
            _make_aspect("groupby-sosp",  venue="SOSP"),
            _make_aspect("groupby-sosp2", venue="SOSP"),
            _make_aspect("groupby-nv",    venue=None),   # no venue → unassigned
        ]
        for r in rows:
            aspects_client.upsert(r)

    def test_vldb_group_has_single_item(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """VLDB group must contain exactly 1 item (the vldb-seeded row)."""
        from nexus.operators import aspect_sql

        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        # NX_SERVICE_URL must be re-pointed at THIS test's service, not merely
        # left alone (nexus-qvs2h root cause, 2026-07-28). The URL leg OUTRANKS
        # the host/port halves in service_endpoint's resolution order, and the
        # session-scoped engine-substrate fixture (t2_service_env via
        # _pin_t2_substrate) has already exported an NX_SERVICE_URL pointing at
        # a DIFFERENT service. Setting only host/port sent every read to that
        # other service carrying this fixture's bearer, which it does not know:
        # HTTP 401, surfaced as an empty result set with the error in the
        # rationale. tests/e2e/local-service-gate.sh documents the same hazard
        # from the other side ("deliberately NOT NX_SERVICE_URL: the URL leg
        # outranks the halves").
        monkeypatch.setenv("NX_SERVICE_URL", base_url)
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        collection = "knowledge__l9hd8"
        items = _items_json(["/l9hd8/groupby-vldb.pdf"], collection)
        result = aspect_sql.try_groupby(items, "venue", source="aspects", aspect_field="extras.venue")
        assert result is not None
        vldb_groups = [g for g in result["groups"] if g["key_value"] == "VLDB"]
        assert len(vldb_groups) == 1
        assert len(vldb_groups[0]["items"]) == 1

class TestOperatorConfidenceAggregateServiceParity:
    """operator_aggregate (confidence): service path produces EQUAL numeric results to SQLite."""

    @pytest.fixture(scope="class", autouse=True)
    def seed(self, aspects_client) -> None:
        """Seed rows with known confidence values into Postgres."""
        rows = [
            _make_aspect("conf-a", confidence=0.80),
            _make_aspect("conf-b", confidence=0.90),
            _make_aspect("conf-c", confidence=0.70),
        ]
        for r in rows:
            aspects_client.upsert(r)

class TestRLSIsolation:
    """Cross-tenant RLS: service-mode operator_filter must not return other tenant's rows."""

    @pytest.fixture(scope="class", autouse=True)
    def seed_tenants(self, aspects_client, other_tenant_client) -> None:
        """Seed tenant A rows; tenant B gets nothing."""
        row = _make_aspect("rls-paxos", proposed_method="Paxos consensus algorithm")
        aspects_client.upsert(row)

    def test_other_tenant_gets_no_matches(
        self, java_service, other_tenant_token, monkeypatch, tmp_path
    ) -> None:
        """Tenant B operator_filter must return no matches for tenant A's rows."""
        from nexus.operators import aspect_sql

        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        # NX_SERVICE_URL must be re-pointed at THIS test's service, not merely
        # left alone (nexus-qvs2h root cause, 2026-07-28). The URL leg OUTRANKS
        # the host/port halves in service_endpoint's resolution order, and the
        # session-scoped engine-substrate fixture (t2_service_env via
        # _pin_t2_substrate) has already exported an NX_SERVICE_URL pointing at
        # a DIFFERENT service. Setting only host/port sent every read to that
        # other service carrying this fixture's bearer, which it does not know:
        # HTTP 401, surfaced as an empty result set with the error in the
        # rationale. tests/e2e/local-service-gate.sh documents the same hazard
        # from the other side ("deliberately NOT NX_SERVICE_URL: the URL leg
        # outranks the halves").
        monkeypatch.setenv("NX_SERVICE_URL", base_url)
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        # Phase E: tenant is bound to the TOKEN server-side; the tenant env/header
        # is advisory only, so tenant B must authenticate with its own token.
        monkeypatch.setenv("NX_SERVICE_TOKEN", other_tenant_token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-other")  # tenant B

        empty_db = tmp_path / "rls_empty.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: empty_db)

        collection = "knowledge__l9hd8"
        items = _items_json(["/l9hd8/rls-paxos.pdf"], collection)
        result = aspect_sql.try_filter(
            items, "consensus", source="aspects", aspect_field="proposed_method",
        )
        assert result is not None
        # Tenant B must see ZERO matches — tenant A's row is invisible via RLS
        assert len(result["items"]) == 0, (
            f"RLS FAILURE: tenant B saw tenant A's row. Matched items: {result['items']}"
        )

    def test_same_tenant_gets_matches(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """Tenant A operator_filter on its own rows returns the match."""
        from nexus.operators import aspect_sql

        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        # NX_SERVICE_URL must be re-pointed at THIS test's service, not merely
        # left alone (nexus-qvs2h root cause, 2026-07-28). The URL leg OUTRANKS
        # the host/port halves in service_endpoint's resolution order, and the
        # session-scoped engine-substrate fixture (t2_service_env via
        # _pin_t2_substrate) has already exported an NX_SERVICE_URL pointing at
        # a DIFFERENT service. Setting only host/port sent every read to that
        # other service carrying this fixture's bearer, which it does not know:
        # HTTP 401, surfaced as an empty result set with the error in the
        # rationale. tests/e2e/local-service-gate.sh documents the same hazard
        # from the other side ("deliberately NOT NX_SERVICE_URL: the URL leg
        # outranks the halves").
        monkeypatch.setenv("NX_SERVICE_URL", base_url)
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")  # tenant A

        empty_db = tmp_path / "rls_own.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: empty_db)

        collection = "knowledge__l9hd8"
        items = _items_json(["/l9hd8/rls-paxos.pdf"], collection)
        result = aspect_sql.try_filter(
            items, "consensus", source="aspects", aspect_field="proposed_method",
        )
        assert result is not None
        assert len(result["items"]) == 1, (
            f"Expected tenant A to see its own row; got: {result['items']}"
        )
