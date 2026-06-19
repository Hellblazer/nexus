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

from tests.db._service_fixture import SERVICE_ROLES_SQL

# ── Prerequisite paths ─────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = Path("/opt/homebrew/opt/postgresql@16/bin")
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
            "skipped: missing jar or pg16 binaries "
            f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
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

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=30.0)
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


@pytest.fixture(scope="module")
def aspects_client(java_service):
    """HttpDocumentAspectsStore (tenant='l9hd8-tenant') connected to the live service."""
    from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
    base_url, token, _ = java_service
    client = HttpDocumentAspectsStore(base_url=base_url, tenant="l9hd8-tenant", _token=token)
    yield client
    client.close()


@pytest.fixture(scope="module")
def other_tenant_client(java_service):
    """HttpDocumentAspectsStore for cross-tenant RLS probe (tenant='l9hd8-other')."""
    from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
    base_url, token, _ = java_service
    client = HttpDocumentAspectsStore(base_url=base_url, tenant="l9hd8-other", _token=token)
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
    from nexus.db.t2.document_aspects import AspectRecord
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
        # doc_id left as "" (default) → nullIfBlank in Java → SQL NULL → satisfies FK
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


def _seed_sqlite(tmp_db_path: Path, rows: list) -> None:
    """Seed aspect rows into a SQLite T2 database for parity testing."""
    from nexus.db.t2.document_aspects import DocumentAspects

    store = DocumentAspects(tmp_db_path)
    for rec in rows:
        store.upsert(rec)
    store.close()


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

    def test_filter_matched_items_equal_sqlite(
        self, aspects_client, java_service, monkeypatch, tmp_path
    ) -> None:
        """Service filter results == SQLite filter results for the same data."""
        from nexus.operators import aspect_sql

        collection = "knowledge__l9hd8"
        paths = ["/l9hd8/filter-paxos.pdf", "/l9hd8/filter-raft.pdf", "/l9hd8/filter-dynamo.pdf"]
        items = _items_json(paths, collection)

        # --- Service path ---
        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        svc_result = aspect_sql.try_filter(
            items, "consensus", source="aspects", aspect_field="proposed_method",
        )
        assert svc_result is not None, f"Service filter returned None; expected dict"
        svc_paths = {item["source_path"] for item in svc_result["items"]}

        # --- SQLite path ---
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)

        from nexus.db.t2.document_aspects import AspectRecord
        from nexus.aspect_readers import uri_for as _uri_for
        rows = [
            AspectRecord(
                collection=collection, source_path=p,
                problem_formulation="P", proposed_method=m,
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="R", extras={},
                confidence=0.8, extracted_at="2026-01-15T10:00:00Z",
                model_version="v1", extractor_name="test",
                source_uri=_uri_for(collection, p) or "", doc_id=f"doc{i}",
                salient_sentences=[],
            )
            for i, (p, m) in enumerate([
                ("/l9hd8/filter-paxos.pdf", "Paxos consensus algorithm"),
                ("/l9hd8/filter-raft.pdf", "Raft consensus"),
                ("/l9hd8/filter-dynamo.pdf", "Dynamo distributed storage"),
            ])
        ]
        sqlite_db = tmp_path / "parity.db"
        _seed_sqlite(sqlite_db, rows)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: sqlite_db)

        sqlite_result = aspect_sql.try_filter(
            items, "consensus", source="aspects", aspect_field="proposed_method",
        )
        assert sqlite_result is not None, "SQLite filter returned None"
        sqlite_paths = {item["source_path"] for item in sqlite_result["items"]}

        # PARITY: same matched paths
        assert svc_paths == sqlite_paths, (
            f"PARITY FAILURE: service matched {svc_paths}, SQLite matched {sqlite_paths}"
        )
        # Both must match paxos + raft (contain "consensus"), not dynamo
        assert "/l9hd8/filter-paxos.pdf" in svc_paths
        assert "/l9hd8/filter-raft.pdf" in svc_paths
        assert "/l9hd8/filter-dynamo.pdf" not in svc_paths

    def test_service_path_taken_in_service_mode(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """Verify that the service HTTP path (not SQLite) is taken in service mode.

        We confirm this by seeding only Postgres and asserting a hit comes back —
        the SQLite db is empty so a SQLite hit would return zero matches."""
        from nexus.operators import aspect_sql

        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
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

    def test_groupby_keys_equal_sqlite(
        self, aspects_client, java_service, monkeypatch, tmp_path
    ) -> None:
        """Service groupby keys == SQLite groupby keys for the same data."""
        from nexus.operators import aspect_sql

        collection = "knowledge__l9hd8"
        paths = [
            "/l9hd8/groupby-vldb.pdf",
            "/l9hd8/groupby-sosp.pdf",
            "/l9hd8/groupby-sosp2.pdf",
            "/l9hd8/groupby-nv.pdf",
        ]
        items = _items_json(paths, collection)

        # --- Service path ---
        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        svc_result = aspect_sql.try_groupby(
            items, "venue", source="aspects", aspect_field="extras.venue",
        )
        assert svc_result is not None
        svc_keys = {g["key_value"] for g in svc_result["groups"]}

        # --- SQLite path ---
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)

        from nexus.db.t2.document_aspects import AspectRecord
        from nexus.aspect_readers import uri_for as _uri_for
        sqlite_rows = []
        for path, venue in [
            ("/l9hd8/groupby-vldb.pdf", "VLDB"),
            ("/l9hd8/groupby-sosp.pdf", "SOSP"),
            ("/l9hd8/groupby-sosp2.pdf", "SOSP"),
            ("/l9hd8/groupby-nv.pdf", None),
        ]:
            extras = {"venue": venue} if venue else {}
            sqlite_rows.append(AspectRecord(
                collection=collection, source_path=path,
                problem_formulation="P", proposed_method="M",
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="R", extras=extras,
                confidence=0.8, extracted_at="2026-01-15T10:00:00Z",
                model_version="v1", extractor_name="test",
                source_uri=_uri_for(collection, path) or "", doc_id=f"doc-gb-{path[-7:-4]}",
                salient_sentences=[],
            ))

        sqlite_db = tmp_path / "groupby_parity.db"
        _seed_sqlite(sqlite_db, sqlite_rows)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: sqlite_db)

        sqlite_result = aspect_sql.try_groupby(
            items, "venue", source="aspects", aspect_field="extras.venue",
        )
        assert sqlite_result is not None
        sqlite_keys = {g["key_value"] for g in sqlite_result["groups"]}

        # PARITY: same group keys
        assert svc_keys == sqlite_keys, (
            f"PARITY FAILURE: service groups {svc_keys}, SQLite groups {sqlite_keys}"
        )
        # Both must have VLDB and SOSP; absent-venue row lands in "unassigned"
        assert "VLDB" in svc_keys
        assert "SOSP" in svc_keys
        assert "unassigned" in svc_keys

    def test_vldb_group_has_single_item(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """VLDB group must contain exactly 1 item (the vldb-seeded row)."""
        from nexus.operators import aspect_sql

        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
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

    def test_groupby_contents_equal_sqlite(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """DIFFERENTIAL: service groupby group item-sets == SQLite group item-sets.

        This tests that not just the group KEYS match (tested in test_groupby_keys_equal_sqlite)
        but also the source_paths within each group are identical across backends.
        """
        from nexus.operators import aspect_sql

        collection = "knowledge__l9hd8"
        paths = [
            "/l9hd8/groupby-vldb.pdf",
            "/l9hd8/groupby-sosp.pdf",
            "/l9hd8/groupby-sosp2.pdf",
            "/l9hd8/groupby-nv.pdf",
        ]
        items = _items_json(paths, collection)

        # --- Service path ---
        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        svc_result = aspect_sql.try_groupby(
            items, "venue", source="aspects", aspect_field="extras.venue",
        )
        assert svc_result is not None

        # Build {key_value: frozenset(source_paths)} from service result
        def _groups_to_map(result):
            out = {}
            for g in result["groups"]:
                kv = g["key_value"]
                out[kv] = frozenset(
                    item["source_path"] for item in g["items"]
                )
            return out

        svc_map = _groups_to_map(svc_result)

        # --- SQLite path (same fixture) ---
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)

        from nexus.db.t2.document_aspects import AspectRecord
        from nexus.aspect_readers import uri_for as _uri_for
        sqlite_rows = []
        for path, venue in [
            ("/l9hd8/groupby-vldb.pdf", "VLDB"),
            ("/l9hd8/groupby-sosp.pdf", "SOSP"),
            ("/l9hd8/groupby-sosp2.pdf", "SOSP"),
            ("/l9hd8/groupby-nv.pdf", None),
        ]:
            extras = {"venue": venue} if venue else {}
            sqlite_rows.append(AspectRecord(
                collection=collection, source_path=path,
                problem_formulation="P", proposed_method="M",
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="R", extras=extras,
                confidence=0.8, extracted_at="2026-01-15T10:00:00Z",
                model_version="v1", extractor_name="test",
                source_uri=_uri_for(collection, path) or "", doc_id=f"doc-gbcont-{path[-7:-4]}",
                salient_sentences=[],
            ))

        sqlite_db = tmp_path / "groupby_contents_parity.db"
        _seed_sqlite(sqlite_db, sqlite_rows)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: sqlite_db)

        sqlite_result = aspect_sql.try_groupby(
            items, "venue", source="aspects", aspect_field="extras.venue",
        )
        assert sqlite_result is not None
        sqlite_map = _groups_to_map(sqlite_result)

        # DIFFERENTIAL: same group keys AND same item membership
        assert svc_map == sqlite_map, (
            f"PARITY FAILURE (group contents):\n"
            f"  service  = {svc_map}\n"
            f"  sqlite   = {sqlite_map}"
        )

    def test_groupby_nested_extras_key_equal_sqlite(
        self, aspects_client, java_service, monkeypatch, tmp_path
    ) -> None:
        """DIFFERENTIAL: nested extras key (extras.meta.year) resolves correctly in
        Postgres (using #>> path operator) and equals SQLite's json_extract($.meta.year).

        This is the H1 fix: Java must use extras::json#>>'{meta,year}' not
        extras::json->>'meta.year' (which treats dotted key as a single top-level key).
        """
        from nexus.operators import aspect_sql

        collection = "knowledge__l9hd8"
        # Seed rows with nested extras (meta.year)
        nested_rows = [
            _make_aspect("nested-a", venue=None),
            _make_aspect("nested-b", venue=None),
        ]
        # Override extras with nested structure
        from nexus.db.t2.document_aspects import AspectRecord
        from nexus.aspect_readers import uri_for as _uri_for
        nested_rows_with_meta = []
        for suffix, year in [("nested-a", "2023"), ("nested-b", "2024")]:
            source_path = f"/l9hd8/{suffix}.pdf"
            nested_rows_with_meta.append(AspectRecord(
                collection=collection, source_path=source_path,
                problem_formulation="P", proposed_method="M",
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="R",
                extras={"meta": {"year": year}},  # nested extras
                confidence=0.8, extracted_at="2026-01-15T10:00:00Z",
                model_version="v1", extractor_name="test",
                source_uri=_uri_for(collection, source_path) or "",
                salient_sentences=[],
            ))
        for r in nested_rows_with_meta:
            aspects_client.upsert(r)

        paths = ["/l9hd8/nested-a.pdf", "/l9hd8/nested-b.pdf"]
        items = _items_json(paths, collection)

        # --- Service path ---
        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        svc_result = aspect_sql.try_groupby(
            items, "meta.year", source="aspects", aspect_field="extras.meta.year",
        )
        assert svc_result is not None, f"Service groupby returned None for nested extras key"
        svc_keys = {g["key_value"] for g in svc_result["groups"]}

        # --- SQLite path (same fixture) ---
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)

        sqlite_db = tmp_path / "nested_extras_parity.db"
        _seed_sqlite(sqlite_db, nested_rows_with_meta)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: sqlite_db)

        sqlite_result = aspect_sql.try_groupby(
            items, "meta.year", source="aspects", aspect_field="extras.meta.year",
        )
        assert sqlite_result is not None
        sqlite_keys = {g["key_value"] for g in sqlite_result["groups"]}

        # DIFFERENTIAL: nested key must resolve correctly on both backends
        assert svc_keys == sqlite_keys, (
            f"PARITY FAILURE (nested extras.meta.year):\n"
            f"  service  keys = {svc_keys}\n"
            f"  sqlite   keys = {sqlite_keys}\n"
            "Likely cause: Java uses ->>'meta.year' (treats dotted key as single "
            "top-level JSON key) instead of #>>'{meta,year}' (path traversal)."
        )
        # Both must produce the year groups (not 'unassigned')
        assert "2023" in svc_keys, f"Expected '2023' in service keys; got {svc_keys}"
        assert "2024" in svc_keys, f"Expected '2024' in service keys; got {svc_keys}"
        assert "unassigned" not in svc_keys, (
            "No unassigned expected — all rows have nested meta.year"
        )


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

    def test_avg_confidence_equal_sqlite(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """avg(confidence) from service == avg from SQLite (0.80 exact)."""
        from nexus.operators import aspect_sql

        collection = "knowledge__l9hd8"
        paths = ["/l9hd8/conf-a.pdf", "/l9hd8/conf-b.pdf", "/l9hd8/conf-c.pdf"]
        groups = _groups_json({"group1": paths}, collection)

        # --- Service path ---
        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        svc_result = aspect_sql.try_aggregate(
            groups, "avg confidence", source="aspects", aspect_field="confidence",
        )
        assert svc_result is not None

        # --- SQLite path ---
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)

        from nexus.db.t2.document_aspects import AspectRecord
        from nexus.aspect_readers import uri_for as _uri_for
        sqlite_rows = [
            AspectRecord(
                collection=collection, source_path=p,
                problem_formulation="P", proposed_method="M",
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="R", extras={},
                confidence=c, extracted_at="2026-01-15T10:00:00Z",
                model_version="v1", extractor_name="test",
                source_uri=_uri_for(collection, p) or "", doc_id=f"doc-conf-{i}",
                salient_sentences=[],
            )
            for i, (p, c) in enumerate([
                ("/l9hd8/conf-a.pdf", 0.80),
                ("/l9hd8/conf-b.pdf", 0.90),
                ("/l9hd8/conf-c.pdf", 0.70),
            ])
        ]
        sqlite_db = tmp_path / "conf_parity.db"
        _seed_sqlite(sqlite_db, sqlite_rows)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: sqlite_db)

        sqlite_result = aspect_sql.try_aggregate(
            groups, "avg confidence", source="aspects", aspect_field="confidence",
        )
        assert sqlite_result is not None

        # Extract numeric values from the aggregates summaries.
        # try_aggregate formats confidence as: "avg(confidence) = 0.800"
        def _parse_value(result):
            import re as _re
            agg = result["aggregates"][0]
            summary = agg.get("summary", "")
            m = _re.search(r"=\s*([\d.]+)", summary)
            if m:
                return float(m.group(1))
            try:
                return float(summary)
            except (ValueError, TypeError):
                return None

        svc_val = _parse_value(svc_result)
        sqlite_val = _parse_value(sqlite_result)

        assert svc_val is not None, f"Service avg confidence parse failed: {svc_result}"
        assert sqlite_val is not None, f"SQLite avg confidence parse failed: {sqlite_result}"

        # PARITY: values within floating-point tolerance
        assert abs(svc_val - sqlite_val) < 1e-6, (
            f"PARITY FAILURE: service avg={svc_val}, SQLite avg={sqlite_val}"
        )
        # Expected avg: (0.80 + 0.90 + 0.70) / 3 = 0.80
        assert abs(svc_val - 0.80) < 1e-6, (
            f"Expected avg(confidence)=0.80, got {svc_val}"
        )

    def test_max_confidence_equal_sqlite(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """max(confidence) from service == max from SQLite (DIFFERENTIAL: same fixture, both paths)."""
        import re as _re
        from nexus.operators import aspect_sql

        collection = "knowledge__l9hd8"
        paths = ["/l9hd8/conf-a.pdf", "/l9hd8/conf-b.pdf", "/l9hd8/conf-c.pdf"]
        groups = _groups_json({"g1": paths}, collection)

        # --- Service path ---
        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        svc_result = aspect_sql.try_aggregate(
            groups, "max confidence", source="aspects", aspect_field="confidence",
        )
        assert svc_result is not None
        m = _re.search(r"=\s*([\d.]+)", svc_result["aggregates"][0]["summary"])
        assert m, f"Cannot parse confidence from service result: {svc_result}"
        svc_val = float(m.group(1))

        # --- SQLite path (same fixture) ---
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)

        from nexus.db.t2.document_aspects import AspectRecord
        from nexus.aspect_readers import uri_for as _uri_for
        sqlite_rows = [
            AspectRecord(
                collection=collection, source_path=p,
                problem_formulation="P", proposed_method="M",
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="R", extras={},
                confidence=c, extracted_at="2026-01-15T10:00:00Z",
                model_version="v1", extractor_name="test",
                source_uri=_uri_for(collection, p) or "", doc_id=f"doc-max-{i}",
                salient_sentences=[],
            )
            for i, (p, c) in enumerate([
                ("/l9hd8/conf-a.pdf", 0.80),
                ("/l9hd8/conf-b.pdf", 0.90),
                ("/l9hd8/conf-c.pdf", 0.70),
            ])
        ]
        sqlite_db = tmp_path / "max_parity.db"
        _seed_sqlite(sqlite_db, sqlite_rows)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: sqlite_db)

        sqlite_result = aspect_sql.try_aggregate(
            groups, "max confidence", source="aspects", aspect_field="confidence",
        )
        assert sqlite_result is not None
        m2 = _re.search(r"=\s*([\d.]+)", sqlite_result["aggregates"][0]["summary"])
        assert m2, f"Cannot parse confidence from SQLite result: {sqlite_result}"
        sqlite_val = float(m2.group(1))

        # DIFFERENTIAL PARITY: both must agree
        assert abs(svc_val - sqlite_val) < 1e-6, (
            f"PARITY FAILURE: service max={svc_val}, SQLite max={sqlite_val}"
        )
        # Expected max = 0.90
        assert abs(svc_val - 0.90) < 1e-6, f"Expected max(confidence)=0.90, got {svc_val}"

    def test_min_confidence_equal_sqlite(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """min(confidence) from service == min from SQLite (DIFFERENTIAL: same fixture, both paths)."""
        import re as _re
        from nexus.operators import aspect_sql

        collection = "knowledge__l9hd8"
        paths = ["/l9hd8/conf-a.pdf", "/l9hd8/conf-b.pdf", "/l9hd8/conf-c.pdf"]
        groups = _groups_json({"g1": paths}, collection)

        # --- Service path ---
        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
        monkeypatch.setenv("NX_SERVICE_TENANT", "l9hd8-tenant")

        svc_result = aspect_sql.try_aggregate(
            groups, "min confidence", source="aspects", aspect_field="confidence",
        )
        assert svc_result is not None
        m = _re.search(r"=\s*([\d.]+)", svc_result["aggregates"][0]["summary"])
        assert m, f"Cannot parse confidence from service result: {svc_result}"
        svc_val = float(m.group(1))

        # --- SQLite path (same fixture) ---
        monkeypatch.delenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", raising=False)

        from nexus.db.t2.document_aspects import AspectRecord
        from nexus.aspect_readers import uri_for as _uri_for
        sqlite_rows = [
            AspectRecord(
                collection=collection, source_path=p,
                problem_formulation="P", proposed_method="M",
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="R", extras={},
                confidence=c, extracted_at="2026-01-15T10:00:00Z",
                model_version="v1", extractor_name="test",
                source_uri=_uri_for(collection, p) or "", doc_id=f"doc-min-{i}",
                salient_sentences=[],
            )
            for i, (p, c) in enumerate([
                ("/l9hd8/conf-a.pdf", 0.80),
                ("/l9hd8/conf-b.pdf", 0.90),
                ("/l9hd8/conf-c.pdf", 0.70),
            ])
        ]
        sqlite_db = tmp_path / "min_parity.db"
        _seed_sqlite(sqlite_db, sqlite_rows)
        monkeypatch.setattr("nexus.config.default_db_path", lambda: sqlite_db)

        sqlite_result = aspect_sql.try_aggregate(
            groups, "min confidence", source="aspects", aspect_field="confidence",
        )
        assert sqlite_result is not None
        m2 = _re.search(r"=\s*([\d.]+)", sqlite_result["aggregates"][0]["summary"])
        assert m2, f"Cannot parse confidence from SQLite result: {sqlite_result}"
        sqlite_val = float(m2.group(1))

        # DIFFERENTIAL PARITY: both must agree
        assert abs(svc_val - sqlite_val) < 1e-6, (
            f"PARITY FAILURE: service min={svc_val}, SQLite min={sqlite_val}"
        )
        # Expected min = 0.70
        assert abs(svc_val - 0.70) < 1e-6, f"Expected min(confidence)=0.70, got {svc_val}"


class TestRLSIsolation:
    """Cross-tenant RLS: service-mode operator_filter must not return other tenant's rows."""

    @pytest.fixture(scope="class", autouse=True)
    def seed_tenants(self, aspects_client, other_tenant_client) -> None:
        """Seed tenant A rows; tenant B gets nothing."""
        row = _make_aspect("rls-paxos", proposed_method="Paxos consensus algorithm")
        aspects_client.upsert(row)

    def test_other_tenant_gets_no_matches(
        self, java_service, monkeypatch, tmp_path
    ) -> None:
        """Tenant B operator_filter must return no matches for tenant A's rows."""
        from nexus.operators import aspect_sql

        base_url, token, svc_port = java_service
        monkeypatch.setenv("NX_STORAGE_BACKEND_DOCUMENT_ASPECTS", "service")
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(svc_port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", token)
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
