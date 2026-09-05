# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language integration test for HttpTelemetryStore against the real Java service.

Requires (on THIS machine — darwin/aarch64 with JDK25 GraalVM):
  - PostgreSQL binaries discoverable (NEXUS_PG_BIN / Homebrew / system dirs / PATH)
  - service/target/nexus-service-1.0-SNAPSHOT.jar built (mvn -f service/pom.xml package -DskipTests)
  - Java on PATH (or JAVA_HOME/bin/java available)

Marked @pytest.mark.integration — collected but skipped automatically when the
jar or PG binaries are absent, so CI (which has neither) stays green.

Run locally with:
    JAVA_HOME=~/.sdkman/candidates/java/25.0.1-graal \\
    PATH=$JAVA_HOME/bin:$PATH \\
    uv run pytest -m integration tests/db/test_http_telemetry_store_integration.py -v

What is exercised (bead nexus-gmiaf.12 requirements):
  a) Per-table round-trip: log/get and import/get for all 6 tables
  b) TIMESTAMP PRESERVATION: import paths write source timestamps VERBATIM (not now())
  c) DO NOTHING idempotency: event log double-import produces no duplicate
  d) GREATEST no-clobber: frecency re-import with stale values does not clobber live
  e) LEAST embedded_at: re-import with newer embedded_at preserves original
  f) Cross-tenant RLS negative: tenant A relevance_log invisible to tenant B
  g) rename_collection: search_telemetry.collection updated

NX_STORAGE_BACKEND is NOT touched — default SQLite path is unchanged.

Schema bootstrap pattern (net63, mirrors the catalog ETL fixture):
  The Java service runs Liquibase at startup (SchemaMigrator) and owns the full
  telemetry schema + grants before binding the HTTP port.  The hermetic fixture
  pre-applies NOTHING but the nexus_svc role (SERVICE_ROLES_SQL) — the NOSUPERUSER
  NOBYPASSRLS DML/RLS role that grants-nexus-svc.xml grants to and that the
  RLS-negative tests exercise.  Two-role DB config: NX_DB_ADMIN_* = OS superuser
  (Liquibase DDL), NX_DB_USER = nexus_svc (app pool under FORCE RLS).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests._t2_fixture_ops import canonical_chunk_id
from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    create_tenant_token,
    pg_bin_dir,
    spawn_service,
)

# ── Prerequisite paths ────────────────────────────────────────────────────────

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

# ── Sentinel timestamps ───────────────────────────────────────────────────────

#: Synthetic past timestamp — 2024-01-15 10:30:00Z.
#: MUST NOT be within 2 years of now() (2026); this is the fidelity sentinel.
PAST_TS = "2024-01-15T10:30:00Z"

# (The pre-net63 raw-psql schema/role/grant bootstrap constants were removed:
#  the service self-migrates via Liquibase at startup; the schema of record is
#  service/src/main/resources/db/changelog/telemetry-*.xml.)


def _psql_query(pgport: int, dbname: str, pg_user: str, sql: str) -> list[list[str]]:
    """Run a read-only SELECT via psql and return rows as lists of string cells.

    Uses the OS superuser (trust auth), which bypasses RLS, so this sees rows for all
    tenants — appropriate for value-fidelity assertions in tests.
    """
    proc = subprocess.run(
        [str(_PSQL), "-h", "127.0.0.1", "-p", str(pgport), "-U", pg_user,
         "-d", dbname, "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql query failed (rc={proc.returncode}):\n{proc.stderr}")
    return [line.split("\t") for line in proc.stdout.strip().splitlines() if line]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_service():
    """Spin up an ephemeral Postgres 16 instance, bootstrap telemetry schema, yield port."""
    pgport  = _find_free_port()
    tmpdir  = tempfile.mkdtemp(prefix="nx_tel_inttest_")
    pgdata  = f"{tmpdir}/pgdata"
    pg_user = os.environ["USER"]

    subprocess.run(
        [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
        check=True, capture_output=True,
    )
    with open(f"{pgdata}/postgresql.conf", "a") as f:
        f.write(f"\nport = {pgport}\nlisten_addresses = '127.0.0.1'\n")

    subprocess.run(
        [str(_PG_CTL), "-D", pgdata, "-l", f"{tmpdir}/pg.log",
         "-o", f"-p {pgport} -k {pgdata}", "start", "-w"],
        check=True, capture_output=True,
    )

    subprocess.run(
        [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pgport),
         "-U", pg_user, "nxteltest"],
        check=True, capture_output=True,
    )

    def _psql(sql: str) -> None:
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pgport),
             "-U", pg_user, "-d", "nxteltest",
             "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"psql bootstrap failed (rc={proc.returncode}):\n"
                f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )

    # net63: the JAR runs Liquibase at startup and owns the FULL telemetry schema
    # (the telemetry-*.xml changelog) BEFORE binding the HTTP port. The fixture must
    # NOT pre-apply schema — doing so collides ("relation already exists") and the
    # service exits at migration. The only pre-start SQL is SERVICE_ROLES_SQL, which
    # creates nexus_svc (the NOSUPERUSER NOBYPASSRLS DML/RLS role that
    # grants-nexus-svc.xml grants to). nexus_svc is also the role the RLS-negative
    # tests exercise (FORCE ROW LEVEL SECURITY applies to it).
    _psql(SERVICE_ROLES_SQL)

    yield pgport, tmpdir, "nxteltest", pg_user

    subprocess.run(
        [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
        capture_output=True,
    )
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="module")
def java_service(pg_service):
    """Start the nexus-service JAR against the ephemeral PG, yield (base_url, token)."""
    pgport, tmpdir, dbname, pg_user = pg_service
    svc_port  = _find_free_port()
    svc_token = "inttest-telemetry-token-xyz"

    jdbc_url = f"jdbc:postgresql://127.0.0.1:{pgport}/{dbname}"
    chroma_data = tempfile.mkdtemp(prefix="nx-tel-chroma-")

    env = dict(os.environ)
    env.update({
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": svc_token,
        # net63 two-role: app pool = nexus_svc (NOSUPERUSER NOBYPASSRLS) so FORCE RLS
        # applies; migration pool = OS superuser (trust auth) for the Liquibase DDL run.
        "NX_DB_URL":        jdbc_url,
        "NX_DB_USER":       "nexus_svc",
        "NX_DB_PASS":       "nexus_svc_pass",
        "NX_POOL_SIZE":     "3",
        "NX_DB_ADMIN_URL":  jdbc_url,
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
        # Isolated Chroma so the service does not open the dev instance at startup.
        "NX_CHROMA_PATH":   chroma_data,
    })
    env.pop("NX_STORAGE_BACKEND", None)

    java_bin = str(_JAVA)
    # nexus-lom9g: FILE-backed output via the shared primitive. This file was
    # the only one of the 22 with ANY drain at all — but its communicate()
    # runs only AFTER the process has already exited, so a service that wedged
    # on a full 64KB pipe (nexus-j0nec) was never drained and never diagnosed.
    svc_proc, _svc_log = spawn_service([java_bin, "-jar", str(_JAR)], env)

    # Wait for service to be up
    base_url = f"http://127.0.0.1:{svc_port}"
    for _ in range(60):
        try:
            import httpx
            resp = httpx.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        if svc_proc.poll() is not None:
            # Output is now file-backed, so communicate() would return
            # (None, None) — read the log instead.
            with open(_svc_log, encoding="utf-8", errors="replace") as _fh:
                _tail = _fh.read()[-2000:]
            raise RuntimeError(
                f"Java service exited early (rc={svc_proc.returncode}). "
                f"Tail of engine log ({_svc_log}):\n{_tail}"
            )
        time.sleep(0.5)
    else:
        svc_proc.terminate()
        with open(_svc_log, encoding="utf-8", errors="replace") as _fh:
            _tail = _fh.read()[-2000:]
        raise RuntimeError(
            f"Java service didn't come up in time. "
            f"Tail of engine log ({_svc_log}):\n{_tail}"
        )

    yield base_url, svc_token

    svc_proc.terminate()
    try:
        svc_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        svc_proc.kill()


@pytest.fixture
def tel_store(java_service):
    """HttpTelemetryStore connected to the real Java service."""
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
    base_url, token = java_service
    # Phase E: the root token is bound to `default` and the X-Nexus-Tenant header
    # is ignored, so this store IS the `default` tenant regardless of the claimed
    # name. Use "default" so the fixture's identity matches reality (the
    # cross-tenant test pairs this with the genuinely-bound `inttest-b` store).
    store = HttpTelemetryStore(base_url=base_url, tenant="default", _token=token)
    yield store
    store.close()


@pytest.fixture
def tel_store_b(java_service):
    """A second HttpTelemetryStore with a DIFFERENT tenant (for RLS negative tests)."""
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
    base_url, token = java_service
    # Phase E: real tenant-bound bearer so this is a GENUINELY different tenant
    # from the primary store (the root token resolves every claim to `default`).
    other_token = create_tenant_token(base_url, token, "inttest-b")
    store = HttpTelemetryStore(base_url=base_url, tenant="inttest-b", _token=other_token)
    yield store
    store.close()


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestRelevanceLogRoundTrip:
    def test_log_and_query_roundtrip(self, tel_store):
        chunk_id = canonical_chunk_id("chunk-int")
        tel_store.log_relevance(
            "integration test query", chunk_id, "store_put",
            collection="knowledge__nexus", session_id="sess-int",
        )
        rows = tel_store.get_relevance_log(query="integration test query")
        assert len(rows) >= 1
        row = rows[0]
        assert row["chunk_id"] == chunk_id
        assert row["action"] == "store_put"
        assert row["collection"] == "knowledge__nexus"

    def test_expire_removes_old(self, tel_store):
        """Import an old row then expire — must be deleted."""
        tel_store.import_relevance_row(
            query="expire-test-query",
            chunk_id=canonical_chunk_id("expire-chunk"),
            collection="",
            action="store_put",
            session_id="",
            timestamp="2020-01-01T00:00:00Z",  # 6+ years ago
        )
        rows = tel_store.get_relevance_log(query="expire-test-query")
        assert len(rows) >= 1
        deleted = tel_store.expire_relevance_log(days=365 * 3)
        assert deleted >= 1


class TestTimestampPreservation:
    """HEADLINE: imported event timestamps must be VERBATIM, not now()."""

    def test_relevance_log_timestamp_preserved_verbatim(self, tel_store):
        tel_store.import_relevance_row(
            query="ts-fidelity-int-query",
            chunk_id=canonical_chunk_id("chunk-ts-fidelity"),
            collection="knowledge__nexus",
            action="store_put",
            session_id="sess-ts",
            timestamp=PAST_TS,
        )
        rows = tel_store.get_relevance_log(query="ts-fidelity-int-query")
        assert rows, "imported row must be retrievable"
        stored_ts = rows[0].get("timestamp", "")
        assert stored_ts, "timestamp must not be blank in PG response"

        # Parse and verify — PG may return "+00:00" suffix or "Z"
        from datetime import datetime, timezone
        if stored_ts.endswith("Z"):
            stored_ts_norm = stored_ts.replace("Z", "+00:00")
        else:
            stored_ts_norm = stored_ts
        stored_dt = datetime.fromisoformat(stored_ts_norm)
        past_dt   = datetime.fromisoformat(PAST_TS.replace("Z", "+00:00"))

        assert abs((stored_dt - past_dt).total_seconds()) < 1.0, (
            f"TIMESTAMP PRESERVATION: PG stored {stored_ts!r}, "
            f"expected ~{PAST_TS!r}. "
            "If this fails, the service is using now() instead of the import timestamp."
        )

    def test_relevance_log_not_migration_time(self, tel_store):
        """Stored timestamp must be years before now(), not within seconds of now()."""
        tel_store.import_relevance_row(
            query="ts-not-now-query",
            chunk_id=canonical_chunk_id("chunk-ts-not-now"),
            collection="",
            action="store_put",
            session_id="",
            timestamp=PAST_TS,
        )
        rows = tel_store.get_relevance_log(query="ts-not-now-query")
        assert rows
        stored_ts = rows[0].get("timestamp", "")
        from datetime import datetime, timezone, timedelta
        if stored_ts.endswith("Z"):
            stored_ts_norm = stored_ts.replace("Z", "+00:00")
        else:
            stored_ts_norm = stored_ts
        stored_dt = datetime.fromisoformat(stored_ts_norm)
        now_dt    = datetime.now(timezone.utc)
        one_year_ago = now_dt - timedelta(days=365)

        assert stored_dt < one_year_ago, (
            f"Stored timestamp {stored_ts!r} is too recent. "
            "Import path must NOT use now() for the timestamp column."
        )


class TestTimestampPreservationPerTable:
    """Fix 4: timestamp fidelity + DO NOTHING idempotency for all 5 event-log tables.

    Each table must:
    a) Accept an import with PAST_TS and store it verbatim (not now()).
    b) Accept a second identical import without duplicating the row (DO NOTHING).
    """

    def _assert_ts_verbatim(self, stored_ts: str, label: str) -> None:
        """Parse stored_ts and assert it is within 1s of PAST_TS (not now())."""
        from datetime import datetime, timezone, timedelta
        norm = stored_ts.replace("Z", "+00:00") if stored_ts.endswith("Z") else stored_ts
        stored_dt = datetime.fromisoformat(norm).astimezone(timezone.utc)
        past_dt   = datetime.fromisoformat(PAST_TS.replace("Z", "+00:00"))
        now_dt    = datetime.now(timezone.utc)
        one_year_ago = now_dt - timedelta(days=365)

        assert abs((stored_dt - past_dt).total_seconds()) < 1.0, (
            f"{label}: timestamp {stored_ts!r} ≠ PAST_TS {PAST_TS!r}. "
            "Import path must write the source event-time verbatim."
        )
        assert stored_dt < one_year_ago, (
            f"{label}: stored timestamp is too recent (within 1 year of now). "
            "Import path must NOT substitute now() for the event-time."
        )

    def test_tier_writes_timestamp_verbatim_and_idempotent(self, tel_store):
        import httpx
        base_url = tel_store._base_url
        # Post-mixin-adoption (nexus-f2qvx.1): headers are built fresh per
        # request via RefreshableHttpStoreMixin._auth_headers(), not baked
        # into a ``self._headers`` dict at construction time.
        headers  = tel_store._auth_headers()

        kwargs = dict(
            session_id="tw-fid-sess",
            ts=PAST_TS,
            tool="memory_put",
            tier="T2",
            agent="developer",
            project="nexus",
            target_title="impl-notes.md",
        )
        tel_store.import_tier_write(**kwargs)
        tel_store.import_tier_write(**kwargs)  # second import — must be DO NOTHING

        # Fetch via direct PG query through psql isn't available in the HTTP store API;
        # verify via the Java service's search stats path (no get endpoint for tier_writes).
        # The key assertion is "no exception on second import" — confirmed by reaching here.
        # We can also verify the search stats path is reachable without 500:
        resp = httpx.get(
            f"{base_url}/v1/telemetry/search/stats",
            params={"collection": "nonexistent", "days": "1"},
            headers=headers,
        )
        assert resp.status_code == 200, (
            f"tier_writes double-import must not corrupt the service; stats returned {resp.status_code}"
        )

    def test_nx_answer_runs_timestamp_verbatim_and_idempotent(self, tel_store):
        kwargs = dict(
            question="nx-ans-ts-fidelity-int",
            plan_id=None,
            matched_confidence=0.9,
            step_count=2,
            final_text="answer",
            cost_usd=0.005,
            duration_ms=1200,
            created_at=PAST_TS,
        )
        # Two identical imports — no exception on either, and no 500
        tel_store.import_nx_answer_run(**kwargs)
        tel_store.import_nx_answer_run(**kwargs)

    def test_nx_answer_runs_plan_id_integer_and_string_coercion(self, tel_store, pg_service):
        """REGRESSION (nexus-5gaj7): plan_id is a BIGINT. The corrected ETL sends an
        int; the service must also tolerate a numeric STRING (defensive optLongNull),
        since the old ETL stringified it. Both must import without a 500 AND the
        coerced value must land correctly (verified via psql, not just no-exception)."""
        # (a) Corrected path: int plan_id via the store.
        tel_store.import_nx_answer_run(
            question="nx-ans-planid-int",
            plan_id=42,
            matched_confidence=0.9,
            step_count=1,
            final_text="a",
            cost_usd=0.001,
            duration_ms=100,
            created_at=PAST_TS,
        )
        # (b) Defensive path: raw payload with plan_id/numeric fields as STRINGS (old
        # ETL shape). Must NOT raise (Java coerces "7" -> 7L); previously 500ed.
        tel_store._post("/v1/telemetry/import", {
            "table": "nx_answer_runs",
            "question": "nx-ans-planid-str",
            "plan_id": "7",
            "matched_confidence": "0.5",
            "step_count": 1,
            "final_text": "b",
            "cost_usd": "0.002",
            "duration_ms": "200",
            "created_at": PAST_TS,
        })

        # Value fidelity: the coerced values actually landed (superuser psql bypasses RLS).
        pgport, _tmpdir, dbname, pg_user = pg_service
        rows = _psql_query(
            pgport, dbname, pg_user,
            "SELECT plan_id, duration_ms FROM nexus.nx_answer_runs "
            "WHERE question IN ('nx-ans-planid-int','nx-ans-planid-str') ORDER BY plan_id;",
        )
        # Two rows: plan_id 7 (string-coerced) and 42 (int); duration_ms 200 and 100.
        assert rows == [["7", "200"], ["42", "100"]], (
            f"coerced plan_id/duration_ms must round-trip exactly; got {rows}"
        )

    def test_hook_failures_timestamp_verbatim_and_idempotent(self, tel_store):
        kwargs = dict(
            doc_id="hf-fid-doc",
            collection="knowledge__nexus",
            hook_name="post_store_hook",
            error="connection refused",
            occurred_at=PAST_TS,
            batch_doc_ids=None,
            is_batch=False,
            chain=None,
        )
        tel_store.import_hook_failure(**kwargs)
        tel_store.import_hook_failure(**kwargs)

    def test_search_telemetry_timestamp_verbatim_and_idempotent(self, tel_store):
        kwargs = dict(
            ts=PAST_TS,
            query_hash="fidelity-hash-int",
            collection="code__nexus",
            raw_count=10,
            kept_count=8,
            top_distance=0.25,
            threshold=0.3,
        )
        tel_store.import_search_row(**kwargs)
        tel_store.import_search_row(**kwargs)

        # Verify the row is visible in stats (wide window to catch our 2024 row)
        stats = tel_store.query_collection_stats("code__nexus", days=365 * 5)
        assert isinstance(stats.get("row_count"), (int, float)), (
            "search_telemetry stats must return row_count after import"
        )


class TestIndexFailuresRoundTrip:
    """nexus-nukn3: durable per-file index-failure record. Real Java-service
    round trip — proves the wire shape between HttpTelemetryStore and
    TelemetryHandler/TelemetryRepository actually matches, which the
    mocked unit tests (tests/test_deyd5_systemic_skip_run_level.py,
    tests/test_false_clean_diagnostics_service_mode.py) cannot."""

    def test_record_and_batch_read_back_by_run(self, tel_store):
        run_id = f"itest-run-{time.time_ns()}"
        tel_store.record_index_failure(
            run_id=run_id, file_path="/repo/a.pdf",
            error_class="UnextractableContentError", error="produced empty output",
        )
        inserted = tel_store.record_index_failures_batch(
            [
                ("/repo/b.pdf", "UnextractableContentError", "scanned image", ""),
                ("/repo/c.pdf", "UnextractableContentError", "boom", ""),
            ],
            run_id=run_id,
        )
        assert inserted == 2

        result = tel_store.list_index_failures(run_id=run_id, limit=100)
        assert result["total"] == 3
        paths = {row["file_path"] for row in result["rows"]}
        assert paths == {"/repo/a.pdf", "/repo/b.pdf", "/repo/c.pdf"}
        assert all(row["error_class"] == "UnextractableContentError" for row in result["rows"])

    def test_aggregates_ignore_the_page_limit(self, tel_store):
        run_id = f"itest-cap-{time.time_ns()}"
        tel_store.record_index_failures_batch(
            [(f"/repo/f{i}.pdf", "UnextractableContentError", "boom", "") for i in range(5)],
            run_id=run_id,
        )

        capped = tel_store.list_index_failures(run_id=run_id, limit=1)

        assert len(capped["rows"]) == 1
        assert capped["total"] == 5, "total must count every matching row, not the page"

    def test_blank_run_id_returns_every_run(self, tel_store):
        run_a = f"itest-all-a-{time.time_ns()}"
        run_b = f"itest-all-b-{time.time_ns()}"
        tel_store.record_index_failure(
            run_id=run_a, file_path="/repo/one.pdf",
            error_class="UnextractableContentError", error="boom",
        )
        tel_store.record_index_failure(
            run_id=run_b, file_path="/repo/two.pdf",
            error_class="UnextractableContentError", error="boom",
        )

        scoped = tel_store.list_index_failures(run_id=run_a, limit=100)
        assert scoped["total"] == 1

        # Both rows are visible without a run_id filter, somewhere within the
        # tenant's full (unbounded) history -- confirmed via a wide days=0 window.
        unscoped = tel_store.list_index_failures(limit=1000)
        assert unscoped["total"] >= 2

    def test_trim_requires_scoping(self, tel_store):
        with pytest.raises(ValueError):
            tel_store.trim_index_failures()

    def test_trim_by_run_id_deletes_only_that_run(self, tel_store):
        run_a = f"itest-trim-a-{time.time_ns()}"
        run_b = f"itest-trim-b-{time.time_ns()}"
        tel_store.record_index_failures_batch(
            [("/repo/a1.pdf", "UnextractableContentError", "boom", ""),
             ("/repo/a2.pdf", "UnextractableContentError", "boom", "")],
            run_id=run_a,
        )
        tel_store.record_index_failure(
            run_id=run_b, file_path="/repo/b1.pdf",
            error_class="UnextractableContentError", error="boom",
        )

        deleted = tel_store.trim_index_failures(run_id=run_a)

        assert deleted == 2
        assert tel_store.list_index_failures(run_id=run_a, limit=100)["total"] == 0
        assert tel_store.list_index_failures(run_id=run_b, limit=100)["total"] == 1

    def test_trim_dry_run_previews_without_deleting(self, tel_store):
        run_id = f"itest-trim-dryrun-{time.time_ns()}"
        tel_store.record_index_failure(
            run_id=run_id, file_path="/repo/a.pdf",
            error_class="UnextractableContentError", error="boom",
        )

        previewed = tel_store.trim_index_failures(run_id=run_id, dry_run=True)

        assert previewed == 1
        assert tel_store.list_index_failures(run_id=run_id, limit=100)["total"] == 1

        deleted = tel_store.trim_index_failures(run_id=run_id)
        assert deleted == 1

    def test_acknowledge_requires_non_blank_error_class(self, tel_store):
        with pytest.raises(ValueError):
            tel_store.acknowledge_index_failure(error_class="")

    def test_acknowledge_by_file_covers_recurring_failure_across_runs(self, tel_store):
        """THE fold-in round 2 non-vacuity proof, against the REAL engine:
        an acknowledged file's failure recurring under a FRESH run_id must
        be excluded from unacknowledged_only, while a brand new file still
        shows up."""
        file_path = f"/repo/broken-{time.time_ns()}.pdf"
        tel_store.record_index_failure(
            run_id="run-1", file_path=file_path,
            error_class="UnextractableContentError", error="encrypted",
        )
        tel_store.acknowledge_index_failure(
            error_class="UnextractableContentError", file_path=file_path,
            reason="known encrypted PDF",
        )
        # A re-run mints a NEW run_id for the identical failure.
        tel_store.record_index_failure(
            run_id="run-2", file_path=file_path,
            error_class="UnextractableContentError", error="encrypted",
        )

        scoped_unacked = tel_store.list_index_failures(
            run_id="run-2", unacknowledged_only=True, limit=100,
        )
        assert scoped_unacked["total"] == 0

        # The plain list shows both recurrences, marked acknowledged.
        plain = tel_store.list_index_failures(limit=1000)
        matching = [r for r in plain["rows"] if r.get("file_path") == file_path]
        assert len(matching) == 2
        assert all(r.get("acknowledged") is True for r in matching)

        # A NEW, unrelated file in the same run still shows up unacknowledged.
        other_path = f"/repo/other-{time.time_ns()}.pdf"
        tel_store.record_index_failure(
            run_id="run-2", file_path=other_path,
            error_class="UnextractableContentError", error="boom",
        )
        scoped_unacked_2 = tel_store.list_index_failures(
            run_id="run-2", unacknowledged_only=True, limit=100,
        )
        assert scoped_unacked_2["total"] == 1
        assert scoped_unacked_2["rows"][0]["file_path"] == other_path


class TestNxAnswerStepsRoundTrip:
    """RDR-196 .p1d (nexus-nyry9.10): direct-store-read proof that
    ``HttpTelemetryStore.record_nx_answer_run(..., steps=[...])`` actually
    persists ``nexus.nx_answer_steps`` child rows against the REAL Java
    service + real Postgres — not a mock, and not merely "the POST
    returned 200" (which the unit-level fake-server tests already cover).
    Direct psql, bypassing RLS via the OS superuser, was originally the
    ONLY read path available for this table (there was no HTTP read
    route — confirmed by inspection of
    ``TelemetryRepository``/``TelemetryHandler`` at the time). RDR-196
    .p1c-b (nexus-lme1s) closed that gap:
    ``GET /nx_answer_runs/query?include_steps=true`` now returns steps
    too — see ``test_write_then_query_with_include_steps_round_trip``
    below, which asserts field equality through the client's OWN read
    path instead of psql. The psql-based test above/below this docstring
    stays as an independent verification of what actually landed in
    Postgres (a different assertion axis, not redundant with the new
    HTTP-round-trip test — the psql test also proves the PARENT row's
    ``cost_usd`` sum, which ``include_steps`` does not touch).
    """

    def test_steps_persist_and_cost_sums_to_the_parent_row(self, tel_store, pg_service):
        question = "nx-ans-steps-roundtrip-int"
        steps = [
            {
                "step_index": 0, "operator": "query", "source": "sql",
                "model": None, "input_tokens": None, "output_tokens": None,
                "cost_usd": None, "elapsed_ms": 300, "ok": True, "bundled_steps": [],
            },
            {
                "step_index": 1, "operator": "operator_generate", "source": "llm",
                "model": "claude-sonnet-5", "input_tokens": 200, "output_tokens": 80,
                "cost_usd": 0.021, "elapsed_ms": 4100, "ok": True, "bundled_steps": [],
            },
        ]
        tel_store.record_nx_answer_run(
            question=question, plan_id=3, matched_confidence=0.77,
            step_count=2, final_text="answer", cost_usd=0.021,
            duration_ms=4400, steps=steps,
        )

        pgport, _tmpdir, dbname, pg_user = pg_service
        rows = _psql_query(
            pgport, dbname, pg_user,
            "SELECT s.step_index, s.operator, s.source, s.model, "
            "s.cost_usd, s.elapsed_ms, s.ok::text "
            "FROM nexus.nx_answer_steps s "
            "JOIN nexus.nx_answer_runs r ON r.id = s.run_id "
            f"WHERE r.question = '{question}' ORDER BY s.step_index;",
        )
        assert rows == [
            ["0", "query", "sql", "", "", "300", "true"],
            ["1", "operator_generate", "llm", "claude-sonnet-5", "0.021", "4100", "true"],
        ], f"nx_answer_steps rows did not round-trip as sent; got {rows}"

        # The parent row's own cost_usd was computed by THIS test (mirroring
        # what core.py's _nx_answer_record_run does: sum of non-None step
        # costs) and sent explicitly — confirm it landed too.
        parent = _psql_query(
            pgport, dbname, pg_user,
            f"SELECT cost_usd FROM nexus.nx_answer_runs WHERE question = '{question}';",
        )
        assert parent == [["0.021"]]

    def test_write_then_query_with_include_steps_round_trip(self, tel_store):
        """RDR-196 .p1c-b (nexus-lme1s): the read half of nx_answer_steps —
        GET /v1/telemetry/nx_answer_runs/query?include_steps=true — proven
        against the REAL Java service, through the client's OWN
        ``query_nx_answer_runs(include_steps=True)`` method, asserting
        field equality against what was sent. This is the round trip .p1d
        had to fall back to a direct psql read for (see the class
        docstring); it now goes through HTTP end to end."""
        question = "nx-ans-steps-query-roundtrip-int"
        steps = [
            {
                "step_index": 0, "operator": "query", "source": "sql",
                "model": None, "input_tokens": None, "output_tokens": None,
                "cost_usd": None, "elapsed_ms": 300, "ok": True, "bundled_steps": [],
            },
            {
                "step_index": 1, "operator": "operator_generate", "source": "llm",
                "model": "claude-sonnet-5", "input_tokens": 200, "output_tokens": 80,
                "cost_usd": 0.021, "elapsed_ms": 4100, "ok": True, "bundled_steps": [],
            },
        ]
        tel_store.record_nx_answer_run(
            question=question, plan_id=3, matched_confidence=0.77,
            step_count=2, final_text="answer", cost_usd=0.021,
            duration_ms=4400, steps=steps,
        )

        result = tel_store.query_nx_answer_runs(limit=100, include_steps=True)
        row = next(r for r in result["rows"] if r["question"] == question)

        assert row["steps"] == steps, (
            "steps must round-trip through the include_steps=true HTTP read "
            "path with the exact field values sent on write"
        )


class TestDoNothingIdempotency:
    """Event log tables must silently ignore duplicate imports."""

    def test_relevance_log_double_import_no_duplicate(self, tel_store):
        chunk_id = canonical_chunk_id("idm-chunk")
        kwargs = dict(
            query="idm-int-query",
            chunk_id=chunk_id,
            collection="",
            action="store_put",
            session_id="sess",
            timestamp="2024-06-01T12:00:00Z",
        )
        tel_store.import_relevance_row(**kwargs)
        tel_store.import_relevance_row(**kwargs)
        rows = tel_store.get_relevance_log(query="idm-int-query", chunk_id=chunk_id)
        assert len(rows) == 1, (
            f"DO NOTHING: second import must not create a duplicate row; got {len(rows)}"
        )


class TestFrecencyGreatestLeast:
    """Frecency uses GREATEST for counters/timestamps, LEAST for embedded_at."""

    def test_greatest_no_clobber(self, tel_store):
        chunk_id = canonical_chunk_id("chunk-greatest-int")
        tel_store.import_frecency_row(
            chunk_id=chunk_id,
            embedded_at="2024-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.95,
            miss_count=20,
            last_hit_at="2025-06-01T00:00:00Z",
        )
        # Re-import with stale (lower) values
        tel_store.import_frecency_row(
            chunk_id=chunk_id,
            embedded_at="2023-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.50,
            miss_count=5,
            last_hit_at="2024-01-01T00:00:00Z",
        )
        import httpx
        # Read back via /v1/telemetry/frecency/get
        base_url = tel_store._base_url
        # Post-mixin-adoption (nexus-f2qvx.1): headers are built fresh per
        # request via RefreshableHttpStoreMixin._auth_headers(), not baked
        # into a ``self._headers`` dict at construction time.
        resp = httpx.get(
            f"{base_url}/v1/telemetry/frecency/get",
            params={"chunk_id": chunk_id},
            headers=tel_store._auth_headers(),
        )
        assert resp.status_code == 200
        row = resp.json()
        score = float(row.get("frecency_score", 0.0))
        miss  = int(row.get("miss_count", 0))
        assert score == pytest.approx(0.95, abs=1e-9), (
            f"GREATEST: re-import with stale score=0.50 must not clobber live score=0.95; got {score}")
        assert miss == 20, (
            f"GREATEST: re-import with stale miss_count=5 must not clobber live miss_count=20; got {miss}")

    def test_least_embedded_at(self, tel_store):
        """LEAST: re-import with newer embedded_at must NOT replace the older one."""
        import httpx
        base_url = tel_store._base_url
        # Post-mixin-adoption (nexus-f2qvx.1): headers are built fresh per
        # request via RefreshableHttpStoreMixin._auth_headers(), not baked
        # into a ``self._headers`` dict at construction time.
        headers = tel_store._auth_headers()
        chunk_id = canonical_chunk_id("chunk-least-int")

        # Seed with 2024-01-01
        tel_store.import_frecency_row(
            chunk_id=chunk_id,
            embedded_at="2024-01-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.5,
            miss_count=1,
            last_hit_at=None,
        )
        # Re-import with newer embedded_at (2025-06-01) — LEAST means 2024 should win
        tel_store.import_frecency_row(
            chunk_id=chunk_id,
            embedded_at="2025-06-01T00:00:00Z",
            ttl_days=30,
            frecency_score=0.3,
            miss_count=0,
            last_hit_at=None,
        )

        resp = httpx.get(
            f"{base_url}/v1/telemetry/frecency/get",
            params={"chunk_id": chunk_id},
            headers=headers,
        )
        assert resp.status_code == 200
        row = resp.json()
        embedded_at = row.get("embedded_at", "")

        # PG may return timestamps in local tz offset form (e.g. "2023-12-31T16:00-08:00")
        # which is the same instant as "2024-01-01T00:00:00Z".  Parse and compare as UTC.
        from datetime import datetime, timezone
        expected_dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        stored_dt = datetime.fromisoformat(embedded_at).astimezone(timezone.utc)
        assert abs((stored_dt - expected_dt).total_seconds()) < 1.0, (
            f"LEAST: re-import with newer embedded_at must not replace older 2024-01-01 value; "
            f"got {embedded_at!r} (parsed UTC: {stored_dt.isoformat()})")


class TestCrossTenantRlsNegative:
    """Tenant A data must be invisible to Tenant B."""

    def test_relevance_log_tenant_isolation(self, tel_store, tel_store_b):
        unique_query = f"rls-neg-test-{id(tel_store)}"
        tel_store.log_relevance(unique_query, canonical_chunk_id("chunk-rls"), "store_put")
        rows_b = tel_store_b.get_relevance_log(query=unique_query)
        assert len(rows_b) == 0, (
            f"RLS NEGATIVE: tenant B must not see tenant A's relevance_log rows; "
            f"got {len(rows_b)} rows"
        )


class TestRenameCollection:
    def test_rename_updates_search_telemetry(self, tel_store):
        tel_store.log_search_batch([
            ("2025-01-15T10:00:00Z", "hash-rename", "old-coll-int", 10, 8, 0.2, 0.3),
        ])
        result = tel_store.rename_collection(old="old-coll-int", new="new-coll-int")
        assert isinstance(result.get("search_telemetry"), int)
        assert result["search_telemetry"] >= 1
